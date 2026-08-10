import io
from email.message import Message
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest
from sqlalchemy.orm import Session

from app.embeddings import (
    _EMBED_BATCH_SIZE,
    _MIN_SECONDS_BETWEEN_REQUESTS,
    EmbeddingError,
    FakeEmbeddingClient,
    VoyageEmbeddingClient,
    generate_embeddings_for_book,
)
from app.models import Book, Principle


class _FakeHttpResponse:
    """Minimal stand-in for the object urllib.request.urlopen()'s context
    manager yields -- just enough for VoyageEmbeddingClient._post_batch's
    `with ... as response: response.read()` usage."""

    def __init__(self, payload: dict) -> None:
        import json

        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _http_error(code: int, body: bytes = b"{}", headers: dict[str, str] | None = None) -> HTTPError:
    hdrs = Message()
    for key, value in (headers or {}).items():
        hdrs[key] = value
    return HTTPError("https://api.voyageai.com/v1/embeddings", code, "error", hdrs, io.BytesIO(body))


def _embed_response(n: int) -> dict:
    return {"data": [{"embedding": [0.0] * 4} for _ in range(n)]}


def test_fake_embedding_client_is_deterministic() -> None:
    client = FakeEmbeddingClient(dimension=16)
    first = client.embed(["living consciously"])
    second = client.embed(["living consciously"])
    assert first == second


def test_fake_embedding_client_different_texts_differ() -> None:
    client = FakeEmbeddingClient(dimension=16)
    vectors = client.embed(["living consciously", "self-acceptance"])
    assert vectors[0] != vectors[1]


def test_fake_embedding_client_returns_unit_vectors() -> None:
    client = FakeEmbeddingClient(dimension=32)
    [vector] = client.embed(["some text"])
    norm = sum(v * v for v in vector) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-6)


def test_fake_embedding_client_records_calls() -> None:
    client = FakeEmbeddingClient(dimension=8)
    client.embed(["a", "b"], input_type="document")
    client.embed(["query text"], input_type="query")
    assert client.texts_seen == ["a", "b", "query text"]
    assert client.input_types_seen == ["document", "query"]


def test_fake_embedding_client_empty_input_returns_empty() -> None:
    client = FakeEmbeddingClient(dimension=8)
    assert client.embed([]) == []


def test_voyage_client_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError):
        VoyageEmbeddingClient(api_key="")


def test_generate_embeddings_for_book_updates_all_principles(
    db_session: Session, seed_book: Book
) -> None:
    principles = [
        Principle(
            principle_id="living-consciously",
            book_id=seed_book.book_id,
            name="Living Consciously",
            summary="Pay attention to facts and goals rather than operating on autopilot.",
            applies_to_tags=["awareness"],
        ),
        Principle(
            principle_id="self-acceptance",
            book_id=seed_book.book_id,
            name="Self-Acceptance",
            summary="Accept all facts of your reality, including thoughts you dislike.",
            applies_to_tags=["acceptance"],
        ),
    ]
    db_session.add_all(principles)
    db_session.commit()

    client = FakeEmbeddingClient(dimension=1024)
    embedded_count = generate_embeddings_for_book(db_session, seed_book.book_id, client)

    assert embedded_count == 2
    assert sorted(client.texts_seen) == sorted(
        [
            "Living Consciously. Pay attention to facts and goals rather than operating on "
            "autopilot. Tags: awareness",
            "Self-Acceptance. Accept all facts of your reality, including thoughts you dislike. "
            "Tags: acceptance",
        ]
    )

    for principle in principles:
        db_session.refresh(principle)
        assert principle.embedding is not None
        assert len(principle.embedding) == 1024
        assert principle.embedding_id == principle.principle_id


def test_generate_embeddings_for_book_returns_zero_for_book_with_no_principles(
    db_session: Session, seed_book: Book
) -> None:
    client = FakeEmbeddingClient(dimension=1024)
    assert generate_embeddings_for_book(db_session, seed_book.book_id, client) == 0
    assert client.texts_seen == []


