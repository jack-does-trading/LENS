from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.embeddings import EmbeddingClient
from app.models import Principle, ReviewStatus

DEFAULT_TOP_K = 3
DEFAULT_EMBEDDING_CANDIDATES = 10
# Tag hits are precise (an explicit author-assigned match); embedding
# similarity is recall-oriented and noisier, so it's weighted lower in fusion
# (architecture SS3 Step A #3: "tag-match hits (weighted higher, since
# they're precise)").
TAG_MATCH_WEIGHT = 2.0


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


def rank_fusion(
    tag_scores: dict[str, float],
    embedding_scores: dict[str, float],
    top_k: int = DEFAULT_TOP_K,
    tag_weight: float = TAG_MATCH_WEIGHT,
) -> list[str]:
    """Combine tag-match and embedding-similarity scores into one ranked list
    of principle_ids (architecture SS3 Step A #3). Also a pure function over
    plain dicts, independent of any LLM behavior.
    """
    combined: dict[str, float] = {}
    for principle_id, score in tag_scores.items():
        combined[principle_id] = combined.get(principle_id, 0.0) + tag_weight * score
    for principle_id, score in embedding_scores.items():
        combined[principle_id] = combined.get(principle_id, 0.0) + score
    ranked = sorted(combined.items(), key=lambda item: item[1], reverse=True)
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
    principles = (
        db.query(Principle)
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
