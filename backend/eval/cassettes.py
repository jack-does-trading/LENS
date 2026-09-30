"""Recorded retrieval corpus and query vectors, so the eval runs without an API key.

Why record at all. Retrieval fuses tag matching (deterministic) with embedding
cosine similarity (needs real vectors). app.embeddings.FakeEmbeddingClient seeds
random.Random with sha256(text), so distinct texts land in roughly orthogonal
directions regardless of meaning -- "went for a run" sits as far from "went
jogging" as from "filed my taxes". Scoring retrieval against it would measure
the tag arm only and report a number that looks like recall and isn't.

Why not call Voyage in CI. It needs VOYAGE_API_KEY as a repo secret, it costs
money on every push, and it is not reproducible: if Voyage retrains voyage-3
your recall moves and nothing tells you whether your code regressed or their
model shifted. Re-recording is instead an explicit, reviewable commit -- the
cassette changes, the numbers change, and the diff says which.

Storage is stdlib array + gzip rather than numpy .npz. numpy is not a
dependency of this project (the plan assumed pgvector pulled it in; it does
not), and 1213 x 1024 float32 is 5MB either way. Adding a numeric stack to make
CI read 5MB of floats is a poor trade, and the rest of this backend deliberately
sticks to the standard library for the same reason it uses urllib over requests.

The corpus is recorded from the database rather than rebuilt from
tools/local_extraction/output/*.json on purpose: those files still carry the
placeholder applies_to_tags that scripts/retag_principles.py replaced, so
seeding from them would silently restore the bug the retag fixed.
"""

from __future__ import annotations

import gzip
import json
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CASSETTE_DIR = Path(__file__).resolve().parent / "cassettes"
CORPUS_PATH = CASSETTE_DIR / "corpus.json.gz"
VECTORS_PATH = CASSETTE_DIR / "principle_vectors.bin.gz"
QUERY_VECTORS_PATH = CASSETTE_DIR / "query_vectors.json"


class CassetteError(RuntimeError):
    pass


@dataclass(frozen=True)
class Corpus:
    """Everything retrieval reads, frozen at record time."""

    books: list[dict[str, Any]]
    principles: list[dict[str, Any]]
    vectors: dict[str, list[float]]
    dimension: int

    def for_book(self, book_id: str) -> list[dict[str, Any]]:
        return [p for p in self.principles if p["book_id"] == book_id]


def save_corpus(
    books: list[dict[str, Any]], principles: list[dict[str, Any]], vectors: dict[str, list[float]]
) -> tuple[int, int]:
    """Write the corpus, vectors flattened into one gzipped float32 blob.

    Vector order follows `principles`, so the blob needs no per-row keys; a
    mismatch between the two files is caught on load rather than silently
    misattributing vectors to the wrong principles.
    """
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    dimension = len(next(iter(vectors.values()))) if vectors else 0
    flat = array("f")
    for principle in principles:
        vector = vectors[principle["principle_id"]]
        if len(vector) != dimension:
            raise CassetteError(
                f"{principle['principle_id']}: dimension {len(vector)} != {dimension}"
            )
        flat.extend(vector)
    with gzip.open(VECTORS_PATH, "wb", compresslevel=6) as fh:
        fh.write(flat.tobytes())
    payload = {
        "dimension": dimension,
        "books": books,
        # Vector order is positional against this list; do not reorder it.
        "principles": principles,
    }
    with gzip.open(CORPUS_PATH, "wt", encoding="utf-8", compresslevel=9) as fh:
        json.dump(payload, fh)
    return CORPUS_PATH.stat().st_size, VECTORS_PATH.stat().st_size


