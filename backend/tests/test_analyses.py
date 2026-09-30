import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.embeddings import FakeEmbeddingClient
from app.llm import FakeLLMClient, GroqLLMClient, OllamaLLMClient
from app.synthesis import PROMPT_VERSION as SYNTHESIS_PROMPT_VERSION
from app.verification import PROMPT_VERSION as VERIFICATION_PROMPT_VERSION
from app.main import app
from app.models import Analysis, Book, DailyLog, Principle, ReviewStatus, User
from app.routers.analyses import MAX_SYNTHESIS_ATTEMPTS, get_embedding_client, get_llm_client


def test_get_llm_client_defaults_to_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", "ollama")
    client = get_llm_client()
    assert isinstance(client, OllamaLLMClient)


def test_get_llm_client_returns_groq_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    monkeypatch.setattr(settings, "groq_model", "llama-3.3-70b-versatile")
    client = get_llm_client()
    assert isinstance(client, GroqLLMClient)


def test_get_llm_client_fails_closed_when_groq_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", None)
    with pytest.raises(HTTPException) as exc_info:
        get_llm_client()
    assert exc_info.value.status_code == 503


@pytest.fixture
def reviewed_principles(db_session: Session, seed_book: Book) -> list[Principle]:
    principles = [
        Principle(
            principle_id="living-consciously",
            book_id=seed_book.book_id,
            name="Living Consciously",
            summary="Pay attention to facts and goals rather than operating on autopilot.",
            applies_to_tags=["awareness"],
            review_status=ReviewStatus.human_reviewed,
        ),
        Principle(
            principle_id="self-acceptance",
            book_id=seed_book.book_id,
            name="Self-Acceptance",
            summary="Accept all facts of your reality, including thoughts you dislike.",
            applies_to_tags=["acceptance"],
            review_status=ReviewStatus.human_reviewed,
        ),
    ]
    db_session.add_all(principles)
    db_session.commit()

    embed_client = FakeEmbeddingClient(dimension=1024)
    from app.embeddings import generate_embeddings_for_book

    generate_embeddings_for_book(db_session, seed_book.book_id, embed_client)
    return principles


@pytest.fixture
def daily_log(db_session: Session, seed_user: User, seed_book: Book) -> DailyLog:
    log = DailyLog(
        user_id=seed_user.user_id,
        chosen_book_id=seed_book.book_id,
        date="2026-07-31",
        entries=[{"time": "09:00", "action": "felt unsure of a decision", "category": "awareness"}],
        mood=3,
    )
    db_session.add(log)
    db_session.commit()
    db_session.refresh(log)
    return log


def _override_clients(llm_responses: list[str]) -> FakeLLMClient:
    fake_llm = FakeLLMClient(responses=llm_responses)
    app.dependency_overrides[get_llm_client] = lambda: fake_llm
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient(dimension=1024)
    return fake_llm


@pytest.fixture(autouse=True)
def _clear_analysis_overrides():
    yield
    app.dependency_overrides.pop(get_llm_client, None)
    app.dependency_overrides.pop(get_embedding_client, None)


def test_create_analysis_happy_path(
    client: TestClient, reviewed_principles: list[Principle], daily_log: DailyLog
) -> None:
    _override_clients(
        [
            '{"reflection": "You are being hard on yourself about a decision you made today.", '
            '"suggestions": [{"text": "Notice the self-talk tonight.", "principle_id": "living-consciously", "explanation": "Awareness is the first step."}]}',
            '{"verdict": "PASS"}',
        ]
    )

    response = client.post("/api/analyses", json={"log_id": str(daily_log.log_id)})
    assert response.status_code == 201
    body = response.json()
    assert body["verification_status"] == "passed"
    assert "living-consciously" in body["retrieved_principle_ids"]
    assert isinstance(body["reflection"], str) and body["reflection"]

    suggestions = client.get("/api/suggestions", params={"analysis_id": body["analysis_id"]}).json()
    assert len(suggestions) == 1
    assert suggestions[0]["principle_id"] == "living-consciously"
    assert suggestions[0]["explanation"] == "Awareness is the first step."


def test_create_analysis_falls_back_when_verification_fails_all_attempts(
    client: TestClient, reviewed_principles: list[Principle], daily_log: DailyLog
) -> None:
    bad_response = (
        '{"reflection": "x", "suggestions": [{"text": "y", "principle_id": "unknown-principle", "explanation": "z"}]}'
    )
    _override_clients([bad_response] * MAX_SYNTHESIS_ATTEMPTS)

    response = client.post("/api/analyses", json={"log_id": str(daily_log.log_id)})
    assert response.status_code == 201
    body = response.json()
    assert body["verification_status"] == "fallback_used"


def test_create_analysis_rejects_duplicate_for_same_log(
    client: TestClient, reviewed_principles: list[Principle], daily_log: DailyLog
) -> None:
    _override_clients(
        [
            '{"reflection": "a", "suggestions": [{"text": "b", "principle_id": "living-consciously", "explanation": "c"}]}',
            '{"verdict": "PASS"}',
        ]
    )
    first = client.post("/api/analyses", json={"log_id": str(daily_log.log_id)})
    assert first.status_code == 201

    second = client.post("/api/analyses", json={"log_id": str(daily_log.log_id)})
    assert second.status_code == 409


