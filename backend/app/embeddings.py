from __future__ import annotations

import hashlib
import json
import logging
import random
import time
import urllib.error
import urllib.request
from typing import Protocol

from sqlalchemy.orm import Session

from app.models import Principle

logger = logging.getLogger(__name__)

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"

# Voyage AI's no-billing-method tier caps requests at 3 RPM / 10K tokens per
# minute (see EmbeddingError raised below for the exact message). A book's
# principles were previously sent as one single request -- fine at small
# scale, but a real ~230-principle book blows past 10K TPM in one call and
# gets an immediate 429. Batch size is picked from real principle text
# (avg ~30 words, worst observed ~76 words per principle across every book
# extracted so far) with a comfortable safety margin under the cap.
_EMBED_BATCH_SIZE = 50
# 60s / 3 requests-per-minute, plus a 1s buffer.
_MIN_SECONDS_BETWEEN_REQUESTS = 21.0
_MAX_RETRY_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 2.0
_BACKOFF_MAX_SECONDS = 60.0


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with jitter, used when Voyage doesn't send a
    parseable Retry-After header on a 429."""
    capped = min(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _BACKOFF_MAX_SECONDS)
    return random.uniform(0, capped)


def _parse_retry_after(headers) -> float | None:
    """Voyage's API is OpenAI-style -- Retry-After, if present, is plain
    seconds (unlike Groq's Go-duration-string format elsewhere in this
    project). Never raises; returns None on anything unparseable."""
    value = headers.get("Retry-After") if headers is not None else None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class EmbeddingClient(Protocol):
    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]: ...


class EmbeddingError(RuntimeError):
    """Raised when the embedding provider fails or returns an unexpected shape."""


class VoyageEmbeddingClient:
    """Real client for Voyage AI's embeddings endpoint (architecture SS5 tech stack).

    Uses urllib (stdlib) rather than adding an HTTP client dependency, same
    approach as tools/local_extraction and scripts/submit_local_draft.py.
    """

    def __init__(self, api_key: str, model: str = "voyage-3", timeout: float = 30.0) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._last_request_at: float | None = None

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        """Splits texts into rate-limit-safe batches (see _EMBED_BATCH_SIZE)
        and sends them as separate requests, spaced to respect the 3 RPM
        no-billing-tier cap. Order is preserved across batches. Each batch
        retries on 429 with backoff (see _post_batch) before giving up."""
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _EMBED_BATCH_SIZE):
            batch = texts[start : start + _EMBED_BATCH_SIZE]
            vectors.extend(self._post_batch(batch, input_type))
        return vectors

    def _throttle(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = _MIN_SECONDS_BETWEEN_REQUESTS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _post_batch(self, texts: list[str], input_type: str) -> list[list[float]]:
        request = urllib.request.Request(
            VOYAGE_API_URL,
            data=json.dumps(
                {"input": texts, "model": self._model, "input_type": input_type}
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        # Every branch of this loop either returns or raises -- on the final
        # attempt, `attempt < _MAX_RETRY_ATTEMPTS` is False, so a persistent
        # 429 falls straight into the "raise EmbeddingError" branch below
        # instead of looping around again.
        for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
            self._throttle()
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self._last_request_at = time.monotonic()
            except urllib.error.HTTPError as exc:
                self._last_request_at = time.monotonic()
                if exc.code == 429 and attempt < _MAX_RETRY_ATTEMPTS:
                    wait = _parse_retry_after(exc.headers) or _backoff_seconds(attempt)
                    logger.warning(
                        "Voyage AI rate-limited (429, attempt %d/%d): retrying in %.1fs",
                        attempt,
                        _MAX_RETRY_ATTEMPTS,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                body = exc.read().decode("utf-8")
                raise EmbeddingError(f"Voyage AI request failed: HTTP {exc.code}: {body}") from exc
            except urllib.error.URLError as exc:
                raise EmbeddingError(f"Voyage AI request failed: {exc}") from exc
            else:
                data = payload.get("data")
                if not isinstance(data, list) or len(data) != len(texts):
                    raise EmbeddingError(f"unexpected Voyage AI response shape: {payload!r}")
                return [entry["embedding"] for entry in data]

        raise AssertionError("unreachable: loop above always returns or raises")


class FakeEmbeddingClient:
    """Deterministic, network-free embedding client for tests.

    The same text always maps to the same vector (hash-seeded), and distinct
    texts land in different, roughly-orthogonal directions -- enough to
    exercise cosine-similarity ranking without a real embeddings API.
    """

    def __init__(self, dimension: int = 1024) -> None:
        self.dimension = dimension
        self.texts_seen: list[str] = []
        self.input_types_seen: list[str] = []

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        self.texts_seen.extend(texts)
        self.input_types_seen.append(input_type)
        return [self._vector_for(text) for text in texts]

    def _vector_for(self, text: str) -> list[float]:
        seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16) % (2**32)
        rng = random.Random(seed)
        raw = [rng.gauss(0, 1) for _ in range(self.dimension)]
        norm = sum(v * v for v in raw) ** 0.5
        return [v / norm for v in raw]


def _embedding_text(principle: Principle) -> str:
    if principle.applies_to_tags:
        return f"{principle.name}. {principle.summary} Tags: {', '.join(principle.applies_to_tags)}"
    return f"{principle.name}. {principle.summary}"


def generate_embeddings_for_book(db: Session, book_id: str, client: EmbeddingClient) -> int:
    """(Re)generate embeddings for every principle of a book, from its summary
    + tags (architecture SS2's Publish step: "embeddings are (re)generated for
    the summary + tags"). Returns the number of principles updated.

    embedding_id is set to the principle_id itself -- there's no external
    vector store in this design (pgvector lives in the same Postgres
    instance), so it just marks "this principle currently has an embedding"
    rather than referencing anything external.
    """
    principles = db.query(Principle).filter(Principle.book_id == book_id).all()
    if not principles:
        return 0

    vectors = client.embed([_embedding_text(p) for p in principles], input_type="document")
    for principle, vector in zip(principles, vectors):
        principle.embedding = vector
        principle.embedding_id = principle.principle_id
    db.commit()
    return len(principles)
