"""Cohen's kappa between the LLM judge and a human rater.

This is the load-bearing piece of the whole harness. Everything else measures
the pipeline; this measures the *instrument*. An uncalibrated LLM judge produces
numbers with the shape of measurements and none of the content, and the failure
is invisible -- a judge that returns 5 for everything reports a flawless system
and never errors.

Why kappa and not raw agreement: the judge and the human agree 88% of the time
on the hallucination flag mostly because hallucinations are rare. A judge that
answered "no hallucination" unconditionally would score 81% agreement on this
set while detecting nothing. Kappa subtracts the agreement you would get from
two raters guessing with the same marginals, so the degenerate judge scores 0.

    kappa = (p_observed - p_expected) / (1 - p_expected)

Two variants, because the rubric has two kinds of field:

  * `cohens_kappa`         -- nominal. Correct for the hallucination flag,
                              where "true" and "false" are just different.
  * `weighted_kappa` (quadratic) -- ordinal. Correct for faithfulness and
                              suggestion_groundedness, where 4-vs-5 is a near
                              miss and 1-vs-5 is a total disagreement.
                              Unweighted kappa treats those two as equally
                              wrong, which on a 1-5 scale understates a judge
                              that is consistently off by one.

Implemented in ~60 lines of stdlib rather than importing scikit-learn, which is
what the plan originally called for. sklearn.metrics.cohen_kappa_score would
save these lines and cost a numpy+scipy+joblib install in CI -- for one formula,
in a backend whose stated convention is stdlib-only outbound HTTP. The formula
is also short enough to be *reviewed*, which matters more than usual here: this
is the number that licenses every other number.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Hashable, Sequence

# Landis & Koch's conventional reading of kappa, reproduced so a report can name
# the band instead of leaving the reader to guess whether 0.41 is good.
_BANDS: list[tuple[float, str]] = [
    (0.81, "almost perfect"),
    (0.61, "substantial"),
    (0.41, "moderate"),
    (0.21, "fair"),
    (0.01, "slight"),
    (-1.0, "none / worse than chance"),
]


def interpret(kappa: float) -> str:
    for floor, label in _BANDS:
        if kappa >= floor:
            return label
    return "none / worse than chance"


class CalibrationError(ValueError):
    pass


def _check(a: Sequence[Any], b: Sequence[Any]) -> None:
    if len(a) != len(b):
        raise CalibrationError(f"rater lengths differ: {len(a)} vs {len(b)}")
    if not a:
        raise CalibrationError("no paired ratings to compare")


def cohens_kappa(human: Sequence[Hashable], judge: Sequence[Hashable]) -> float:
    """Unweighted (nominal) kappa. Use for the hallucination flag."""
    _check(human, judge)
    n = len(human)
    observed = sum(1 for h, j in zip(human, judge) if h == j) / n
    h_counts, j_counts = Counter(human), Counter(judge)
    expected = sum(
        (h_counts[label] / n) * (j_counts[label] / n) for label in set(h_counts) | set(j_counts)
    )
    if expected == 1.0:
        # Both raters used exactly one label, and the same one. Agreement is
        # total and chance agreement is also total, so kappa is 0/0. Returning
        # 1.0 would claim a calibrated judge on evidence that contains no
        # information; returning 0.0 would claim disagreement that did not
        # happen. This is genuinely undefined and says so.
        raise CalibrationError(
            "kappa is undefined: both raters used a single identical label, so "
            "there is no variance to agree about. Label a case where they differ."
        )
    return (observed - expected) / (1 - expected)


def weighted_kappa(human: Sequence[int], judge: Sequence[int], *, scale: int = 5) -> float:
    """Quadratic-weighted kappa. Use for the 1-5 ordinal rubric fields.

    Disagreements are penalised by (h - j)^2 / (scale - 1)^2, so adjacent
    ratings cost very little and opposite ends of the scale cost everything.
    """
    _check(human, judge)
    if scale < 2:
        raise CalibrationError("scale must be at least 2")
    n = len(human)
    labels = list(range(1, scale + 1))
    for value in list(human) + list(judge):
        if value not in labels:
            raise CalibrationError(f"rating {value!r} is outside 1..{scale}")

    def w(x: int, y: int) -> float:
        return ((x - y) ** 2) / ((scale - 1) ** 2)

    observed = sum(w(h, j) for h, j in zip(human, judge)) / n
    h_counts, j_counts = Counter(human), Counter(judge)
    expected = sum(
        w(x, y) * (h_counts[x] / n) * (j_counts[y] / n) for x in labels for y in labels
    )
    if expected == 0:
        raise CalibrationError(
            "weighted kappa is undefined: one rater gave a single value to every "
            "case, so expected disagreement is zero."
        )
    # Note the inversion relative to cohens_kappa: these are *disagreement*
    # terms, so a perfect rater has observed == 0 and kappa 1.0.
    return 1.0 - (observed / expected)


def confusion_matrix(
    human: Sequence[Hashable], judge: Sequence[Hashable], labels: Sequence[Hashable] | None = None
) -> tuple[list[Hashable], list[list[int]]]:
    """Rows = human, columns = judge. The thing you actually read when kappa is
    bad: it says *which way* the judge is wrong, and "the judge never flags
    anything" and "the judge flags everything" are opposite fixes."""
    _check(human, judge)
    if labels is None:
        labels = sorted(set(human) | set(judge), key=repr)
    index = {label: i for i, label in enumerate(labels)}
    matrix = [[0] * len(labels) for _ in labels]
    for h, j in zip(human, judge):
        matrix[index[h]][index[j]] += 1
    return list(labels), matrix