def test_delete_analysis_allows_regenerating_for_same_log(
    client: TestClient, reviewed_principles: list[Principle], daily_log: DailyLog
) -> None:
    _override_clients(
        [
            '{"reflection": "a", "suggestions": [{"text": "b", "principle_id": "living-consciously", "explanation": "c"}]}',
            '{"verdict": "PASS"}',
        ]
    )
    first = client.post("/api/analyses", json={"log_id": str(daily_log.log_id)})
    assert first.status_code == 201
    analysis_id = first.json()["analysis_id"]

    delete_response = client.delete(f"/api/analyses/{analysis_id}")
    assert delete_response.status_code == 204
    assert client.get(f"/api/analyses/{analysis_id}").status_code == 404
    assert client.get("/api/suggestions", params={"analysis_id": analysis_id}).json() == []

    _override_clients(
        [
            '{"reflection": "d", "suggestions": [{"text": "e", "principle_id": "living-consciously", "explanation": "f"}]}',
            '{"verdict": "PASS"}',
        ]
    )
    second = client.post("/api/analyses", json={"log_id": str(daily_log.log_id)})
    assert second.status_code == 201


def test_delete_analysis_404_for_unknown_id(client: TestClient) -> None:
    response = client.delete(f"/api/analyses/{uuid.uuid4()}")
    assert response.status_code == 404


def test_create_analysis_404_for_unknown_log(client: TestClient) -> None:
    _override_clients([])
    response = client.post("/api/analyses", json={"log_id": str(uuid.uuid4())})
    assert response.status_code == 404


def test_create_analysis_rejects_unreviewed_book(
    client: TestClient, db_session: Session, seed_user: User
) -> None:
    unreviewed_book = Book(
        book_id="draft-book",
        title="Draft Book",
        author="Someone",
        core_thesis="A draft thesis awaiting review.",
        review_status=ReviewStatus.pending_review,
    )
    db_session.add(unreviewed_book)
    db_session.commit()
    log = DailyLog(user_id=seed_user.user_id, chosen_book_id=unreviewed_book.book_id, date="2026-07-31")
    db_session.add(log)
    db_session.commit()
    db_session.refresh(log)

    _override_clients([])
    response = client.post("/api/analyses", json={"log_id": str(log.log_id)})
    assert response.status_code == 400


# --- pipeline telemetry (migration 008) ------------------------------------
# Until these columns existed, an analysis that passed on the first attempt and
# one that only passed on the fifth were identical rows, and a total provider
# outage was indistinguishable from normal operation.


def test_passing_analysis_records_how_it_was_reached(
    client: TestClient, db_session: Session, reviewed_principles: list[Principle], daily_log: DailyLog
) -> None:
    _override_clients(
        [
            '{"reflection": "You noticed the gap between intent and action today.", "suggestions": [{"text": "Write one fact down.", "principle_id": "living-consciously", "explanation": "Attention beats autopilot."}]}',
            '{"verdict": "PASS"}',
        ]
    )
    response = client.post("/api/analyses", json={"log_id": str(daily_log.log_id)})
    assert response.status_code == 201

    analysis = db_session.get(Analysis, uuid.UUID(response.json()["analysis_id"]))
    assert analysis.synthesis_attempts == 1
    assert analysis.verification_issues == []
    assert analysis.prompt_version == f"{SYNTHESIS_PROMPT_VERSION}+{VERIFICATION_PROMPT_VERSION}"
    assert analysis.llm_provider == settings.llm_provider
    assert analysis.latency_ms is not None and analysis.latency_ms >= 0


def test_retried_analysis_records_the_attempt_it_took(
    client: TestClient, db_session: Session, reviewed_principles: list[Principle], daily_log: DailyLog
) -> None:
    """A retry is invisible to the user and used to be invisible afterwards
    too -- `verification_status` says "passed" either way.
    """
    _override_clients(
        [
            # Attempt 1: rule-clean but the entailment judge rejects it.
            '{"reflection": "You noticed something today.", "suggestions": [{"text": "Write one fact down.", "principle_id": "living-consciously", "explanation": "Attention beats autopilot."}]}',
            '{"verdict": "FAIL"}',
            # Attempt 2: accepted.
            '{"reflection": "You noticed the gap between intent and action.", "suggestions": [{"text": "Write one fact down.", "principle_id": "living-consciously", "explanation": "Attention beats autopilot."}]}',
            '{"verdict": "PASS"}',
        ]
    )
    response = client.post("/api/analyses", json={"log_id": str(daily_log.log_id)})
    assert response.status_code == 201
    assert response.json()["verification_status"] == "passed"

    analysis = db_session.get(Analysis, uuid.UUID(response.json()["analysis_id"]))
    assert analysis.synthesis_attempts == 2


def test_provider_outage_is_recorded_not_just_absorbed(
    client: TestClient, db_session: Session, reviewed_principles: list[Principle], daily_log: DailyLog
) -> None:
    """The README's incident, reproduced: every provider call fails, the user
    still gets a 201 and a reasonable-looking answer. The difference now is
    that the row says why.
    """
    _override_clients([])  # FakeLLMClient raises LLMError on every call

    response = client.post("/api/analyses", json={"log_id": str(daily_log.log_id)})
    assert response.status_code == 201
    assert response.json()["verification_status"] == "fallback_used"

    analysis = db_session.get(Analysis, uuid.UUID(response.json()["analysis_id"]))
    assert analysis.synthesis_attempts == MAX_SYNTHESIS_ATTEMPTS
    assert analysis.verification_issues
    assert any("rejected" in issue for issue in analysis.verification_issues)

    # ...and it surfaces as a rate, which is the thing a human would notice.
    metrics = client.get("/api/metrics/quality").json()
    assert metrics["fallback_rate"] == 1.0
    assert metrics["issue_counts"] == {"synthesis_error": 1}
