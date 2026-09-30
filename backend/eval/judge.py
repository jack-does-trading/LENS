"""The faithfulness judge: an LLM scoring pipeline output against §6's rubric.

Architecture §6 specifies three human-rated quantities per case -- faithfulness
(1-5), a hallucination flag (yes/no), and suggestion groundedness (1-5) -- and
two independent human raters with an inter-rater agreement number. That plan
does not survive contact with a golden set that grows every time someone uses
the app: it prices every new case at two human reads, so the set stops growing
the moment the humans get bored. This module substitutes an LLM rater for the
second human, and §10's addendum records the deviation.

The substitution is only legitimate because of one thing: **the judge is itself
a model, so it is evaluated before it is trusted.** eval/calibration.py computes
Cohen's kappa between this judge's verdicts and human labels, and
eval/thresholds.json refuses to let a judge score be quoted below kappa 0.60.
A judge nobody has calibrated is not a measurement, it is a vibe with a decimal
point.

Scoring the same rubric §6 already wrote down, rather than inventing a new one,
is also deliberate: the rubric is the contract with the reader of the numbers.
Changing it silently would make every score before and after incomparable,
which is why PROMPT_VERSION below exists and is bumped by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.json_extraction import JSONExtractionError, extract_json_object
from app.llm import LLMClient
from app.models import Principle

# Bumped by hand whenever JUDGE_PROMPT changes. A score whose prompt version is
# unknown is unattributable -- if faithfulness moves from 4.1 to 3.6 you have to
# be able to tell "the pipeline got worse" from "I reworded the judge".
PROMPT_VERSION = "judge-v2"  # v2: evidence-first hallucination flag, see JUDGE_PROMPT

# The rubric's own scale, spelled out for the model. Free-form "rate 1-5"
# prompts drift toward 4 for everything; anchoring each point is what makes the
# distribution usable, and it is also what a human rater is reading, so the two
# raters are at least answering the same question.
JUDGE_PROMPT = """\
You are grading a generated reflection and its suggestions against the ONLY
source material the generator was given. You are not grading whether the advice
is wise, well-written, or kind. You are grading whether it is *supported*.

You can see exactly what the generator could see. If a claim is true in the
real world but is not in the principles below, it is still unsupported.

PRINCIPLES (the complete source material):
{principles_block}

USER'S DAY (what the reflection is about):
{entries_block}

GENERATED REFLECTION:
{reflection_block}

GENERATED SUGGESTIONS:
{suggestions_block}

Answer in this order. The order is not optional: decide the invented-item
question by finding evidence for it, BEFORE you assign any score.

1. hallucinated_item -- quote the exact phrase from the reflection or a
   suggestion's explanation that was invented or misattributed, or null if there
   is none. "Invented or misattributed" means exactly one of:
     (a) a specific fabricated fact: a statistic, study, researcher, named
         person, or quotation presented as coming from the book; or
     (b) an idea attributed to the book, or a description of what a principle
         says, that contradicts or does not appear in the principles above.
   A claim that is simply ABSENT from the principles and is NOT presented as
   the book's -- an ordinary observation about people or the user's day -- is
   unsupported, not invented. It lowers faithfulness. It is NOT a hallucination.
   If you cannot quote a phrase that meets (a) or (b), the answer is null.

2. hallucination (true/false) -- true if and only if hallucinated_item is not
   null. Do not set this from how low the faithfulness score feels.

3. faithfulness (1-5) -- does every claim in the REFLECTION trace back to a
   principle above?
   5 = every claim traces to a principle; nothing added.
   4 = all claims traceable; some phrasing goes slightly beyond the summaries.
   3 = one claim is a stretch or over-generalises a principle.
   2 = a claim is not supported by any principle above, though nothing is
       attributed to the book that is not there.
   1 = a claim contradicts a principle, or attributes something to the book
       that plainly is not there.
   Note that 2 and 1 differ by exactly the test in step 1: unsupported is 2,
   invented or misattributed is 1. A reflection with no defects at all is 5 even
   if a SUGGESTION is flawed -- suggestion problems belong to step 4.

4. suggestion_groundedness (1-5) -- is each suggestion a reasonable application
   of the principle_id it cites?
   5 = every suggestion clearly applies its cited principle.
   4 = all apply; one is a loose fit.
   3 = one suggestion does not really follow from its cited principle.
   2 = a suggestion cites a principle it has nothing to do with.
   1 = NONE of the suggestions applies the principle it cites.
   IMPORTANT: a suggestion's action text is SUPPOSED to be a concrete tactic
   that is not written verbatim in the principles. Being specific is not a
   deduction. Only the link between the suggestion and its cited principle is.

Return ONLY this JSON, no prose:
{{"hallucinated_item": <"exact phrase" or null>, "hallucination": <true|false>,
  "faithfulness": <1-5>, "suggestion_groundedness": <1-5>,
  "reason": "<one sentence, under 30 words>"}}
