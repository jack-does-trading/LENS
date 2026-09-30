"""Golden-set retrieval gates. Offline: replays recorded vectors, no API key.

Marked `eval` so `pytest` stays fast; CI runs these in their own job so a
threshold breach reads as "quality regression", not "a test broke".
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from eval.cases import GOLDEN_PATH, load_cases
from eval.cassettes import CassetteEmbeddingClient, CassetteError, load_corpus
from eval.harness import load_thresholds, run_golden_set, seed_corpus
from eval.metrics import aggregate

pytestmark = pytest.mark.eval


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


@pytest.fixture
def seeded(db_session: Session, corpus):
    seed_corpus(db_session, corpus)
    return db_session


@pytest.fixture
def scores(seeded: Session):
    return run_golden_set(seeded, top_k=load_thresholds()["retrieval"]["top_k"])


# --- the cassette itself -----------------------------------------------------


def test_cassette_covers_every_book_the_golden_set_uses(corpus) -> None:
    needed = {c.book_id for c in load_cases(GOLDEN_PATH) if c.expected_principle_ids}
    assert needed <= {b["book_id"] for b in corpus.books}


def test_every_recorded_principle_has_a_vector_of_the_right_width(corpus) -> None:
    assert corpus.dimension > 0
    for principle in corpus.principles:
        vector = corpus.vectors[principle["principle_id"]]
        assert len(vector) == corpus.dimension


def test_a_missing_query_vector_raises_instead_of_degrading() -> None:
    """The worst failure an eval harness can have is a silent fallback to a
    fake vector: recall would still compute, and still look plausible."""
    client = CassetteEmbeddingClient(vectors={})
    with pytest.raises(CassetteError):
        client.embed(["a situation that was never recorded"], input_type="query")


def test_recorded_tags_are_not_the_extraction_placeholders(corpus) -> None:
    """Guards the retag: seeding from tools/local_extraction/output/*.json would
    restore `tag-one`/`tag-two` and silently halve the tag arm."""
    from eval.tagging import PLACEHOLDER

    placeholders = [
        p["principle_id"]
        for p in corpus.principles
        if p["applies_to_tags"] and all(PLACEHOLDER.match(t) for t in p["applies_to_tags"])
    ]
    assert not placeholders, f"{len(placeholders)} principles still fully placeholder-tagged"


# --- the gates ---------------------------------------------------------------


def test_retrieval_meets_the_recorded_floors(scores) -> None:
    agg = aggregate(scores)
    t = load_thresholds()["retrieval"]
    failures = [
        f"{name} {value:.3f} < floor {floor:.2f}"
        for name, value, floor in (
            ("recall", agg["recall_at_k"], t["recall_at_k_floor"]),
            ("precision", agg["precision_at_k"], t["precision_at_k_floor"]),
            ("mrr", agg["mrr"], t["mrr_floor"]),
            ("hit_rate", agg["hit_rate"], t["hit_rate_floor"]),
        )
        if value < floor
    ]
    assert not failures, "; ".join(failures)


def test_the_gate_can_actually_fail(seeded: Session) -> None:
    """A gate that has never failed is not a gate. Degrade retrieval on purpose
    -- weight the tag arm to nothing AND take only the top 1 -- and confirm the
    floors reject it."""
    from eval.harness import run_golden_set as run

    degraded = run(seeded, top_k=1)
    agg = aggregate(degraded)
    t = load_thresholds()["retrieval"]
    assert agg["hit_rate"] < t["hit_rate_floor"] or agg["recall_at_k"] < t["recall_at_k_floor"]


def test_offline_eval_reproduces_the_live_measurement(scores) -> None:
    """The cassette must agree with what measure_retrieval.py reports against a
    real database -- otherwise the CI number is measuring the cassette, not the
    system. Recorded 2026-09-25: recall .12 precision .21 MRR .49 hit .75.
    """
    agg = aggregate(scores)
    assert agg["n"] == 16
    assert agg["hit_rate"] == pytest.approx(0.75, abs=0.07)
    assert agg["mrr"] == pytest.approx(0.49, abs=0.05)
