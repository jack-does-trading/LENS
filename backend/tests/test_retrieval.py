from sqlalchemy.orm import Session

from app.embeddings import FakeEmbeddingClient, generate_embeddings_for_book
from app.models import Book, Principle, ReviewStatus
from app.retrieval import (
    LogEntryLike,
    embedding_candidates,
    rank_fusion,
    retrieve_principles,
    tag_match_scores,
)


def _principle(principle_id: str, tags: list[str]) -> Principle:
    return Principle(
        principle_id=principle_id,
        book_id="unused",
        name=principle_id,
        summary="A short summary for testing.",
        applies_to_tags=tags,
    )


# --- tag_match_scores: pure function, no DB ---------------------------------


def test_tag_match_scores_counts_overlapping_tags() -> None:
    entries = [LogEntryLike(category="exercise", action="went for a run")]
    principles = [
        _principle("p1", ["exercise", "habit"]),
        _principle("p2", ["reading"]),
    ]
    assert tag_match_scores(entries, principles) == {"p1": 1.0}


def test_tag_match_scores_is_case_insensitive() -> None:
    entries = [LogEntryLike(category="Exercise")]
    principles = [_principle("p1", ["EXERCISE"])]
    assert tag_match_scores(entries, principles) == {"p1": 1.0}


def test_tag_match_scores_multiple_overlaps_score_higher() -> None:
    entries = [LogEntryLike(category="exercise"), LogEntryLike(category="habit")]
    principles = [_principle("p1", ["exercise", "habit", "self-image"])]
    assert tag_match_scores(entries, principles) == {"p1": 2.0}


def test_tag_match_scores_no_overlap_returns_empty() -> None:
    entries = [LogEntryLike(category="cooking")]
    principles = [_principle("p1", ["exercise"])]
    assert tag_match_scores(entries, principles) == {}


# --- rank_fusion: pure function, no DB ---------------------------------------


def test_rank_fusion_weights_tag_matches_higher() -> None:
    tag_scores = {"p1": 1.0}
    embedding_scores = {"p2": 0.99, "p1": 0.5}
    ranked = rank_fusion(tag_scores, embedding_scores, top_k=5, tag_weight=2.0)
    # Under RRF: p1 = 2/(60+1) + 1/(60+2) = .0328+.0161; p2 = 1/(60+1) = .0164.
    # p1 still ranks first despite p2's raw embedding score being nearly a
    # perfect match -- but note that what carries p1 is now its *rank* in each
    # arm, not the magnitude 2.0, so the tag arm can no longer win purely
    # because its units are larger than cosine's.
    assert ranked == ["p1", "p2"]


def test_rank_fusion_gives_tied_tag_scores_identical_weight() -> None:
    """A broad category tag-matches many principles at the same flat score.
    Those ties must not be ordered by the tag arm -- it has no information to
    order them with -- so the embedding arm decides, in its own order.
    """
    tag_scores = {"p1": 1.0, "p2": 1.0, "p3": 1.0}
    embedding_scores = {"p3": 0.9, "p2": 0.8, "p1": 0.7}
    assert rank_fusion(tag_scores, embedding_scores, top_k=3, tag_weight=2.0) == [
        "p3",
        "p2",
        "p1",
    ]


def test_rank_fusion_is_deterministic_regardless_of_input_order() -> None:
    """Ties used to resolve by dict insertion order, i.e. DB row order, which
    made every retrieval metric irreproducible across databases."""
    forward = rank_fusion({"a": 1.0, "b": 1.0, "c": 1.0}, {}, top_k=2)
    reverse = rank_fusion({"c": 1.0, "b": 1.0, "a": 1.0}, {}, top_k=2)
    assert forward == reverse == ["a", "b"]


def test_rank_fusion_tag_arm_cannot_be_outscaled_by_unit_choice() -> None:
    """The regression this replaced: cosine tops out near 0.4 in practice
    while a tag hit scored a flat 2.0, so one tag match outranked every
    embedding result no matter how strong. With rank-based fusion a
    tag_weight of 0 leaves the embedding order completely intact.
    """
    embedding_scores = {"p1": 0.40, "p2": 0.39, "p3": 0.38}
    ranked = rank_fusion({"p3": 1.0}, embedding_scores, top_k=3, tag_weight=0.0)
    assert ranked == ["p1", "p2", "p3"]