def load_corpus() -> Corpus:
    if not CORPUS_PATH.exists() or not VECTORS_PATH.exists():
        raise CassetteError(
            f"no cassette at {CASSETTE_DIR}. Record one with "
            "scripts/record_cassettes.py --database-url ..."
        )
    with gzip.open(CORPUS_PATH, "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    dimension = payload["dimension"]
    principles = payload["principles"]
    with gzip.open(VECTORS_PATH, "rb") as fh:
        flat = array("f")
        flat.frombytes(fh.read())
    expected = len(principles) * dimension
    if len(flat) != expected:
        raise CassetteError(
            f"cassette is inconsistent: {len(flat)} floats for {len(principles)} principles "
            f"x {dimension} dims (expected {expected}). Re-record it."
        )
    vectors = {
        p["principle_id"]: list(flat[i * dimension : (i + 1) * dimension])
        for i, p in enumerate(principles)
    }
    return Corpus(
        books=payload["books"], principles=principles, vectors=vectors, dimension=dimension
    )


class CassetteEmbeddingClient:
    """Replays recorded query vectors. Implements app.embeddings.EmbeddingClient.

    A miss raises instead of falling back to a fake vector: a silently-degraded
    embedding arm would still produce a plausible-looking recall number, which
    is the single worst failure mode an eval harness can have.
    """

    def __init__(self, vectors: dict[str, list[float]] | None = None, model: str = "voyage-3"):
        self._model = model
        if vectors is None:
            if not QUERY_VECTORS_PATH.exists():
                raise CassetteError(f"no query vectors at {QUERY_VECTORS_PATH}")
            vectors = json.loads(QUERY_VECTORS_PATH.read_text())
        self._vectors = vectors
        self.hits = 0

    @staticmethod
    def key(text: str, input_type: str, model: str) -> str:
        import hashlib

        return hashlib.sha256(f"{text}|{input_type}|{model}".encode()).hexdigest()

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        out = []
        for text in texts:
            key = self.key(text, input_type, self._model)
            if key not in self._vectors:
                raise CassetteError(
                    f"no recorded vector for {input_type} {text[:60]!r}. "
                    "Re-record with scripts/measure_retrieval.py against a live database."
                )
            self.hits += 1
            out.append(self._vectors[key])
        return out


# --- judge verdicts -------------------------------------------------------
# The same record-once-replay-forever trade as the query vectors above, applied
# to the faithfulness judge. Calibration needs the judge's answers, the judge is
# a hosted LLM, and an LLM in a PR gate is both a cost and a flake. So the
# verdicts are recorded locally against real Groq and replayed in CI.
#
# Keyed by sha256 of the full prompt, not by case_id. That is the load-bearing
# detail: the prompt contains the rubric, the principles and the output, so
# editing JUDGE_PROMPT or a calibration case invalidates its recording and the
# replay misses loudly. Keying on case_id would happily replay a verdict the
# judge gave to a question it is no longer being asked, and the resulting kappa
# would describe a prompt that no longer exists.

JUDGE_VERDICTS_PATH = CASSETTE_DIR / "judge_verdicts.json"


def judge_key(prompt: str, model: str) -> str:
    import hashlib

    return hashlib.sha256(f"{model}|{prompt}".encode("utf-8")).hexdigest()


def load_judge_verdicts() -> dict[str, Any]:
    if not JUDGE_VERDICTS_PATH.exists():
        return {}
    return json.loads(JUDGE_VERDICTS_PATH.read_text())


def save_judge_verdicts(verdicts: dict[str, Any]) -> None:
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    JUDGE_VERDICTS_PATH.write_text(json.dumps(verdicts, indent=2, sort_keys=True) + "\n")


class CassetteJudgeClient:
    """Replays recorded judge completions. Implements app.llm.LLMClient.

    A miss raises. The alternative -- returning a default verdict -- would let a
    stale or missing recording produce a plausible kappa for a judge that was
    never asked anything, which is the one failure an eval harness must never
    have. `record` mode is the escape hatch, and it is explicit.
    """

    def __init__(self, model: str, verdicts: dict[str, Any] | None = None) -> None:
        self._model = model
        self._verdicts = load_judge_verdicts() if verdicts is None else verdicts
        self.misses: list[str] = []

    def generate(self, prompt: str, *, timeout: float | None = None) -> str:
        key = judge_key(prompt, self._model)
        entry = self._verdicts.get(key)
        if entry is None:
            self.misses.append(key)
            raise CassetteError(
                f"no recorded judge verdict for {key[:12]}... (model {self._model}). "
                "The prompt or a calibration case changed. Re-record with "
                "scripts/calibrate_judge.py --record."
            )
        return entry["raw"]