def format_confusion(labels: Sequence[Hashable], matrix: list[list[int]]) -> str:
    width = max(6, max(len(str(label)) for label in labels) + 1)
    header = " " * (width + 8) + "judge"
    cols = "human".ljust(width + 8) + "".join(str(label).rjust(width) for label in labels)
    lines = [header, cols]
    for label, row in zip(labels, matrix):
        lines.append(str(label).ljust(width + 8) + "".join(str(v).rjust(width) for v in row))
    return "\n".join(lines)


@dataclass(frozen=True)
class FieldAgreement:
    field: str
    n: int
    kind: str  # "nominal" or "ordinal"
    kappa: float | None
    raw_agreement: float
    note: str = ""

    @property
    def band(self) -> str:
        return interpret(self.kappa) if self.kappa is not None else "undefined"


def agreement(
    field: str, human: Sequence[Any], judge: Sequence[Any], *, kind: str, scale: int = 5
) -> FieldAgreement:
    """Kappa for one rubric field, with the undefined case reported rather than
    raised -- a report that dies because one field was unanimous is a worse
    outcome than a report that says so and prints the other two."""
    _check(human, judge)
    raw = sum(1 for h, j in zip(human, judge) if h == j) / len(human)
    try:
        value = (
            weighted_kappa(human, judge, scale=scale)
            if kind == "ordinal"
            else cohens_kappa(human, judge)
        )
        note = ""
    except CalibrationError as exc:
        value, note = None, str(exc)
    return FieldAgreement(
        field=field, n=len(human), kind=kind, kappa=value, raw_agreement=raw, note=note
    )


def calibrate(
    human_labels: dict[str, dict[str, Any]], judge_labels: dict[str, dict[str, Any]]
) -> list[FieldAgreement]:
    """Pair up two label sets by case_id and score every rubric field.

    Only cases present in *both* are used, and the count is reported, so a
    partially-labelled set produces a smaller honest n rather than a silent
    substitution of defaults for the missing half.
    """
    shared = sorted(set(human_labels) & set(judge_labels))
    if not shared:
        raise CalibrationError(
            f"no overlap between {len(human_labels)} human-labelled and "
            f"{len(judge_labels)} judge-labelled cases"
        )
    specs = [
        ("faithfulness", "ordinal"),
        ("hallucination", "nominal"),
        ("suggestion_groundedness", "ordinal"),
    ]
    results = []
    for field, kind in specs:
        pairs = [
            (human_labels[c][field], judge_labels[c][field])
            for c in shared
            if field in human_labels[c] and field in judge_labels[c]
        ]
        if not pairs:
            continue
        results.append(
            agreement(field, [p[0] for p in pairs], [p[1] for p in pairs], kind=kind)
        )
    return results


def report(results: list[FieldAgreement], *, kappa_floor: float) -> str:
    """Markdown. Printed at the top of every eval report, above the scores it
    licenses -- a faithfulness number quoted without the kappa that justifies
    it is the failure mode this whole module exists to prevent."""
    lines = [
        "### Judge calibration (Cohen's κ vs human labels)",
        "",
        "| rubric field | n | κ | agreement | reading |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        kappa = "—" if r.kappa is None else f"{r.kappa:.2f}"
        mark = "" if r.kappa is None else (" ✅" if r.kappa >= kappa_floor else " ❌")
        lines.append(
            f"| {r.field} ({r.kind}) | {r.n} | {kappa}{mark} | {r.raw_agreement:.0%} | {r.band} |"
        )
    lines += [
        "",
        f"κ floor is {kappa_floor:.2f}. Ordinal fields use quadratic weighting; "
        "the flag uses nominal κ.",
    ]
    notes = [r.note for r in results if r.note]
    if notes:
        lines += ["", *(f"> {n}" for n in notes)]
    if not results:
        lines += ["", "> No paired labels yet — judge scores are **uncalibrated** and must not be quoted."]
    return "\n".join(lines)