def test_voyage_client_splits_into_rate_limit_safe_batches() -> None:
    client = VoyageEmbeddingClient(api_key="test-key")
    texts = [f"principle {i}" for i in range(_EMBED_BATCH_SIZE * 2 + 5)]

    with patch.object(client, "_post_batch") as mock_post_batch:
        mock_post_batch.side_effect = lambda batch, input_type: [[float(len(batch))]] * len(batch)
        vectors = client.embed(texts, input_type="document")

    assert mock_post_batch.call_count == 3
    sizes = [len(call.args[0]) for call in mock_post_batch.call_args_list]
    assert sizes == [_EMBED_BATCH_SIZE, _EMBED_BATCH_SIZE, 5]
    # Order preserved across batch boundaries.
    assert len(vectors) == len(texts)
    assert vectors[0] == [float(_EMBED_BATCH_SIZE)]
    assert vectors[-1] == [5.0]


def test_voyage_client_throttles_between_batches() -> None:
    client = VoyageEmbeddingClient(api_key="test-key")
    texts = [f"p{i}" for i in range(_EMBED_BATCH_SIZE + 1)]

    with (
        patch("app.embeddings.urllib.request.urlopen") as mock_urlopen,
        patch("app.embeddings.time.sleep") as mock_sleep,
        patch("app.embeddings.time.monotonic", side_effect=[0.0, 5.0, 5.0]),
    ):
        mock_urlopen.side_effect = [
            _FakeHttpResponse(_embed_response(_EMBED_BATCH_SIZE)),
            _FakeHttpResponse(_embed_response(1)),
        ]
        client.embed(texts)

    # First batch: no prior request, no sleep. Second batch: only 5s elapsed
    # since the first request, so it waits out the remainder of the 3-RPM window.
    mock_sleep.assert_called_once()
    (waited,) = mock_sleep.call_args.args
    assert waited == pytest.approx(_MIN_SECONDS_BETWEEN_REQUESTS - 5.0)


def test_voyage_client_retries_on_429_honoring_retry_after_header() -> None:
    client = VoyageEmbeddingClient(api_key="test-key")

    with (
        patch("app.embeddings.urllib.request.urlopen") as mock_urlopen,
        patch("app.embeddings.time.sleep") as mock_sleep,
        # Plenty of wall-clock time between attempts, so the RPM throttle
        # itself contributes no extra wait -- isolates the 429-specific
        # backoff this test is actually checking.
        patch("app.embeddings.time.monotonic", side_effect=[0.0, 100.0, 100.0]),
    ):
        mock_urlopen.side_effect = [
            _http_error(429, headers={"Retry-After": "7"}),
            _FakeHttpResponse(_embed_response(1)),
        ]
        vectors = client.embed(["some principle text"])

    assert len(vectors) == 1
    mock_sleep.assert_called_once_with(7.0)


def test_voyage_client_falls_back_to_backoff_when_retry_after_missing() -> None:
    client = VoyageEmbeddingClient(api_key="test-key")

    with (
        patch("app.embeddings.urllib.request.urlopen") as mock_urlopen,
        patch("app.embeddings.time.sleep") as mock_sleep,
        patch("app.embeddings.time.monotonic", side_effect=[0.0, 100.0, 100.0]),
        patch("app.embeddings.random.uniform", return_value=3.5),
    ):
        mock_urlopen.side_effect = [
            _http_error(429),
            _FakeHttpResponse(_embed_response(1)),
        ]
        vectors = client.embed(["some principle text"])

    assert len(vectors) == 1
    mock_sleep.assert_called_once_with(3.5)


def test_voyage_client_raises_after_exhausting_429_retries() -> None:
    client = VoyageEmbeddingClient(api_key="test-key")

    with (
        patch("app.embeddings.urllib.request.urlopen") as mock_urlopen,
        patch("app.embeddings.time.sleep"),
    ):
        mock_urlopen.side_effect = [_http_error(429) for _ in range(10)]
        with pytest.raises(EmbeddingError, match="429"):
            client.embed(["some principle text"])


def test_voyage_client_raises_immediately_on_non_429_http_error() -> None:
    client = VoyageEmbeddingClient(api_key="test-key")

    with (
        patch("app.embeddings.urllib.request.urlopen") as mock_urlopen,
        patch("app.embeddings.time.sleep") as mock_sleep,
    ):
        mock_urlopen.side_effect = [_http_error(500, body=b'{"detail": "server error"}')]
        with pytest.raises(EmbeddingError, match="500"):
            client.embed(["some principle text"])

    mock_sleep.assert_not_called()


def test_voyage_client_raises_embedding_error_on_url_error() -> None:
    client = VoyageEmbeddingClient(api_key="test-key")

    with patch("app.embeddings.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [URLError("DNS lookup failed")]
        with pytest.raises(EmbeddingError, match="DNS lookup failed"):
            client.embed(["some principle text"])
