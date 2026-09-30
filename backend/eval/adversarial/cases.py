"""Loading and running the seeded bad outputs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.models import Principle
from app.verification import _rule_based_issues
from app.telemetry import classify_issue

HERE = Path(__file__).parent
RULES_PATH = HERE / "rules.jsonl"
ENTAILMENT_PATH = HERE / "entailment.jsonl"

# A small, fixed corpus. Real principle text would tie these cases to whichever
# books happen to be published, and an eval set that changes when someone
# ingests a book measures two things at once.
FIXTURE_PRINCIPLES: list[dict[str, Any]] = [
    {
        "principle_id": "identity-habits",
        "name": "Identity-based habits",
        "summary": (
            "Habits last when they reflect who you believe you are rather than a result you want. "
            "Each repetition is a vote for that identity."
        ),
        "applies_to_tags": ["habit-formation", "self-image"],
    },
    {
        "principle_id": "design-environment",
        "name": "Design your environment",
        "summary": (
            "Make the behaviour you want obvious and easy, and the behaviour you do not want "
            "invisible and hard. Willpower is a worse lever than surroundings."
        ),
        "applies_to_tags": ["environment", "habit-formation"],
    },
    {
        "principle_id": "start-small",
        "name": "Start absurdly small",
        "summary": (
            "A version of the habit too small to fail builds the consistency that a larger "
            "version depends on. Scale comes after the streak, not before it."
        ),
        "applies_to_tags": ["habit-formation", "consistency"],
    },
]


def fixture_principles() -> list[Principle]:
    """Unsaved ORM objects -- verification never touches the database."""
    return [Principle(book_id="fixture-book", **p) for p in FIXTURE_PRINCIPLES]


@dataclass
class AdversarialCase:
    case_id: str
    # "reject" = the verifier must refuse this output. "accept" = it must not.
    expect: str
    why: str
    reflection: str
    suggestions: list[dict[str, Any]]
    # Which rule should fire, as classify_issue names it. None for accept cases.
    expect_rule: str | None = None
    # A gap we know about and have chosen not to fix yet. Reported separately
    # rather than silently dropped, so it can't quietly become permanent.
    known_gap: bool = False
    extra_valid_ids: list[str] = field(default_factory=list)

    def valid_ids(self) -> set[str]:
        return {p["principle_id"] for p in FIXTURE_PRINCIPLES} | set(self.extra_valid_ids)


def load(path: Path) -> list[AdversarialCase]:
    cases: list[AdversarialCase] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(AdversarialCase(**json.loads(line)))
    return cases


@dataclass
class RuleOutcome:
    case: AdversarialCase
    issues: list[str]
    rules_fired: set[str]

    @property
    def rejected(self) -> bool:
        return bool(self.issues)

    @property
    def correct(self) -> bool:
        if self.case.expect == "accept":
            return not self.rejected
        # Rejecting for the wrong reason is not a pass: it would mean the case
        # is testing a different rule than it claims to, and the rule it names
        # could rot undetected.
        return self.rejected and (
            self.case.expect_rule is None or self.case.expect_rule in self.rules_fired
        )


def run_rule_cases(cases: list[AdversarialCase]) -> list[RuleOutcome]:
    principles = fixture_principles()
    outcomes = []
    for case in cases:
        issues = _rule_based_issues(case.reflection, case.suggestions, case.valid_ids())
        outcomes.append(
            RuleOutcome(case=case, issues=issues, rules_fired={classify_issue(i) for i in issues})
        )
    return outcomes


def catch_rate(outcomes: list[RuleOutcome]) -> tuple[int, int]:
    """(caught, total) over the cases that are supposed to be rejected."""
    rejects = [o for o in outcomes if o.case.expect == "reject" and not o.case.known_gap]
    return sum(1 for o in rejects if o.correct), len(rejects)


def false_positive_rate(outcomes: list[RuleOutcome]) -> tuple[int, int]:
    """(wrongly rejected, total) over the cases that are supposed to pass.

    Matters more than it sounds: a false positive burns a synthesis retry, and
    five of them hand the user the fallback template.
    """
    accepts = [o for o in outcomes if o.case.expect == "accept"]
    return sum(1 for o in accepts if o.rejected), len(accepts)


# --- entailment half (needs a real LLM) ------------------------------------


@dataclass
class EntailmentOutcome:
    case: AdversarialCase
    passed_verification: bool
    issues: list[str]

    @property
    def correct(self) -> bool:
        return self.passed_verification == (self.case.expect == "accept")

    @property
    def reached_the_llm(self) -> bool:
        """False means a rule caught it first, so this case proves nothing
        about the entailment judge -- the result is not evidence either way.
        """
        return not any(classify_issue(i) != "entailment_failed" for i in self.issues)


def run_entailment_cases(client, cases: list[AdversarialCase]) -> list[EntailmentOutcome]:
    from app.verification import verify_analysis

    principles = fixture_principles()
    outcomes = []
    for case in cases:
        result = verify_analysis(client, case.reflection, case.suggestions, principles)
        outcomes.append(
            EntailmentOutcome(case=case, passed_verification=result.passed, issues=result.issues)
        )
    return outcomes


def summarise(outcomes: list[EntailmentOutcome]) -> dict[str, Any]:
    rejects = [o for o in outcomes if o.case.expect == "reject"]
    accepts = [o for o in outcomes if o.case.expect == "accept"]
    return {
        "caught": sum(1 for o in rejects if o.correct),
        "seeded": len(rejects),
        "catch_rate": (sum(1 for o in rejects if o.correct) / len(rejects)) if rejects else None,
        "false_positives": sum(1 for o in accepts if not o.correct),
        "clean_cases": len(accepts),
        "short_circuited": [o.case.case_id for o in outcomes if not o.reached_the_llm],
    }
