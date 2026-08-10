import uuid
from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Book, DailyLog, Principle, ReviewStatus, User


def test_daily_logs_allows_multiple_rows_same_user_and_date(db_session, seed_user: User, seed_book: Book) -> None:
    # Each ask-for-advice is its own independent situation (migration 006
    # dropped the old one-row-per-day uniqueness) -- a user can log more than
    # one situation on the same date without any DB-level rejection.
    first = DailyLog(
        log_id=uuid.uuid4(),
        user_id=seed_user.user_id,
        date=date(2026, 7, 1),
        chosen_book_id=seed_book.book_id,
    )
    second = DailyLog(
        log_id=uuid.uuid4(),
        user_id=seed_user.user_id,
        date=date(2026, 7, 1),
        chosen_book_id=seed_book.book_id,
    )
    db_session.add_all([first, second])
    db_session.commit()

    rows = (
        db_session.query(DailyLog)
        .filter(DailyLog.user_id == seed_user.user_id, DailyLog.date == date(2026, 7, 1))
        .all()
    )
    assert len(rows) == 2


def test_principle_with_nonexistent_book_id_rejected_by_fk(db_session) -> None:
    principle = Principle(
        principle_id="orphan-principle",
        book_id="does-not-exist",
        name="Orphan",
        summary="A principle pointing at a book that was never created.",
        review_status=ReviewStatus.pending_review,
    )
    db_session.add(principle)

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()
