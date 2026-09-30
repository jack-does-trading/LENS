"""The judge's own calibration set: outputs with human rubric labels attached.

Separate from eval/adversarial/ on purpose. The adversarial set asks a binary
question -- did the verifier refuse this? -- and its cases are authored to trip
one specific check. Calibrating a 1-5 rubric needs something the adversarial set
structurally cannot give: **cases that land in the middle of the scale.** A set
made only of "perfect" and "catastrophic" makes kappa on faithfulness a
near-binary agreement score, and a judge that can only tell 5 from 1 will pass
it while being useless on the 3s and 4s that real output actually produces.

So every case here is authored to sit at a named rubric point, and the human
label *is the specification of the case* rather than a later judgement of it.
That matters for calibration validity: the labels were fixed before the judge
ever ran, so they cannot have been fitted to its answers.

The set runs against the same small FIXTURE_PRINCIPLES as the adversarial set,
which means no database, no retrieval, and no coupling to whichever books happen
to be published. It also means the judge sees exactly the source material a
human rater sees, which is the condition Architecture section 6 imposes on its
raters.

n is 16. That is small, and every report says so. The honest reading of a kappa
at n=16 is "this judge is not obviously broken", not "this judge is validated".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.models import Principle
from eval.adversarial.cases import FIXTURE_PRINCIPLES, fixture_principles

CASES_PATH = Path(__file__).parent / "labels" / "judge_calibration.jsonl"

VALID_IDS = {p["principle_id"] for p in FIXTURE_PRINCIPLES}
RUBRIC_FIELDS = ("faithfulness", "hallucination", "suggestion_groundedness")


class JudgeCaseError(ValueError):
    pass


@dataclass
class JudgeCase:
    case_id: str
    # What the case is built to test, and why the labels below are what they are.
    why: str
    entries: list[dict[str, Any]]
    reflection: str
    suggestions: list[dict[str, Any]]
    # The human rater's scores. Authored with the case, never edited to match
    # the judge -- see the module docstring.
    human: dict[str, Any] = field(default_factory=dict)
    labelled_by: str = "bhavyadeep"

    def validate(self) -> None:
        for name in RUBRIC_FIELDS:
            if name not in self.human:
                raise JudgeCaseError(f"{self.case_id}: human label missing {name}")
        if not isinstance(self.human["hallucination"], bool):
            raise JudgeCaseError(f"{self.case_id}: hallucination label must be a bool")
        for name in ("faithfulness", "suggestion_groundedness"):
            value = self.human[name]
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
                raise JudgeCaseError(f"{self.case_id}: {name}={value!r} outside the 1-5 rubric")
        if not self.suggestions:
            raise JudgeCaseError(f"{self.case_id}: no suggestions to grade")
        for s in self.suggestions:
            if s["principle_id"] not in VALID_IDS:
                raise JudgeCaseError(
                    f"{self.case_id}: cites {s['principle_id']!r}, which is not in the fixture set. "
                    "An invalid id is a rule-check case, not a judge case -- the verifier would "
                    "reject it before the judge ever saw it."
                )


def load(path: Path = CASES_PATH) -> list[JudgeCase]:
    cases: list[JudgeCase] = []
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            case = JudgeCase(**json.loads(line))
        except (json.JSONDecodeError, TypeError) as exc:
            raise JudgeCaseError(f"{path}:{line_no}: {exc}") from exc
        case.validate()
        cases.append(case)
    return cases


def human_labels(cases: list[JudgeCase]) -> dict[str, dict[str, Any]]:
    return {c.case_id: dict(c.human) for c in cases}


def to_judge_input(case: JudgeCase, principles: list[Principle] | None = None):
    from eval.judge import JudgeInput

    return JudgeInput(
        case_id=case.case_id,
        principles=principles if principles is not None else fixture_principles(),
        entries=case.entries,
        reflection=case.reflection,
        suggestions=case.suggestions,
    )
