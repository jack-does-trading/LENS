#!/usr/bin/env python3
"""Record the retrieval corpus so the golden-set eval can run offline.

    python scripts/record_cassettes.py --database-url ...

Read-only against the source database. Dumps every human_reviewed principle
(id, book, name, summary, tags, embedding) for the books the golden set
exercises, plus their book rows, into eval/cassettes/.

No Voyage calls: the vectors already exist in the database, written there by
app.embeddings. Recording is a dump, not a re-embed -- which also means the
cassette captures exactly what production retrieves against today.

Re-record whenever principle text, tags or embeddings change. The commit diff
is the audit trail: cassette changed, therefore the numbers are allowed to move.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import Book, Principle, ReviewStatus  # noqa: E402
from eval.cases import GOLDEN_PATH, load_cases  # noqa: E402
from eval.cassettes import CORPUS_PATH, VECTORS_PATH, save_corpus  # noqa: E402
from scripts.retag_principles import _engine  # noqa: E402

# Rows per vector fetch. 50 x ~20KB = ~1MB per result set, comfortably
# inside what the Supabase pooler will deliver without dropping.
CHUNK = 50


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database-url", required=True)
    ap.add_argument(
        "--book-id",
        action="append",
        help="record only these books (default: every book the golden set uses)",
    )
    args = ap.parse_args()

    book_ids = args.book_id or sorted({c.book_id for c in load_cases(GOLDEN_PATH)})
    if not book_ids:
        print("no books to record", file=sys.stderr)
        return 1
    print(f"recording {len(book_ids)} book(s)")

    engine = _engine(args.database_url)
    books: list[dict] = []
    principles: list[dict] = []
    vectors: dict[str, list[float]] = {}
    try:
        with Session(engine) as db:
            for book_id in book_ids:
                book = db.get(Book, book_id)
                if book is None:
                    print(f"  !! no such book: {book_id}", file=sys.stderr)
                    return 1
                books.append(
                    {
                        "book_id": book.book_id,
                        "title": book.title,
                        "author": book.author,
                        "core_thesis": book.core_thesis,
                        "tone": book.tone.value if hasattr(book.tone, "value") else book.tone,
                        "tracked_metrics": book.tracked_metrics,
                    }
                )
                # Two passes, and vectors fetched CHUNK at a time. pgvector
                # serializes a 1024-dim vector as text, ~20KB per row, so even
                # a single book (203 principles = 4MB in one result set) is
                # enough to drop the Supabase pooler mid-read. The id list is
                # cheap; the vectors are what has to be paginated.
                ids = list(
                    db.scalars(
                        select(Principle.principle_id)
                        .where(
                            Principle.book_id == book_id,
                            Principle.review_status == ReviewStatus.human_reviewed,
                            Principle.embedding.is_not(None),
                        )
                        .order_by(Principle.principle_id)
                    ).all()
                )
                rows = []
                for start in range(0, len(ids), CHUNK):
                    rows.extend(
                        db.execute(
                            select(
                                Principle.principle_id,
                                Principle.name,
                                Principle.summary,
                                Principle.source_chapter,
                                Principle.applies_to_tags,
                                Principle.embedding,
                            ).where(Principle.principle_id.in_(ids[start : start + CHUNK]))
                        ).all()
                    )
                for pid, name, summary, chapter, tags, embedding in rows:
                    principles.append(
                        {
                            "principle_id": pid,
                            "book_id": book_id,
                            "name": name,
                            "summary": summary,
                            "source_chapter": chapter,
                            "applies_to_tags": list(tags or []),
                        }
                    )
                    vectors[pid] = [float(x) for x in embedding]
                print(f"  {len(rows):5d}  {book_id}")
    finally:
        engine.dispose()

    corpus_bytes, vector_bytes = save_corpus(books, principles, vectors)
    print(
        f"\n{len(principles)} principles, {len(books)} books\n"
        f"  {CORPUS_PATH.name:28} {corpus_bytes / 1e6:5.2f} MB\n"
        f"  {VECTORS_PATH.name:28} {vector_bytes / 1e6:5.2f} MB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
