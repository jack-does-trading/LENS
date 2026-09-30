"""GET /api/metrics/quality -- the number that would have caught the silent
Groq outage described in the README.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Analysis, Book, DailyLog, User, VerificationStatus


@pytest.fixture
def log(db_session: Session, seed_user: User, seed_book: Book) -> DailyLog:
    entry = DailyLog(
        user_id=seed_user.user_id,
        chosen_book_id=seed_book.book_id,
        date="2026-09-19",
        entries=[{"time": "09:00", "action": "felt unsure", "category": "awareness"}],
        mood=3,
    )
    db_session.add(entry)
    db_session.commit()
    db_session.refresh(entry)
    return entry


def _analysis(db_session: Session, user: User, book: Book, **kw) -> Analysis:
    """One analysis, with its own daily_log (analyses.log_id is unique)."""
    log = DailyLog(
        user_id=user.user_id,
        chosen_book_id=book.book_id,
        date="2026-09-19",
        entries=[{"time": "09:00", "action": "felt unsure", "category": "awareness"}],
        mood=3,
    )
    db_session.add(log)
    db_session.flush()

    fields = dict(
        log_id=log.log_id,
        retrieved_principle_ids=["p1"],
        reflection="You noticed something today.",
        verification_status=VerificationStatus.passed,
        synthesis_attempts=1,
        verification_issues=[],
        llm_provider="groq",
        llm_model="openai/gpt-oss-120b",
        prompt_version="synthesis-v1+verification-v1",
        latency_ms=1200,
    )
    fields.update(kw)
    analysis = Analysis(**fields)
    db_session.add(analysis)
    db_session.commit()
    return analysis


def test_empty_window_reports_zero_not_an_error(client: TestClient) -> None:
    body = client.get("/api/metrics/quality").json()
    assert body["total_analyses"] == 0
    assert body["fallback_rate"] == 0.0
    # None, not 0.0 -- "no data" and "every analysis passed first try" are
    # different answers and the endpoint must not conflate them.
    assert body["mean_synthesis_attempts"] is None
    assert body["first_attempt_pass_rate"] is None


def test_fallback_rate_and_issue_histogram(
    client: TestClient, db_session: Session, seed_user: User, seed_book: Book
) -> None:
    _analysis(db_session, seed_user, seed_book)
    _analysis(db_session, seed_user, seed_book)
    _analysis(
        db_session,
        seed_user,
        seed_book,
        verification_status=VerificationStatus.fallback_used,
        synthesis_attempts=5,
        verification_issues=[
            "entailment check: reflection claims something not supported by the principles",
            "reflection is written in first person (uses \"I\"/\"my\"/\"me\")",
        ],
    )

    body = client.get("/api/metrics/quality").json()
    assert body["total_analyses"] == 3
    assert body["fallback_used"] == 1
    assert body["fallback_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert body["mean_synthesis_attempts"] == pytest.approx((1 + 1 + 5) / 3, abs=0.01)
    assert body["issue_counts"] == {"entailment_failed": 1, "first_person_voice": 1}
    assert body["by_model"] == {"groq/openai/gpt-oss-120b": 3}


def test_total_outage_reads_as_a_hundred_percent_fallback(
    client: TestClient, db_session: Session, seed_user: User, seed_book: Book
) -> None:
    """The exact shape of the incident this endpoint exists for: every call to
    the provider fails, every analysis silently becomes a template, and nothing
    else in the system looks wrong.
    """
    for _ in range(3):
        _analysis(
            db_session,
            seed_user,
            seed_book,
            verification_status=VerificationStatus.fallback_used,
            synthesis_attempts=5,
            verification_issues=["the previous response was rejected: LLMError: Groq request failed"],
        )

    body = client.get("/api/metrics/quality").json()
    assert body["fallback_rate"] == 1.0
    assert body["issue_counts"] == {"synthesis_error": 3}
    assert body["first_attempt_pass_rate"] is None  # nothing passed at all


def test_first_attempt_pass_rate_counts_only_passing_analyses(
    client: TestClient, db_session: Session, seed_user: User, seed_book: Book
) -> None:
    _analysis(db_session, seed_user, seed_book, synthesis_attempts=1)
    _analysis(db_session, seed_user, seed_book, synthesis_attempts=3)
    _analysis(
        db_session,
        seed_user,
        seed_book,
        verification_status=VerificationStatus.fallback_used,
        synthesis_attempts=5,
    )

    body = client.get("/api/metrics/quality").json()
    # 1 of the 2 *passing* rows landed on the first attempt; the fallback row
    # is not in the denominator.
    assert body["first_attempt_pass_rate"] == 0.5


def test_rows_predating_the_telemetry_migration_are_excluded_from_the_mean(
    client: TestClient, db_session: Session, seed_user: User, seed_book: Book
) -> None:
    """Migration 008 backfills nothing, so older rows carry NULL. Counting a
    NULL as zero attempts would drag the mean below the possible minimum of 1.
    """
    _analysis(db_session, seed_user, seed_book, synthesis_attempts=None, llm_model=None)
    _analysis(db_session, seed_user, seed_book, synthesis_attempts=3)

    body = client.get("/api/metrics/quality").json()
    assert body["total_analyses"] == 2
    assert body["mean_synthesis_attempts"] == 3.0
    assert body["by_model"] == {"groq/openai/gpt-oss-120b": 1}


def test_window_days_excludes_older_rows(
    client: TestClient, db_session: Session, seed_user: User, seed_book: Book
) -> None:
    now = datetime.now(timezone.utc)
    _analysis(db_session, seed_user, seed_book, created_at=now - timedelta(days=30))
    _analysis(db_session, seed_user, seed_book, created_at=now - timedelta(hours=1))

    assert client.get("/api/metrics/quality?window_days=7").json()["total_analyses"] == 1
    assert client.get("/api/metrics/quality?window_days=60").json()["total_analyses"] == 2


def test_response_leaks_no_user_text(
    client: TestClient, db_session: Session, seed_user: User, seed_book: Book
) -> None:
    """The endpoint is unauthenticated like every other route here, so it must
    expose counts only -- never a reflection, a journal entry, or a principle.
    """
    _analysis(db_session, seed_user, seed_book, reflection="You wrote about a private thing.")

    raw = client.get("/api/metrics/quality").text
    assert "private thing" not in raw
    assert "felt unsure" not in raw


def test_window_days_is_validated(client: TestClient) -> None:
    assert client.get("/api/metrics/quality?window_days=0").status_code == 422
    assert client.get("/api/metrics/quality?window_days=9999").status_code == 422
