from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Book, DailyLog, User


def _log(user: User, book: Book, log_date: date) -> DailyLog:
    return DailyLog(user_id=user.user_id, chosen_book_id=book.book_id, date=log_date)


def test_get_streak_computes_current_and_longest(
    client: TestClient, db_session: Session, seed_user: User, seed_book: Book
) -> None:
    today = date.today()
    db_session.add_all(
        [
            _log(seed_user, seed_book, today),
            _log(seed_user, seed_book, today - timedelta(days=1)),
            _log(seed_user, seed_book, today - timedelta(days=2)),
        ]
    )
    db_session.commit()

    response = client.get(
        "/api/streaks", params={"user_id": str(seed_user.user_id), "book_id": seed_book.book_id}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["current_streak_days"] == 3
    assert body["longest_streak_days"] == 3
    assert body["metric_id"] == "consistency"


def test_get_streak_with_no_logs_returns_zero(
    client: TestClient, seed_user: User, seed_book: Book
) -> None:
    response = client.get(
        "/api/streaks", params={"user_id": str(seed_user.user_id), "book_id": seed_book.book_id}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["current_streak_days"] == 0
    assert body["longest_streak_days"] == 0


def test_get_streak_is_idempotent_on_repeat_calls(
    client: TestClient, db_session: Session, seed_user: User, seed_book: Book
) -> None:
    db_session.add(_log(seed_user, seed_book, date.today()))
    db_session.commit()

    first = client.get("/api/streaks", params={"user_id": str(seed_user.user_id), "book_id": seed_book.book_id})
    second = client.get("/api/streaks", params={"user_id": str(seed_user.user_id), "book_id": seed_book.book_id})
    assert first.json()["streak_id"] == second.json()["streak_id"]
