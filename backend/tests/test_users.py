from fastapi.testclient import TestClient

from app.models import Book


def test_create_user_requires_no_pii(client: TestClient) -> None:
    response = client.post("/api/users", json={"timezone": "America/Chicago"})
    assert response.status_code == 201
    body = response.json()
    assert body["timezone"] == "America/Chicago"
    assert body["active_book_id"] is None


def test_get_user_not_found(client: TestClient) -> None:
    import uuid

    response = client.get(f"/api/users/{uuid.uuid4()}")
    assert response.status_code == 404


def test_set_active_book(client: TestClient, seed_book: Book) -> None:
    create_response = client.post("/api/users", json={})
    user_id = create_response.json()["user_id"]

    response = client.put(f"/api/users/{user_id}/active-book", params={"book_id": seed_book.book_id})
    assert response.status_code == 200
    assert response.json()["active_book_id"] == seed_book.book_id


def test_set_active_book_rejects_unknown_book(client: TestClient) -> None:
    create_response = client.post("/api/users", json={})
    user_id = create_response.json()["user_id"]

    response = client.put(f"/api/users/{user_id}/active-book", params={"book_id": "nonexistent-book"})
    assert response.status_code == 404
