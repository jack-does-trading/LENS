from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from app.embeddings import EmbeddingClient
from app.models import Principle, ReviewStatus

DEFAULT_TOP_K = 5
DEFAULT_EMBEDDING_CANDIDATES = 25
# Multiplier on the tag arm's RRF contribution -- NOT on a raw score, see
# rank_fusion. Chosen by measurement, not by argument: scripts/measure_retrieval.py
# --sweep over 16 labelled cases x 3 values of top_k x 3 candidate limits x 5
# weights puts 0.5 ahead of 1.0, 2.0 and 4.0 on recall, precision, MRR and hit
# rate at *every* (top_k, candidates) pair, and ahead of 0.0 as well -- so the
# tag arm earns its place, just far more quietly than it used to.
#
# Why 0.5 specifically, mechanically: every tag-matched principle shares RRF
# rank 1 (a flat point per category, so competition ranking ties them all), and
# so contributes w/61. The embedding arm spans 1/61 = .0164 at rank 1 down to
# 1/85 = .0118 at rank 25, a range of .0046. At w >= 1.0 the tag bonus exceeds
# that entire range and tag membership decides the result outright, which is
# the failure this replaced. At 0.5 the bonus is .0082 -- worth roughly a
# 20-rank jump: decisive when the embedding arm is undecided, overridable when
# it is confident.
#
# Note this reads as *lower* than the embedding arm's implicit 1.0, where
# architecture SS3 Step A #3 says tag hits are "weighted higher, since they're
# precise". Deviation recorded deliberately; the measurement is the reason.
TAG_MATCH_WEIGHT = 0.5
# Reciprocal Rank Fusion damping constant. 60 is the value from Cormack et al.
# (2009) and the de facto default; it is large relative to top_k, which makes
# the contribution curve nearly flat across the ranks that matter and stops
# rank 1 from swamping ranks 2-5.
RRF_K = 60


@dataclass(frozen=True)
class LogEntryLike:
    category: str
    action: str = ""


def tag_match_scores(entries: list[LogEntryLike], principles: list[Principle]) -> dict[str, float]:
    """Deterministic tag-match scoring (architecture SS3 Step A #1). A
    principle scores one point per distinct logged category it shares a tag
    with. Pure function over plain inputs -- no DB or LLM call -- so it's
    testable with fixed inputs and expected outputs on its own.
    """
    categories = {e.category.strip().lower() for e in entries if e.category.strip()}
    scores: dict[str, float] = {}
    for principle in principles:
        tags = {t.strip().lower() for t in principle.applies_to_tags}
        overlap = len(categories & tags)
        if overlap:
            scores[principle.principle_id] = float(overlap)
    return scores


def _competition_ranks(scores: dict[str, float]) -> dict[str, int]:
    """Rank ids by descending score, giving equal scores the same rank
    (1, 1, 1, 4, ...) rather than an arbitrary order among them.

    This matters more than it looks. tag_match_scores awards one flat point
    per matched category, so a broad category like "relationships" ties 18
    principles at exactly 1.0. Ordering those 18 by anything -- insertion
    order, principle_id -- invents a ranking out of nothing, and RRF would
    then faithfully reward whichever happened to sort first. Tied entries must
    contribute identically so that the *other* arm decides their order.
    """
    ranks: dict[str, int] = {}
    previous_score: float | None = None
    rank = 0
    for position, (principle_id, score) in enumerate(
        sorted(scores.items(), key=lambda item: (-item[1], item[0])), start=1
    ):
        if previous_score is None or score != previous_score:
            rank = position
            previous_score = score
        ranks[principle_id] = rank
    return ranks


