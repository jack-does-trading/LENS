"""The golden set: real situations with a human-decided expected principle set.

Why real situations rather than invented ones: the whole argument for evals is
that you have to look at what your system actually sees. A synthetic case tests
the pipeline against your imagination of a user; a mined one tests it against a
user. The cost is that the set starts small and grows only as the app is used,
which is why every report prints `n` next to every metric.

The two id lists are kept strictly apart:

  * `retrieved_principle_ids` -- what the system *did* return at export time.
  * `expected_principle_ids`  -- what a human says it *should* have returned.

Conflating them would have the harness grade the system against its own past
output, which always scores perfectly and measures nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

CASES_DIR = Path(__file__).parent / "cases"
# Committed. Only human-reviewed cases ever land here.
GOLDEN_PATH = CASES_DIR / "golden.jsonl"
# Gitignored staging area written by scripts/export_eval_cases.py. Holds raw,
# unreviewed journal text straight out of the database, so it must never be
# committed -- the labelling pass is what moves a case into GOLDEN_PATH.
STAGING_PATH = CASES_DIR / "staging.jsonl"


class CaseError(ValueError):
    """A case failed validation on load."""


@dataclass
class EvalCase:
    case_id: str
    # sha256 of the source daily_log's id: lets a re-export skip cases that
    # are already labelled, without storing the id itself.
    source_log_id_hash: str
    book_id: str
    entries: list[dict[str, Any]]
    mood: int | None
    retrieved_at_export: list[str]
    expected_principle_ids: list[str] = field(default_factory=list)
    label_notes: str = ""
    labelled_by: str | None = None
    labelled_at: str | None = None
    # "verbatim" -- the entries are as the user wrote them.
    # "redacted"  -- the action text was paraphrased during review.
    redaction: str = "verbatim"

    @property
    def is_labelled(self) -> bool:
        return self.labelled_by is not None and bool(self.expected_principle_ids)

    def validate(self) -> None:
        if not self.book_id:
            raise CaseError(f"{self.case_id}: missing book_id")
        if not self.entries:
            raise CaseError(f"{self.case_id}: a case with no entries cannot exercise retrieval")
        for e in self.entries:
            if "category" not in e or "action" not in e:
                raise CaseError(f"{self.case_id}: entry missing category/action: {e!r}")
        if self.mood is not None and not 1 <= self.mood <= 5:
            raise CaseError(f"{self.case_id}: mood {self.mood} outside 1-5")
        if len(set(self.expected_principle_ids)) != len(self.expected_principle_ids):
            raise CaseError(f"{self.case_id}: duplicate ids in expected_principle_ids")
        if self.redaction not in ("verbatim", "redacted"):
            raise CaseError(f"{self.case_id}: unknown redaction {self.redaction!r}")


def hash_log_id(log_id: Any) -> str:
    return "sha256:" + hashlib.sha256(str(log_id).encode("utf-8")).hexdigest()


def case_id_for(log_id_hash: str) -> str:
    """Stable across re-exports, so labelling is never invalidated by one."""
    return "case-" + log_id_hash.removeprefix("sha256:")[:10]


def load_cases(path: Path, *, labelled_only: bool = False) -> list[EvalCase]:
    if not path.exists():
        return []
    cases: list[EvalCase] = []
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            case = EvalCase(**json.loads(line))
        except (json.JSONDecodeError, TypeError) as exc:
            raise CaseError(f"{path}:{line_no}: {exc}") from exc
        case.validate()
        if labelled_only and not case.is_labelled:
            continue
        cases.append(case)
    return cases


def save_cases(path: Path, cases: list[EvalCase]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Sorted by case_id so a re-export produces a reviewable diff rather than a
    # reshuffled file.
    lines = [json.dumps(asdict(c), sort_keys=True) for c in sorted(cases, key=lambda c: c.case_id)]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def iter_golden() -> Iterator[EvalCase]:
    yield from load_cases(GOLDEN_PATH, labelled_only=True)