def test_rank_fusion_respects_top_k() -> None:
    tag_scores = {"p1": 3.0, "p2": 2.0, "p3": 1.0}
    ranked = rank_fusion(tag_scores, {}, top_k=2)
    assert ranked == ["p1", "p2"]


def test_rank_fusion_embedding_only_when_no_tag_matches() -> None:
    ranked = rank_fusion({}, {"p1": 0.8, "p2": 0.9}, top_k=5)
    assert ranked == ["p2", "p1"]


def test_rank_fusion_empty_inputs_returns_empty() -> None:
    assert rank_fusion({}, {}) == []


# --- embedding_candidates / retrieve_principles: require Postgres + pgvector -


def _add_principle(
    db_session: Session, book_id: str, principle_id: str, tags: list[str], review_status=ReviewStatus.human_reviewed
) -> Principle:
    principle = Principle(
        principle_id=principle_id,
        book_id=book_id,
        name=principle_id,
        summary=f"Summary text for {principle_id}.",
        applies_to_tags=tags,
        review_status=review_status,
    )
    db_session.add(principle)
    db_session.commit()
    db_session.refresh(principle)
    return principle


def test_embedding_candidates_scoped_to_book(db_session: Session, seed_book: Book) -> None:
    other_book = Book(
        book_id="other-book",
        title="Other Book",
        author="Someone Else",
        core_thesis="An unrelated thesis for scoping tests.",
        review_status=ReviewStatus.human_reviewed,
    )
    db_session.add(other_book)
    db_session.commit()

    p1 = _add_principle(db_session, seed_book.book_id, "p1", ["habit"])
    p2 = _add_principle(db_session, other_book.book_id, "p2", ["habit"])

    client = FakeEmbeddingClient(dimension=1024)
    generate_embeddings_for_book(db_session, seed_book.book_id, client)
    generate_embeddings_for_book(db_session, other_book.book_id, client)
    db_session.refresh(p1)

    results = embedding_candidates(db_session, seed_book.book_id, list(p1.embedding))
    assert "p2" not in results
    assert "p1" in results


def test_embedding_candidates_excludes_pending_review(db_session: Session, seed_book: Book) -> None:
    p1 = _add_principle(
        db_session, seed_book.book_id, "p1", ["habit"], review_status=ReviewStatus.pending_review
    )
    client = FakeEmbeddingClient(dimension=1024)
    generate_embeddings_for_book(db_session, seed_book.book_id, client)
    db_session.refresh(p1)

    results = embedding_candidates(db_session, seed_book.book_id, list(p1.embedding))
    assert results == {}


def test_embedding_candidates_excludes_principles_without_embedding(
    db_session: Session, seed_book: Book
) -> None:
    _add_principle(db_session, seed_book.book_id, "p1", ["habit"])
    client = FakeEmbeddingClient(dimension=1024)
    query_vector = client.embed(["some query"])[0]

    results = embedding_candidates(db_session, seed_book.book_id, query_vector)
    assert results == {}


def test_retrieve_principles_ranks_tag_match_first(db_session: Session, seed_book: Book) -> None:
    _add_principle(db_session, seed_book.book_id, "matching", ["exercise"])
    _add_principle(db_session, seed_book.book_id, "unrelated", ["cooking"])
    client = FakeEmbeddingClient(dimension=1024)
    generate_embeddings_for_book(db_session, seed_book.book_id, client)

    entries = [LogEntryLike(category="exercise", action="went for a run")]
    ranked = retrieve_principles(db_session, seed_book.book_id, entries, client)

    assert ranked[0] == "matching"


def test_retrieve_principles_returns_empty_for_book_with_no_reviewed_principles(
    db_session: Session, seed_book: Book
) -> None:
    client = FakeEmbeddingClient(dimension=1024)
    entries = [LogEntryLike(category="exercise")]
    assert retrieve_principles(db_session, seed_book.book_id, entries, client) == []
