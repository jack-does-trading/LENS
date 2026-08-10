import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.models import Book, Principle, ReviewStatus
from app.schemas import PrincipleWriteBase


def _overlong_summary(word_count: int = 201) -> str:
    return " ".join(f"word{i}" for i in range(1, word_count + 1))


def test_principle_summary_over_word_cap_rejected_by_pydantic() -> None:
    with pytest.raises(ValidationError, match="summary exceeds 200-word cap"):
        PrincipleWriteBase(
            principle_id="over-cap-principle",
            book_id="atomic-habits",
            name="Over cap",
            summary=_overlong_summary(),
        )


def test_principle_summary_over_word_cap_rejected_by_db(db_session, seed_book: Book) -> None:
    principle = Principle(
        principle_id="over-cap-principle",
        book_id=seed_book.book_id,
        name="Over cap",
        summary=_overlong_summary(),
        review_status=ReviewStatus.pending_review,
    )
    db_session.add(principle)

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()


def test_valid_principle_summary_is_accepted(db_session, seed_book: Book) -> None:
    summary = " ".join(f"word{i}" for i in range(1, 201))
    principle = Principle(
        principle_id=f"valid-principle-{uuid.uuid4()}",
        book_id=seed_book.book_id,
        name="Valid",
        summary=summary,
        review_status=ReviewStatus.human_reviewed,
    )
    db_session.add(principle)
    db_session.commit()

    stored = db_session.get(Principle, principle.principle_id)
    assert stored is not None
    assert len(stored.summary.split()) == 200
