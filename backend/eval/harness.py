"""Run the golden set against a cassette-seeded database. No network, no API key.

The seam this relies on: retrieve_principles() takes a Session and an
EmbeddingClient, and app.embeddings.EmbeddingClient is a typing.Protocol. So a
replay client needs no inheritance and no monkeypatching, and the code under
test is the same code production runs -- not a reimplementation of it, which
would be an eval that grades a copy of the system rather than the system.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models import Book, BookTone, Principle, ReviewStatus
from app.retrieval import DEFAULT_TOP_K, LogEntryLike, retrieve_principles
from eval.cases import GOLDEN_PATH, load_cases
from eval.cassettes import CassetteEmbeddingClient, Corpus, load_corpus
from eval.metrics import CaseScore, aggregate, score_case

THRESHOLDS_PATH = Path(__file__).resolve().parent / "thresholds.json"


def load_thresholds() -> dict[str, Any]:
    return json.loads(THRESHOLDS_PATH.read_text())


def seed_corpus(db: Session, corpus: Corpus) -> int:
    """Insert the recorded books and principles, embeddings included.

    review_status is forced to human_reviewed: only recorded principles that
    were already published are in the cassette, and retrieve_principles filters
    on it, so leaving it at the model default would make every case return
    nothing and every metric read 0.0 for a reason unrelated to retrieval.
    """
    for book in corpus.books:
        db.add(
            Book(
                book_id=book["book_id"],
                title=book["title"],
                author=book["author"],
                core_thesis=book["core_thesis"],
                tone=BookTone(book["tone"]),
                tracked_metrics=book["tracked_metrics"],
                review_status=ReviewStatus.human_reviewed,
            )
        )
    db.flush()
    for principle in corpus.principles:
        db.add(
            Principle(
                principle_id=principle["principle_id"],
                book_id=principle["book_id"],
                name=principle["name"],
                summary=principle["summary"],
                source_chapter=principle["source_chapter"],
                applies_to_tags=principle["applies_to_tags"],
                embedding=corpus.vectors[principle["principle_id"]],
                review_status=ReviewStatus.human_reviewed,
            )
        )
    db.flush()
    return len(corpus.principles)


def run_golden_set(db: Session, top_k: int = DEFAULT_TOP_K) -> list[CaseScore]:
    """Score every labelled case. Requires seed_corpus() to have run on `db`."""
    client = CassetteEmbeddingClient()
    scores = []
    for case in load_cases(GOLDEN_PATH):
        if not case.expected_principle_ids:
            continue
        entries = [
            LogEntryLike(category=e.get("category", ""), action=e.get("action", ""))
            for e in case.entries
        ]
        retrieved = retrieve_principles(
            db, case.book_id, entries, client, mood=case.mood, top_k=top_k
        )
        scores.append(
            score_case(case.case_id, case.expected_principle_ids, retrieved, top_k)
        )
    return scores


def report(scores: list[CaseScore], top_k: int) -> str:
    """Markdown, for the CI artifact and the PR comment."""
    agg = aggregate(scores)
    thresholds = load_thresholds()["retrieval"]
    rows = [
        ("recall@%d" % top_k, agg["recall_at_k"], thresholds["recall_at_k_floor"]),
        ("precision@%d" % top_k, agg["precision_at_k"], thresholds["precision_at_k_floor"]),
        ("MRR", agg["mrr"], thresholds["mrr_floor"]),
        ("hit rate", agg["hit_rate"], thresholds["hit_rate_floor"]),
    ]
    lines = [
        f"### Retrieval eval — n={agg['n']}, k={top_k}",
        "",
        "| metric | value | floor | |",
        "|---|---|---|---|",
    ]
    for name, value, floor in rows:
        lines.append(f"| {name} | {value:.3f} | {floor:.2f} | {'✅' if value >= floor else '❌'} |")
    lines += [
        "",
        f"recall ceiling at this k is {agg['recall_ceiling']:.2f} "
        f"({agg['recall_vs_ceiling']:.0%} of achievable); "
        f"§6 target is {thresholds['recall_at_k_target']:.2f}.",
        "",
        f"<sub>n={agg['n']}. Every figure is directional; "
        f"±{(0.25 / agg['n']) ** 0.5 * 100:.0f}pp standard error on a rate.</sub>",
    ]
    return "\n".join(lines)
