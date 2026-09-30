#!/usr/bin/env python3
"""Replace placeholder `applies_to_tags` with real, retrieval-usable ones.

    # see what would change, no writes, no cost beyond the LLM calls
    python scripts/retag_principles.py --database-url ... --dry-run --limit 12

    # apply, then regenerate the affected books' embeddings (needs VOYAGE_API_KEY)
    python scripts/retag_principles.py --database-url ... --reembed

Re-embedding is not optional after a real run: `embeddings.py` builds its input
as "{name}. {summary} Tags: {tags}", so every vector for an affected book still
encodes the placeholder text until it is regenerated.

`--database-url` is required and has no default, for the same reason it is in
scripts/export_eval_cases.py: this writes to whatever it is pointed at.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataclasses import dataclass  # noqa: E402

from sqlalchemy import create_engine, select, update  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.embeddings import VoyageEmbeddingClient, generate_embeddings_for_book  # noqa: E402
from app.llm import GroqLLMClient, LLMClient, OllamaLLMClient  # noqa: E402
from app.models import Principle, ReviewStatus  # noqa: E402
from eval.tagging import BATCH, TaggingError, needs_retagging, propose_tags  # noqa: E402


@dataclass
class _Target:
    """Just the columns tagging needs.

    Loading the ORM entity pulls `embedding` too, and pgvector serialises a
    1024-dim vector as text -- roughly 20KB a row, so ~27MB for the whole
    corpus in a single result set. Supabase's pooler closes the connection
    partway through ("SSL connection has been closed unexpectedly"). Selecting
    four columns instead makes the read a few hundred KB.
    """

    principle_id: str
    book_id: str
    name: str
    summary: str
    applies_to_tags: list[str]


def _engine(database_url: str):
    # pool_pre_ping plus TCP keepalives: this script runs for 15-25 minutes
    # against a pooled connection that will otherwise be reaped mid-run.
    return create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
        },
    )


def _client() -> LLMClient:
    if settings.groq_api_key:
        return GroqLLMClient(model=settings.groq_model, api_key=settings.groq_api_key)
    return OllamaLLMClient(host=settings.ollama_host, model=settings.ollama_model)


def run(database_url: str, dry_run: bool, limit: int | None, book_id: str | None, reembed: bool) -> int:
    engine = _engine(database_url)
    llm = _client()
    changed_books: set[str] = set()
    updated = failed = 0

    with Session(engine) as db:
        cols = select(
            Principle.principle_id,
            Principle.book_id,
            Principle.name,
            Principle.summary,
            Principle.applies_to_tags,
        ).where(Principle.review_status == ReviewStatus.human_reviewed)
        if book_id:
            cols = cols.where(Principle.book_id == book_id)
        rows = db.execute(cols.order_by(Principle.principle_id)).all()

        targets = [
            t
            for t in (_Target(r[0], r[1], r[2], r[3], list(r[4])) for r in rows)
            if needs_retagging(t)
        ]
        if limit:
            targets = targets[:limit]

        print(f"{len(targets)} principle(s) need real tags")
        for i in range(0, len(targets), BATCH):
            batch = targets[i : i + BATCH]
            try:
                proposed = propose_tags(llm, batch)
            except TaggingError as exc:
                print(f"  batch {i // BATCH + 1}: {exc} -- skipped")
                failed += len(batch)
                continue

            for t in batch:
                tags = proposed.get(t.principle_id)
                if not tags:
                    failed += 1
                    continue
                if dry_run:
                    print(f"  {t.principle_id}\n      {t.applies_to_tags} -> {tags}")
                else:
                    db.execute(
                        update(Principle)
                        .where(Principle.principle_id == t.principle_id)
                        .values(applies_to_tags=tags)
                    )
                    changed_books.add(t.book_id)
                updated += 1

            # Commit per batch, not once at the end. A 25-minute transaction on
            # a pooled connection is one dropped socket away from losing every
            # batch; committing as we go also makes a re-run resume, since
            # needs_retagging() no longer matches what was already fixed.
            if not dry_run:
                db.commit()
            done = min(i + BATCH, len(targets))
            print(f"  ...{done}/{len(targets)}" + ("" if dry_run else " committed"))

        if dry_run:
            db.rollback()
            print(f"\ndry run: {updated} would be retagged, {failed} had no usable proposal")
        else:
            print(f"\nretagged {updated}, {failed} left unchanged (no usable proposal)")

        if reembed and not dry_run and changed_books:
            if not settings.voyage_api_key:
                print("VOYAGE_API_KEY unset -- embeddings NOT regenerated; vectors still "
                      "encode the old tags. Re-run with the key set.")
            else:
                embed = VoyageEmbeddingClient(
                    api_key=settings.voyage_api_key, model=settings.voyage_model
                )
                for b in sorted(changed_books):
                    n = generate_embeddings_for_book(db, b, embed)
                    db.commit()
                    print(f"  re-embedded {n} principles in {b}")

    engine.dispose()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database-url", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--book-id", default=None)
    ap.add_argument("--reembed", action="store_true")
    args = ap.parse_args()
    return run(args.database_url, args.dry_run, args.limit, args.book_id, args.reembed)


if __name__ == "__main__":
    raise SystemExit(main())
