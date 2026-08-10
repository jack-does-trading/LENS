import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture
def ingestion_api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "ingestion_api_key", "test-secret-key")
    return "test-secret-key"


def _valid_payload(book_id: str = "six-pillars-of-self-esteem") -> dict:
    return {
        "book_id": book_id,
        "title": "The Six Pillars of Self-Esteem",
        "author": "Nathaniel Branden",
        "core_thesis": "Self-esteem grows from practices like living consciously, self-acceptance, and self-responsibility, not from external validation.",
        "tone": "philosophical",
        "extraction_method": "local_model",
        "principles": [
            {
                "principle_id": "living-consciously",
                "name": "Living Consciously",
                "summary": "Pay attention to facts, needs, and goals rather than operating on autopilot.",
                "source_chapter": "Chapter 2",
                "applies_to_tags": ["awareness", "mindfulness"],
            }
        ],
    }


def test_submit_without_configured_key_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ingestion_api_key", None)
    response = client.post("/api/ingestion/draft-submissions", json=_valid_payload())
    assert response.status_code == 503


def test_submit_without_header_returns_401(client: TestClient, ingestion_api_key: str) -> None:
    response = client.post("/api/ingestion/draft-submissions", json=_valid_payload())
    assert response.status_code == 401


def test_submit_with_wrong_key_returns_401(client: TestClient, ingestion_api_key: str) -> None:
    response = client.post(
        "/api/ingestion/draft-submissions",
        json=_valid_payload(),
        headers={"X-Ingestion-Api-Key": "wrong-key"},
    )
    assert response.status_code == 401


def test_submit_new_draft_creates_book_and_principles(
    client: TestClient, ingestion_api_key: str
) -> None:
    response = client.post(
        "/api/ingestion/draft-submissions",
        json=_valid_payload(),
        headers={"X-Ingestion-Api-Key": ingestion_api_key},
    )
    assert response.status_code == 201
    body = response.json()

    assert body["book"]["book_id"] == "six-pillars-of-self-esteem"
    assert body["book"]["review_status"] == "pending_review"
    assert body["book"]["extraction_method"] == "local_model"
    assert body["book"]["version"] == 1
    assert len(body["principles"]) == 1
    assert body["principles"][0]["principle_id"] == "living-consciously"
    assert body["principles"][0]["review_status"] == "pending_review"
    assert body["principles"][0]["embedding_id"] is None

    get_response = client.get("/api/books/six-pillars-of-self-esteem")
    assert get_response.status_code == 200


def test_resubmit_without_replace_returns_409(client: TestClient, ingestion_api_key: str) -> None:
    headers = {"X-Ingestion-Api-Key": ingestion_api_key}
    client.post("/api/ingestion/draft-submissions", json=_valid_payload(), headers=headers)

    response = client.post("/api/ingestion/draft-submissions", json=_valid_payload(), headers=headers)
    assert response.status_code == 409


def test_resubmit_with_replace_true_overwrites(client: TestClient, ingestion_api_key: str) -> None:
    headers = {"X-Ingestion-Api-Key": ingestion_api_key}
    client.post("/api/ingestion/draft-submissions", json=_valid_payload(), headers=headers)

    updated = _valid_payload()
    updated["title"] = "The Six Pillars of Self-Esteem (Revised)"
    updated["principles"] = [
        {
            "principle_id": "self-acceptance",
            "name": "Self-Acceptance",
            "summary": "Accept all facts of your reality, including thoughts and feelings you dislike.",
            "source_chapter": "Chapter 3",
            "applies_to_tags": ["acceptance"],
        }
    ]

    response = client.post(
        "/api/ingestion/draft-submissions?replace=true", json=updated, headers=headers
    )
    assert response.status_code == 201
    body = response.json()
    assert body["book"]["title"] == "The Six Pillars of Self-Esteem (Revised)"
    assert [p["principle_id"] for p in body["principles"]] == ["self-acceptance"]

    principles_response = client.get(
        "/api/principles", params={"book_id": "six-pillars-of-self-esteem"}
    )
    assert [p["principle_id"] for p in principles_response.json()] == ["self-acceptance"]


def test_submit_rejects_core_thesis_over_word_cap(client: TestClient, ingestion_api_key: str) -> None:
    payload = _valid_payload()
    payload["core_thesis"] = " ".join(["word"] * 151)
    response = client.post(
        "/api/ingestion/draft-submissions",
        json=payload,
        headers={"X-Ingestion-Api-Key": ingestion_api_key},
    )
    assert response.status_code == 422


def test_submit_rejects_principle_with_overlength_quote(
    client: TestClient, ingestion_api_key: str
) -> None:
    payload = _valid_payload()
    long_quote = " ".join(["word"] * 16)
    payload["principles"][0]["summary"] = f'The author says "{long_quote}" about this.'
    response = client.post(
        "/api/ingestion/draft-submissions",
        json=payload,
        headers={"X-Ingestion-Api-Key": ingestion_api_key},
    )
    assert response.status_code == 422


def test_submit_rejects_principle_with_multiple_quotes(
    client: TestClient, ingestion_api_key: str
) -> None:
    payload = _valid_payload()
    payload["principles"][0]["summary"] = 'As the author notes, "quote one" and also "quote two".'
    response = client.post(
        "/api/ingestion/draft-submissions",
        json=payload,
        headers={"X-Ingestion-Api-Key": ingestion_api_key},
    )
    assert response.status_code == 422


def test_submit_requires_at_least_one_principle(client: TestClient, ingestion_api_key: str) -> None:
    payload = _valid_payload()
    payload["principles"] = []
    response = client.post(
        "/api/ingestion/draft-submissions",
        json=payload,
        headers={"X-Ingestion-Api-Key": ingestion_api_key},
    )
    assert response.status_code == 422


def test_submit_rejects_duplicate_principle_id_in_payload(
    client: TestClient, ingestion_api_key: str
) -> None:
    payload = _valid_payload()
    payload["principles"] = payload["principles"] * 2
    response = client.post(
        "/api/ingestion/draft-submissions",
        json=payload,
        headers={"X-Ingestion-Api-Key": ingestion_api_key},
    )
    assert response.status_code == 422