"""

# §6's ship gate, restated: "no case with a human-flagged hallucination,
# average faithfulness >= 4/5".
RUBRIC_FIELDS = ("faithfulness", "hallucination", "suggestion_groundedness")


class JudgeError(RuntimeError):
    """The judge returned something that cannot be read as a verdict.

    Raised rather than defaulted. A judge whose unparseable answers quietly
    become 5s reports a perfect score for a broken judge, and a judge whose
    unparseable answers quietly become 1s reports a broken pipeline. Both are
    worse than a crash, because both look like data.
    """


@dataclass(frozen=True)
class JudgeVerdict:
    faithfulness: int
    hallucination: bool
    suggestion_groundedness: int
    reason: str = ""
    raw: str = ""
    # The phrase the judge says was invented, or None. Asking for the evidence
    # before the flag is what v2 changed, and why: under v1 the flag was a
    # deterministic function of the judge's own faithfulness score (flagged iff
    # faithfulness <= 2 on all 16 calibration cases), so its kappa was
    # re-measuring faithfulness rather than independently validating the flag.
    # A flag that has to cite a phrase cannot be derived from a score.
    hallucinated_item: str | None = None

    @property
    def flag_has_evidence(self) -> bool:
        """A `true` flag with nothing quoted is an unfalsifiable accusation."""
        return (not self.hallucination) or bool(self.hallucinated_item)

    @property
    def passes_ship_gate(self) -> bool:
        return self.faithfulness >= 4 and not self.hallucination

    def as_labels(self) -> dict[str, Any]:
        """The three rubric values alone, for calibration against a human."""
        return {
            "faithfulness": self.faithfulness,
            "hallucination": self.hallucination,
            "suggestion_groundedness": self.suggestion_groundedness,
        }


@dataclass(frozen=True)
class JudgeInput:
    """Everything the judge is allowed to see about one case.

    Note what is absent: the book's full text, the expected_principle_ids, and
    whether verification passed. §6 is explicit that raters see "only the
    retrieved principle summaries (not the whole book) and the generated
    output", because a rater who can see more than the generator could will
    mark down output for omitting things it never had access to.
    """

    case_id: str
    principles: list[Principle]
    entries: list[dict[str, Any]]
    reflection: str
    suggestions: list[dict[str, Any]]
    tags: list[str] = field(default_factory=list)


def _principles_block(principles: list[Principle]) -> str:
    return "\n".join(f"- id: {p.principle_id}\n  {p.name}: {p.summary}" for p in principles) or "(none)"


def _entries_block(entries: list[dict[str, Any]]) -> str:
    return "\n".join(f"- [{e.get('category') or 'uncategorised'}] {e.get('action', '')}" for e in entries) or "(none)"


def _suggestions_block(suggestions: list[dict[str, Any]]) -> str:
    return (
        "\n".join(
            f"- cites {s.get('principle_id')!r}\n  action: {s.get('text', '')}\n"
            f"  explanation: {s.get('explanation', '')}"
            for s in suggestions
        )
        or "(none)"
    )


def build_prompt(case: JudgeInput) -> str:
    return JUDGE_PROMPT.format(
        principles_block=_principles_block(case.principles),
        entries_block=_entries_block(case.entries),
        reflection_block=case.reflection or "(empty)",
        suggestions_block=_suggestions_block(case.suggestions),
    )


def _coerce_score(payload: dict[str, Any], field_name: str) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool):  # bool is an int in Python; 1-5 it is not
        raise JudgeError(f"{field_name} came back as a boolean: {value!r}")
    if isinstance(value, str):
        value = value.strip()
    try:
        score = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise JudgeError(f"{field_name} is not an integer: {value!r}") from exc
    if not 1 <= score <= 5:
        raise JudgeError(f"{field_name}={score} is outside the 1-5 rubric")
    return score


def _coerce_flag(payload: dict[str, Any]) -> bool:
    value = payload.get("hallucination")
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "y"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "no", "n"):
        return False
    raise JudgeError(f"hallucination is not a yes/no value: {value!r}")


def parse_verdict(raw: str) -> JudgeVerdict:
    """Read a verdict out of a raw completion.

    Same extract_json_object the synthesis and entailment parsers use: Groq is
    under no obligation to omit a markdown fence, and a bare json.loads here
    would fail on every fenced response -- the exact bug that once forced every
    analysis in production to the fallback template.
    """
    try:
        payload = json.loads(extract_json_object(raw))
    except (JSONExtractionError, json.JSONDecodeError) as exc:
        raise JudgeError(f"unparseable judge response: {raw[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise JudgeError(f"judge returned {type(payload).__name__}, not an object")
    item = payload.get("hallucinated_item")
    return JudgeVerdict(
        faithfulness=_coerce_score(payload, "faithfulness"),
        hallucination=_coerce_flag(payload),
        suggestion_groundedness=_coerce_score(payload, "suggestion_groundedness"),
        reason=str(payload.get("reason", ""))[:300],
        raw=raw,
        hallucinated_item=str(item)[:300] if item not in (None, "", "null") else None,
    )


def judge_output(client: LLMClient, case: JudgeInput) -> JudgeVerdict:
    return parse_verdict(client.generate(build_prompt(case)))


def aggregate_verdicts(verdicts: dict[str, JudgeVerdict]) -> dict[str, Any]:
    """§6's ship gate as numbers.

    `hallucination_free_rate` rather than a mean: §6's gate is "no case with a
    flagged hallucination", so one bad case matters and an average over 16 would
    hide it.
    """
    if not verdicts:
        return {"n": 0}
    values = list(verdicts.values())
    n = len(values)
    return {
        "n": n,
        "mean_faithfulness": round(sum(v.faithfulness for v in values) / n, 2),
        "mean_suggestion_groundedness": round(sum(v.suggestion_groundedness for v in values) / n, 2),
        "hallucination_free_rate": round(sum(1 for v in values if not v.hallucination) / n, 3),
        "hallucinated_cases": sorted(cid for cid, v in verdicts.items() if v.hallucination),
        "ship_gate_failures": sorted(cid for cid, v in verdicts.items() if not v.passes_ship_gate),
    }