def rank_fusion(
    tag_scores: dict[str, float],
    embedding_scores: dict[str, float],
    top_k: int = DEFAULT_TOP_K,
    tag_weight: float = TAG_MATCH_WEIGHT,
    rrf_k: int = RRF_K,
) -> list[str]:
    """Combine tag-match and embedding-similarity scores into one ranked list
    of principle_ids (architecture SS3 Step A #3). Pure function over plain
    dicts, independent of any LLM behavior.

    Fuses by Reciprocal Rank Fusion -- each arm contributes 1/(rrf_k + rank)
    -- rather than by adding raw scores, because the two arms are not on a
    common scale. Measured on the golden set: a tag hit scores a flat 2.0
    while cosine similarity for a real query spans 0.333..0.378, a range of
    0.045. Summing those means the tag arm decides *membership* outright and
    cosine only jitters the order within it, using differences indistinguish-
    able from noise. That was worth 2 relevant results out of 4 golden cases
    against 4 before the corpus was retagged -- the fusion, not the corpus,
    was the regression. RRF discards magnitudes and keeps only ordering, so
    neither arm can dominate by virtue of its units.
    """
    combined: dict[str, float] = {}
    for principle_id, rank in _competition_ranks(tag_scores).items():
        combined[principle_id] = combined.get(principle_id, 0.0) + tag_weight / (rrf_k + rank)
    for principle_id, rank in _competition_ranks(embedding_scores).items():
        combined[principle_id] = combined.get(principle_id, 0.0) + 1.0 / (rrf_k + rank)
    # Sort on (-score, principle_id), not score alone: Python's sort is stable
    # over dict insertion order, which here is DB row order, so ties would
    # otherwise resolve differently on a different database and make every
    # metric irreproducible.
    ranked = sorted(combined.items(), key=lambda item: (-item[1], item[0]))
    return [principle_id for principle_id, _ in ranked[:top_k]]


def _day_text(entries: list[LogEntryLike], mood: int | None) -> str:
    lines = [f"{e.action} [{e.category}]" for e in entries]
    if mood is not None:
        lines.append(f"mood: {mood}/5")
    return "\n".join(lines)


def embedding_candidates(
    db: Session,
    book_id: str,
    query_vector: list[float],
    limit: int = DEFAULT_EMBEDDING_CANDIDATES,
) -> dict[str, float]:
    """Scoped (single-book) cosine-similarity nearest-neighbor query via
    pgvector (architecture SS3 Step A #2: "compared ... against principle
    summary embeddings for that book only -- scoped query, not cross-book").

    Only human_reviewed principles are eligible -- an unreviewed draft must
    never surface in a real analysis (architecture SS2's review gate).
    """
    distance = Principle.embedding.cosine_distance(query_vector).label("distance")
    rows = db.execute(
        select(Principle.principle_id, distance)
        .where(
            Principle.book_id == book_id,
            Principle.review_status == ReviewStatus.human_reviewed,
            Principle.embedding.is_not(None),
        )
        .order_by(distance)
        .limit(limit)
    ).all()
    # pgvector's cosine_distance is 1 - cosine_similarity; convert back so
    # higher is always better, matching tag_match_scores' convention.
    return {principle_id: 1.0 - dist for principle_id, dist in rows}


def retrieve_principles(
    db: Session,
    book_id: str,
    entries: list[LogEntryLike],
    embedding_client: EmbeddingClient,
    mood: int | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> list[str]:
    """Full Step A orchestration: tag match + embedding similarity + rank
    fusion, scoped to one book's human_reviewed principles. No LLM call here
    -- deterministic given the same DB state and embedding client.
    """
    # defer(embedding): only tag_match_scores reads these rows, and it
    # touches principle_id/applies_to_tags only. Without the defer every
    # analysis request drags the book's entire 1024-dim vector set back from
    # Postgres as text (~20KB/row, ~4.7MB for 12-rules-for-life) to throw it
    # away. The similarity search runs server-side in embedding_candidates.
    principles = (
        db.query(Principle)
        .options(defer(Principle.embedding))
        .filter(
            Principle.book_id == book_id,
            Principle.review_status == ReviewStatus.human_reviewed,
        )
        .all()
    )
    if not principles:
        return []

    tag_scores = tag_match_scores(entries, principles)

    embedding_scores: dict[str, float] = {}
    day_text = _day_text(entries, mood)
    if day_text.strip():
        query_vector = embedding_client.embed([day_text], input_type="query")[0]
        embedding_scores = embedding_candidates(db, book_id, query_vector)

    return rank_fusion(tag_scores, embedding_scores, top_k=top_k)
