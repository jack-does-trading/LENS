import uuid

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session


def _ping(client: TestClient, session_id: uuid.UUID) -> dict:
    response = client.post("/api/presence", json={"session_id": str(session_id)})
    assert response.status_code == 200, response.text
    return response.json()


def test_first_ping_counts_one_visit_and_one_online(client: TestClient) -> None:
    body = _ping(client, uuid.uuid4())
    assert body == {"total_visits": 1, "people_online": 1}


def test_repeat_ping_from_same_session_does_not_inflate_visits(client: TestClient) -> None:
    session_id = uuid.uuid4()
    _ping(client, session_id)
    for _ in range(4):
        body = _ping(client, session_id)
    # The whole point of the ON CONFLICT upsert: a heartbeat every 30s for an
    # hour is one visitor, not 120 of them.
    assert body == {"total_visits": 1, "people_online": 1}


def test_distinct_sessions_each_count(client: TestClient) -> None:
    for expected in range(1, 4):
        body = _ping(client, uuid.uuid4())
        assert body["total_visits"] == expected
        assert body["people_online"] == expected


def test_stale_session_still_counts_as_a_visit_but_not_online(
    client: TestClient, db_session: Session
) -> None:
    stale, fresh = uuid.uuid4(), uuid.uuid4()
    _ping(client, stale)
    # Age the heartbeat past ONLINE_WINDOW (75s) without sleeping in a test.
    db_session.execute(
        text("UPDATE site_visits SET last_seen = now() - interval '10 minutes' WHERE session_id = :s"),
        {"s": str(stale)},
    )
    db_session.commit()

    body = _ping(client, fresh)
    assert body["total_visits"] == 2, "a visit is permanent"
    assert body["people_online"] == 1, "only the fresh heartbeat is online"


def test_rejects_a_non_uuid_session_id(client: TestClient) -> None:
    response = client.post("/api/presence", json={"session_id": "not-a-uuid"})
    assert response.status_code == 422
