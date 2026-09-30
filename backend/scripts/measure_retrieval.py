#!/usr/bin/env python3
"""Score retrieval against the labelled golden set.

    python scripts/measure_retrieval.py --database-url ...
    python scripts/measure_retrieval.py --database-url ... --sweep

Read-only. `--sweep` re-fuses the same signals at every (top_k, tag_weight,
candidate-limit) combination, which is how the current constants were chosen
instead of guessed.

Query vectors are cached in eval/cassettes/query_vectors.json keyed by
sha256(text|input_type|model), so the first run pays Voyage once per case and
every later run is free and byte-identical. That cache is the embryo of the
Phase 2 cassette: once principle vectors are recorded the same way, this whole
measurement becomes runnable in CI with no API key and no drift when Voyage
updates voyage-3.

`retrieved_at_export` in the case file is what production returned at export
time. It is deliberately NOT used for scoring here -- this re-runs retrieval
against the corpus as it stands now -- but --baseline scores it, which is what
makes a before/after comparison possible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session, defer  # noqa: E402

from app.config import settings  # noqa: E402
from app.embeddings import EmbeddingError, VoyageEmbeddingClient  # noqa: E402
from app.models import Principle, ReviewStatus  # noqa: E402
from app.retrieval import (  # noqa: E402
    DEFAULT_EMBEDDING_CANDIDATES,
    DEFAULT_TOP_K,
    TAG_MATCH_WEIGHT,
    LogEntryLike,
    _day_text,
    embedding_candidates,
    rank_fusion,
    tag_match_scores,
)
from eval.cases import GOLDEN_PATH, STAGING_PATH, load_cases  # noqa: E402
from eval.metrics import aggregate, format_report, score_case  # noqa: E402
from scripts.retag_principles import _engine  # noqa: E402

CACHE_PATH = Path(__file__).resolve().parents[1] / "eval" / "cassettes" / "query_vectors.json"


def _cache_key(text: str, input_type: str, model: str) -> str:
    return hashlib.sha256(f"{text}|{input_type}|{model}".encode()).hexdigest()


def _embed_with_retry(client: VoyageEmbeddingClient, text: str, attempts: int = 4) -> list[float]:
    """Retry a query embedding through transient network failures.

    VoyageEmbeddingClient._post_batch retries 429s but lets a socket timeout
    propagate, and one timed-out request should not end a run that has already
    paid for a dozen other vectors.
    """
    for attempt in range(1, attempts + 1):
        try:
            return client.embed([text], input_type="query")[0]
        except EmbeddingError as exc:
            if attempt == attempts:
                raise
            wait = 5 * attempt
            print(f"    embed failed ({exc}); retry {attempt}/{attempts - 1} in {wait}s", flush=True)
            time.sleep(wait)
    raise AssertionError("unreachable")


def _collect_signals(
    db: Session, cases: list, cache: dict, limits: tuple[int, ...], cache_path: Path
):
    """Run both retrieval arms once per case and keep the raw scores.

    Split out from scoring so a sweep re-fuses in memory rather than re-querying
    -- fusion is pure, so there is no reason to pay for the signals per config.
    """
    client: VoyageEmbeddingClient | None = None
    signals = []
    for case in cases:
        entries = [
            LogEntryLike(category=e.get("category", ""), action=e.get("action", ""))
            for e in case.entries
        ]
        principles = (
            db.query(Principle)
            .options(defer(Principle.embedding))
            .filter(
                Principle.book_id == case.book_id,
                Principle.review_status == ReviewStatus.human_reviewed,
            )
            .all()
        )
        tag_scores = tag_match_scores(entries, principles)
        day_text = _day_text(entries, case.mood)
        key = _cache_key(day_text, "query", settings.voyage_model)
        if key not in cache:
            if client is None:
                if not settings.voyage_api_key:
                    raise SystemExit("VOYAGE_API_KEY is not set and the query vector is not cached")
                client = VoyageEmbeddingClient(
                    api_key=settings.voyage_api_key, model=settings.voyage_model
                )
            cache[key] = _embed_with_retry(client, day_text)
            # Flush as soon as it exists: these cost money and 20s of
            # rate-limit wait each, so a failure on case 14 must not discard
            # the thirteen vectors already fetched.
            cache_path.write_text(json.dumps(cache))
        vector = cache[key]
        by_limit = {
            limit: embedding_candidates(db, case.book_id, vector, limit=limit)
            for limit in limits
        }
        signals.append((case, tag_scores, by_limit))
    return signals


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database-url", required=True)
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    ap.add_argument("--source", choices=["golden", "staging"], default="golden")
    ap.add_argument("--sweep", action="store_true", help="grid over top_k/tag_weight/candidates")
    ap.add_argument(
        "--baseline",
        action="store_true",
        help="score retrieved_at_export instead (what production returned pre-change)",
    )
    args = ap.parse_args()

    path = GOLDEN_PATH if args.source == "golden" else STAGING_PATH
    cases = [c for c in load_cases(path) if c.expected_principle_ids]
    if not cases:
        print(f"no labelled cases in {path}", file=sys.stderr)
        return 1

    if args.baseline:
        scores = [
            score_case(c.case_id, c.expected_principle_ids, c.retrieved_at_export, args.top_k)
            for c in cases
        ]
        print(format_report(scores, aggregate(scores), args.top_k, "retrieved_at_export"))
        return 0

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
    limits = (10, 25, 50) if args.sweep else (DEFAULT_EMBEDDING_CANDIDATES,)

    engine = _engine(args.database_url)
    try:
        with Session(engine) as db:
            signals = _collect_signals(db, cases, cache, limits, CACHE_PATH)
    finally:
        engine.dispose()
    CACHE_PATH.write_text(json.dumps(cache))

    if not args.sweep:
        scores = [
            score_case(
                case.case_id,
                case.expected_principle_ids,
                rank_fusion(tag, by_limit[DEFAULT_EMBEDDING_CANDIDATES], top_k=args.top_k),
                args.top_k,
            )
            for case, tag, by_limit in signals
        ]
        print(format_report(scores, aggregate(scores), args.top_k, f"{args.source} ({path.name})"))
        return 0

    header = f"{'k':>3} {'cand':>5} {'tagw':>5} {'recall':>7} {'prec':>6} {'MRR':>5} {'hit':>5}"
    print(f"sweep over {len(signals)} cases from {path.name}\n\n{header}\n" + "-" * len(header))
    for k in (3, 5, 10):
        for limit in limits:
            for weight in (0.0, 0.5, 1.0, 2.0, 4.0):
                scores = [
                    score_case(
                        case.case_id,
                        case.expected_principle_ids,
                        rank_fusion(tag, by_limit[limit], top_k=k, tag_weight=weight),
                        k,
                    )
                    for case, tag, by_limit in signals
                ]
                agg = aggregate(scores)
                current = (
                    "  <-- shipped"
                    if (k, limit, weight)
                    == (DEFAULT_TOP_K, DEFAULT_EMBEDDING_CANDIDATES, TAG_MATCH_WEIGHT)
                    else ""
                )
                print(
                    f"{k:>3} {limit:>5} {weight:>5.1f} {agg['recall_at_k']:>7.2f} "
                    f"{agg['precision_at_k']:>6.2f} {agg['mrr']:>5.2f} "
                    f"{agg['hit_rate']:>5.0%}{current}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
