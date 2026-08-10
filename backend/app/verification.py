from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.constraints import MAX_QUOTE_WORDS, MAX_QUOTES_PER_PRINCIPLE, count_words, find_quotes
from app.json_extraction import JSONExtractionError, extract_json_object
from app.llm import LLMClient
from app.models import Principle

MAX_SUGGESTIONS = 3

# Lens speaks TO the user as their advisor, not AS the user -- a reflection
# written in first person ("I'm feeling...", "my mood...") reads like the
# user's own voice, not advice. Checked with a regex (not left to the LLM's
# prompt compliance alone) for the same reason quote limits are: rule-based
# code is deterministic, the model's instruction-following isn't.
_FIRST_PERSON_PATTERN = re.compile(r"\b(?:I|I'm|I've|I'd|I'll|my|mine|myself|me)\b")

ENTAILMENT_PROMPT_TEMPLATE = """\
You are a fact-checker verifying that a generated reflection + suggestions
do not misrepresent the book principles they claim to be grounded in.

PRINCIPLES:
{principles_block}

REFLECTION:
{reflection_block}

SUGGESTIONS:
{suggestions_block}

Fail this check ONLY if the reflection or a suggestion's explanation does
one of these:
- Attributes a claim, fact, or idea to the book that contradicts or is
  unrelated to the principles above.
- Cites a principle_id for the wrong principle, or describes a principle
  as saying something it does not say.

Do NOT fail this check just because a suggestion's action (its "text"
field) is a concrete, specific tactic that isn't written verbatim in the
principles -- suggestions are supposed to translate a principle into a
concrete next step, and that is expected, not a violation. Only the
REFLECTION and each suggestion's EXPLANATION need to stay strictly
accurate about what the principles say.

Return valid JSON only, matching this shape: {{"verdict": "PASS"}} if
nothing misrepresents the principles, {{"verdict": "FAIL"}} if something
does. No prose outside the JSON.
"""


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    issues: list[str] = field(default_factory=list)


def _rule_based_issues(
    reflection: str, suggestions: list[dict], valid_principle_ids: set[str]
) -> list[str]:
    issues: list[str] = []

    if len(suggestions) > MAX_SUGGESTIONS:
        issues.append(f"{len(suggestions)} suggestions given, exceeds the top-{MAX_SUGGESTIONS} limit")

    for s in suggestions:
        if s["principle_id"] not in valid_principle_ids:
            issues.append(f"suggestion cites unknown principle_id {s['principle_id']!r}")
        if not s["text"].strip():
            issues.append(f"suggestion for principle_id {s['principle_id']!r} has empty text")
        if not s["explanation"].strip():
            issues.append(f"suggestion for principle_id {s['principle_id']!r} has empty explanation")

    labeled_texts = [("reflection", reflection)]
    for s in suggestions:
        labeled_texts.append((f"suggestion {s['principle_id']!r} text", s["text"]))
        labeled_texts.append((f"suggestion {s['principle_id']!r} explanation", s["explanation"]))

    for label, text in labeled_texts:
        if _FIRST_PERSON_PATTERN.search(text):
            issues.append(
                f"{label} is written in first person (uses \"I\"/\"my\"/\"me\") -- must address the user "
                'directly in second person ("you"/"your") instead'
            )
        quotes = find_quotes(text)
        if len(quotes) > MAX_QUOTES_PER_PRINCIPLE:
            issues.append(f"{label} contains {len(quotes)} quotes, exceeds {MAX_QUOTES_PER_PRINCIPLE}-quote limit")
        for q in quotes:
            if count_words(q) > MAX_QUOTE_WORDS:
                issues.append(f"{label} has a {count_words(q)}-word quote, exceeds {MAX_QUOTE_WORDS}-word limit")
    return issues


def _build_entailment_prompt(reflection: str, suggestions: list[dict], principles: list[Principle]) -> str:
    principles_block = "\n".join(f"- {p.name}: {p.summary}" for p in principles)
    suggestions_block = "\n".join(f"- {s['text']} ({s['explanation']})" for s in suggestions) or "(none)"
    return ENTAILMENT_PROMPT_TEMPLATE.format(
        principles_block=principles_block, reflection_block=reflection, suggestions_block=suggestions_block
    )


def _entailment_passes(client: LLMClient, reflection: str, suggestions: list[dict], principles: list[Principle]) -> bool:
    prompt = _build_entailment_prompt(reflection, suggestions, principles)
    raw = client.generate(prompt)
    # Fail-closed: anything that isn't an unambiguous {"verdict": "PASS"} is
    # treated as FAIL -- malformed JSON, an unexpected shape, or any verdict
    # other than exactly "PASS" -- same fail-closed stance as the ingestion
    # API key check elsewhere in this app. A plain "PASS"/"FAIL" word (no
    # JSON) used to be accepted here, but forcing the same structured-JSON
    # shape the model already has to hit for synthesis is both more reliable
    # and removes the ambiguity of substring-matching "PASS" in free text.
    # extract_json_object strips markdown fences/prose first -- Ollama's
    # format="json" mode makes that a no-op there, but GroqLLMClient has no
    # such guarantee, so a bare json.loads(raw) used to fail-closed on
    # *every* Groq response and silently force every analysis to the
    # fallback template. Same extraction app/synthesis.py's Step B parse
    # already relies on for the same reason.
    try:
        verdict = json.loads(extract_json_object(raw)).get("verdict")
    except (JSONExtractionError, json.JSONDecodeError, AttributeError):
        return False
    return isinstance(verdict, str) and verdict.strip().upper() == "PASS"


def verify_analysis(
    client: LLMClient,
    reflection: str,
    suggestions: list[dict],
    principles: list[Principle],
) -> VerificationResult:
    """Step C: rule-based checks + one LLM entailment call, kept strictly
    separate from Step B (architecture's "three independent steps" rule).
    """
    valid_ids = {p.principle_id for p in principles}
    issues = _rule_based_issues(reflection, suggestions, valid_ids)
    if issues:
        return VerificationResult(passed=False, issues=issues)

    if not _entailment_passes(client, reflection, suggestions, principles):
        return VerificationResult(passed=False, issues=["entailment check: reflection claims something not supported by the principles"])

    return VerificationResult(passed=True, issues=[])
