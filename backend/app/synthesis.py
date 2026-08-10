from __future__ import annotations

import json

from app.json_extraction import JSONExtractionError, extract_json_object
from app.llm import LLMClient
from app.models import Book, Principle

SYNTHESIS_PROMPT_TEMPLATE = """\
SYSTEM:
You are an advisor analyzing a user's day and writing them a personal
reflection, grounded strictly in the following book's ideas. Tone: {book_tone}.
Address the user directly in second person ("you", "your") throughout, as
an advisor speaking to them -- never write in first person ("I", "I'm",
"my", "me") as if you were the user narrating their own day. For example,
write "Your mood today suggests you're feeling uncertain," not "My mood
today suggests I'm feeling uncertain."
You must only use the principles provided below. Do not use any other
knowledge of this book. Do not quote more than 15 words verbatim from
any single principle summary, and do not quote more than once per
principle. If a quote isn't necessary, paraphrase instead.

BOOK: {book_title} by {book_author}
CORE THESIS: {core_thesis}

RETRIEVED PRINCIPLES (use only these, cite by name and principle_id):
{principles_block}

USER'S DAY:
Mood (1-5, optional): {mood}
Logged entries:
{entries_block}

PRIOR SUGGESTIONS STATUS (yesterday, if any):
{prior_suggestions_block}

EXAMPLE (format and voice only -- a different book, different principle;
do not reuse any of its wording or reasoning):

Example logged entries: "18:20: skipped the gym again and ordered takeout instead of cooking [health]"
Example mood: 2 (Low)
Example principle available: principle_id "start-small", "Start Small" --
"Big goals fail when the first step is too large; shrink the action until
skipping it feels harder than doing it."

Example of a correctly-formatted response:
{{
  "reflection": "Skipping the gym again and reaching for takeout both point to the same pattern: you're aiming for a version of tonight that's too big to start on a low-energy day. Start Small makes the case for shrinking the action “until skipping it feels harder than doing it” -- a smaller first step your mood today could have actually cleared.",
  "suggestions": [{{"text": "Tomorrow, commit to five minutes of movement instead of a full workout.", "principle_id": "start-small", "explanation": "A five-minute version is small enough that low motivation can't derail it, per Start Small."}}]
}}

Notice throughout: every sentence addresses "you"/"your", never "I"/"my";
the one quotation used stays under 15 words; and the suggestion is a
concrete tomorrow-action, not a restatement of the reflection.

TASK: Return valid JSON only, matching this shape:
{{
  "reflection": "one paragraph (3-5 sentences) that assesses today's specific situation -- referencing the user's actual logged entries and mood, not a generic restatement -- and names which of the principles above it connects to and why",
  "suggestions": [{{"text": "one concrete tip for tomorrow", "principle_id": "id from above", "explanation": "one short sentence on why this tip follows from the principle"}}]
}}
The reflection is a single paragraph, not a list -- do not repeat the same
sentence structure for each principle. Give at most 3 suggestions (these
are the user's top 3 tips for tomorrow), each with its own explanation. A
suggestion must be a forward-looking action for tomorrow, not a
restatement of anything already said in the reflection -- if a suggestion
and the reflection would read almost identically, reword the suggestion to
be a concrete next step instead. No prose outside the JSON.
"""

def _build_retry_reminder(issues: list[str]) -> str:
    # Feeding back the *actual* verification.issues from the previous attempt
    # (rather than a generic static reminder) so a retry has something
    # concrete to correct -- an entailment rejection and a quote-limit
    # violation need different fixes, and the model can't guess which one
    # happened from a one-size-fits-all message.
    issues_block = "\n".join(f"- {issue}" for issue in issues) if issues else "- output did not pass verification"
    return (
        "\n\nREMINDER: your previous attempt failed verification for these "
        f"specific reasons:\n{issues_block}\n"
        "Fix exactly these issues in this attempt. Also, every principle_id you "
        "cite MUST be copied exactly from the quoted principle_id value above "
        '(e.g. "teacher-confidence-matters"), with no extra words or prefix -- '
        'not "id: teacher-confidence-matters". Verbatim quotes must be 15 words '
        "or fewer."
    )


class SynthesisError(RuntimeError):
    pass


