from fastapi.testclient import TestClient

from app.models import Book, Principle, ReviewStatus, User


def test_books_are_read_only(client: TestClient, seed_book: Book) -> None:
    response = client.get("/api/books")
    assert response.status_code == 200
    assert response.json()[0]["book_id"] == seed_book.book_id

    response = client.get(f"/api/books/{seed_book.book_id}")
    assert response.status_code == 200
    assert response.json()["title"] == "Atomic Habits"

    assert client.post("/api/books", json={"book_id": "x"}).status_code == 405
    assert client.put(f"/api/books/{seed_book.book_id}", json={"title": "x"}).status_code == 405


def test_principles_are_read_only(client: TestClient, db_session, seed_book: Book) -> None:
    principle = Principle(
        principle_id="identity-based-habits",
        book_id=seed_book.book_id,
        name="Identity-based habits",
        summary="Focus on who you wish to become, not just outcomes.",
        applies_to_tags=["habit-formation"],
        review_status=ReviewStatus.human_reviewed,
    )
    db_session.add(principle)
    db_session.commit()

    response = client.get("/api/principles", params={"book_id": seed_book.book_id})
    assert response.status_code == 200
    assert len(response.json()) == 1

    response = client.get("/api/principles/identity-based-habits")
    assert response.status_code == 200

    assert client.post("/api/principles", json={"principle_id": "x"}).status_code == 405
    assert (
        client.put(
            "/api/principles/identity-based-habits", json={"name": "x"}
        ).status_code
        == 405
    )


def test_daily_logs_crud(client: TestClient, seed_user: User, seed_book: Book) -> None:
    payload = {
        "user_id": str(seed_user.user_id),
        "date": "2026-07-01",
        "chosen_book_id": seed_book.book_id,
        "entries": [{"time": "07:30", "action": "went for a run", "category": "health"}],
        "mood": 4,
    }

    create_response = client.post("/api/daily-logs", json=payload)
    assert create_response.status_code == 201
    log_id = create_response.json()["log_id"]

    list_response = client.get("/api/daily-logs", params={"user_id": str(seed_user.user_id)})
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1

    update_response = client.put(
        f"/api/daily-logs/{log_id}",
        json={"mood": 5},
    )
    assert update_response.status_code == 200
    assert update_response.json()["mood"] == 5

    delete_response = client.delete(f"/api/daily-logs/{log_id}")
    assert delete_response.status_code == 204

    get_response = client.get(f"/api/daily-logs/{log_id}")
    assert get_response.status_code == 404
