"""Retrieval metrics over the golden set.

A note on recall@k that matters for reading any of these numbers: when a case's
expected set is larger than k, recall@k cannot reach 1.0 by construction. With
top_k=3 and 8 expected principles the ceiling is 0.375. Reporting recall against
a ceiling of 1.0 in that situation understates the retriever and hides the real
question, so `recall_at_k` is always reported next to `recall_ceiling`.

`precision_at_k` has no such problem and is the cleanest single number while
top_k is 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    expected: int
    returned: int
    hits: int
    recall_at_k: float
    recall_ceiling: float
    precision_at_k: float
    reciprocal_rank: float

    @property
    def any_relevant(self) -> bool:
        return self.hits > 0


def score_case(case_id: str, expected: list[str], retrieved: list[str], k: int) -> CaseScore:
    exp = set(expected)
    top = retrieved[:k]
    hits = sum(1 for pid in top if pid in exp)
    rr = 0.0
    for rank, pid in enumerate(top, start=1):
        if pid in exp:
            rr = 1.0 / rank
            break
    return CaseScore(
        case_id=case_id,
        expected=len(exp),
        returned=len(top),
        hits=hits,
        recall_at_k=hits / len(exp) if exp else 0.0,
        # The best any retriever could do for this case at this k.
        recall_ceiling=min(len(exp), k) / len(exp) if exp else 0.0,
        precision_at_k=hits / len(top) if top else 0.0,
        reciprocal_rank=rr,
    )


def aggregate(scores: list[CaseScore]) -> dict[str, float | int]:
    if not scores:
        return {"n": 0}
    return {
        "n": len(scores),
        "recall_at_k": round(mean(s.recall_at_k for s in scores), 3),
        "recall_ceiling": round(mean(s.recall_ceiling for s in scores), 3),
        # Recall as a fraction of what was actually achievable at this k.
        "recall_vs_ceiling": round(
            mean(s.recall_at_k / s.recall_ceiling for s in scores if s.recall_ceiling), 3
        ),
        "precision_at_k": round(mean(s.precision_at_k for s in scores), 3),
        "mrr": round(mean(s.reciprocal_rank for s in scores), 3),
        # The bluntest question: did the user see anything relevant at all?
        "hit_rate": round(mean(1.0 if s.any_relevant else 0.0 for s in scores), 3),
    }


def format_report(scores: list[CaseScore], agg: dict, k: int, source: str) -> str:
    lines = [
        f"retrieval @ k={k}   source: {source}",
        "",
        f"  {'case':18} {'exp':>4} {'hit':>4} {'recall':>7} {'ceil':>6} {'prec':>6} {'RR':>5}",
        "  " + "-" * 56,
    ]
    for s in sorted(scores, key=lambda s: s.case_id):
        lines.append(
            f"  {s.case_id:18} {s.expected:4} {s.hits:4} {s.recall_at_k:7.2f} "
            f"{s.recall_ceiling:6.2f} {s.precision_at_k:6.2f} {s.reciprocal_rank:5.2f}"
        )
    lines += [
        "",
        f"  n = {agg['n']}   (small; treat every figure below as directional)",
        f"  recall@{k}        {agg['recall_at_k']:.2f}   ceiling {agg['recall_ceiling']:.2f}"
        f"   -> {agg['recall_vs_ceiling']:.0%} of achievable",
        f"  precision@{k}     {agg['precision_at_k']:.2f}",
        f"  MRR             {agg['mrr']:.2f}",
        f"  hit rate        {agg['hit_rate']:.0%}  (cases returning at least one relevant principle)",
    ]
    return "\n".join(lines)