def _principles_block(principles: list[Principle]) -> str:
    # Field name matches the output JSON key exactly ("principle_id", not
    # "id"), and the value is quoted -- llama3.2:3b was otherwise copying
    # the literal line prefix ("id: teacher-confidence-matters") into the
    # suggestions' principle_id field instead of just the id value, which
    # made every suggestion fail the unknown-principle_id rule check.
    return "\n".join(
        f'- principle_id: "{p.principle_id}"\n  name: {p.name}\n  summary: {p.summary}' for p in principles
    )


def _entries_block(entries: list[dict]) -> str:
    if not entries:
        return "(no entries logged)"
    return "\n".join(f"- {e.get('time', '')}: {e.get('action', '')} [{e.get('category', '')}]" for e in entries)


def _prior_suggestions_block(prior_suggestions: list[dict]) -> str:
    if not prior_suggestions:
        return "(none)"
    return "\n".join(f"- {s['text']} -> {s['status']}" for s in prior_suggestions)


def build_synthesis_prompt(
    book: Book,
    principles: list[Principle],
    entries: list[dict],
    mood: int | None,
    prior_suggestions: list[dict] | None = None,
) -> str:
    return SYNTHESIS_PROMPT_TEMPLATE.format(
        book_tone=book.tone,
        book_title=book.title,
        book_author=book.author,
        core_thesis=book.core_thesis,
        principles_block=_principles_block(principles),
        mood=mood if mood is not None else "(not logged)",
        entries_block=_entries_block(entries),
        prior_suggestions_block=_prior_suggestions_block(prior_suggestions or []),
    )


def parse_synthesis_response(raw: str) -> dict:
    try:
        payload = json.loads(extract_json_object(raw))
    except JSONExtractionError as exc:
        raise SynthesisError(str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise SynthesisError(f"model response was not valid JSON: {exc}") from exc

    reflection = payload.get("reflection")
    suggestions = payload.get("suggestions")
    if not isinstance(reflection, str) or not reflection.strip():
        raise SynthesisError("model response missing non-empty 'reflection'")
    if not isinstance(suggestions, list) or not suggestions:
        raise SynthesisError("model response missing non-empty 'suggestions' list")
    for s in suggestions:
        if not isinstance(s, dict) or "text" not in s or "principle_id" not in s or "explanation" not in s:
            raise SynthesisError(f"malformed suggestion entry: {s!r}")
        if not isinstance(s["text"], str) or not s["text"].strip():
            raise SynthesisError(f"suggestion has empty 'text': {s!r}")
        if not isinstance(s["explanation"], str) or not s["explanation"].strip():
            raise SynthesisError(f"suggestion has empty 'explanation': {s!r}")
    return {"reflection": reflection, "suggestions": suggestions}


def synthesize_analysis(
    client: LLMClient,
    book: Book,
    principles: list[Principle],
    entries: list[dict],
    mood: int | None = None,
    prior_suggestions: list[dict] | None = None,
    retry_issues: list[str] | None = None,
) -> dict:
    """Step B: one LLM call producing {reflection, suggestions}. No rule
    enforcement here -- that's Step C's job (app/verification.py), kept
    strictly separate per architecture's "three independent steps" rule.
    """
    prompt = build_synthesis_prompt(book, principles, entries, mood, prior_suggestions)
    if retry_issues is not None:
        prompt += _build_retry_reminder(retry_issues)
    raw = client.generate(prompt)
    return parse_synthesis_response(raw)


_FALLBACK_EXPLANATION = (
    "Shown directly from the book's own reviewed text -- a generated "
    "explanation couldn't be verified today."
)


def _join_names(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def fallback_analysis(principles: list[Principle]) -> dict:
    """Non-LLM fallback used when synthesis fails verification twice
    (architecture SS3 Step C: "fallback to template displaying principle
    text directly"). Just surfaces the retrieved principles verbatim --
    already human-reviewed, length-capped text -- with no model call and
    therefore nothing left to verify.

    Reflection only names which book ideas were retrieved for today (no
    per-day assessment -- that needs an LLM); the summary text itself, the
    actual grounded content, only appears once, in the matching suggestion,
    not duplicated here.
    """
    reflection = (
        f"Based on what you logged today, these ideas from the book connect "
        f"to your situation: {_join_names([p.name for p in principles])}. See "
        "the suggestions below for what the book says about each."
    )
    suggestions = [
        {"text": p.summary, "principle_id": p.principle_id, "explanation": _FALLBACK_EXPLANATION}
        for p in principles[:3]
    ]
    return {"reflection": reflection, "suggestions": suggestions}
