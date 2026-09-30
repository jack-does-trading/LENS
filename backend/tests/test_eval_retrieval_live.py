"""Has Voyage changed under us? Needs VOYAGE_API_KEY.

The PR gate replays recorded query vectors, which is what makes it
deterministic and free -- and is exactly why it cannot notice a provider-side
change. If Voyage retrains voyage-3, every recorded number stays identical while
production retrieval quietly starts behaving differently.

So this asks the one question the offline gate structurally cannot: does the live
model still return the vector we recorded? Cosine similarity against the
cassette, on the real golden-set query text. A drop here with no code change
means the cassette is stale, not that retrieval regressed -- and the fix is to
re-record, look at how the metrics moved, and commit both together.

One Voyage request for all sixteen queries (VoyageEmbeddingClient batches at 50),
so this costs one call against the 3 RPM free tier rather than sixteen.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.embeddings import VoyageEmbeddingClient
from app.retrieval import LogEntryLike, _day_text
from eval.cases import GOLDEN_PATH, load_cases
from eval.cassettes import QUERY_VECTORS_PATH, CassetteEmbeddingClient

pytestmark = pytest.mark.eval_live

# voyage-3 is deterministic, so a matching model should reproduce a recorded
# vector to floating-point noise. 0.999 leaves room for float32 round-tripping
# through the cassette without leaving room for a retrained model.
MIN_COSINE = 0.999


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _golden_query_texts() -> list[str]:
    texts = []
    for case in load_cases(GOLDEN_PATH, labelled_only=True):
        entries = [
            LogEntryLike(category=e.get("category", ""), action=e.get("action", ""))
            for e in case.entries
        ]
        texts.append(_day_text(entries, case.mood))
    return texts


def test_live_voyage_still_reproduces_the_recorded_query_vectors() -> None:
    if not settings.voyage_api_key:
        pytest.skip("VOYAGE_API_KEY not set")
    if not QUERY_VECTORS_PATH.exists():
        pytest.skip("no recorded query vectors to compare against")

    texts = _golden_query_texts()
    assert texts, "the golden set is empty"

    cassette = CassetteEmbeddingClient(model=settings.voyage_model)
    recorded = cassette.embed(texts, input_type="query")

    live = VoyageEmbeddingClient(
        api_key=settings.voyage_api_key, model=settings.voyage_model
    ).embed(texts, input_type="query")

    assert len(live) == len(recorded)
    drifted = [
        (i, _cosine(r, l)) for i, (r, l) in enumerate(zip(recorded, live)) if _cosine(r, l) < MIN_COSINE
    ]
    assert not drifted, (
        f"{len(drifted)}/{len(texts)} golden queries no longer match the cassette "
        f"(worst cosine {min(c for _, c in drifted):.4f} < {MIN_COSINE}). "
        f"Voyage's {settings.voyage_model} has changed, or the cassette is from a "
        "different model. Re-record and commit the metric change with it."
    )


def test_live_voyage_returns_the_configured_dimension() -> None:
    """A dimension change would break the pgvector column outright, and it is
    the cheapest possible early warning."""
    if not settings.voyage_api_key:
        pytest.skip("VOYAGE_API_KEY not set")
    vector = VoyageEmbeddingClient(
        api_key=settings.voyage_api_key, model=settings.voyage_model
    ).embed(["a short probe"], input_type="query")[0]
    assert len(vector) == settings.embedding_dimension
