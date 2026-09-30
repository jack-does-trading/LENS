"""Re-embed every principle in the given books.

Needed after scripts/retag_principles.py: embeddings.py builds its embedding
text as f"{name}. {summary} Tags: {tags}", so any book whose tags were
placeholders has that placeholder text baked into its vectors. Retagging
without re-embedding leaves the embedding arm of retrieval scoring against
"tag-one".

Takes --database-url explicitly rather than reading settings.database_url:
this writes to production, and a script that silently picks up whatever is in
.env is the footgun scripts/mark_reviewed.py already has.
"""

import argparse
import sys
import time

from sqlalchemy.orm import Session

sys.path.insert(0, ".")

from app.config import settings  # noqa: E402
from app.embeddings import VoyageEmbeddingClient, generate_embeddings_for_book  # noqa: E402
from app.models import Principle  # noqa: E402
from scripts.retag_principles import _engine  # noqa: E402

# The four books that migration-era extraction left with placeholder tags.
DEFAULT_BOOKS = [
    "12-rules-for-life",
    "six-pillars-of-self-esteem",
    "atomic-habits",
    "never-split-the-difference",
]


class ProgressClient(VoyageEmbeddingClient):
    """VoyageEmbeddingClient that narrates each batch.

    embed() only returns once a whole book is done, and _post_batch sleeps
    ~20s between requests to respect the 3 RPM free-tier cap, so without this
    a correct run is indistinguishable from a hung one for minutes at a time.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.batch_no = 0

    def _post_batch(self, texts: list[str], input_type: str) -> list[list[float]]:
        self.batch_no += 1
        started = time.monotonic()
        print(f"    batch {self.batch_no:>2} ({len(texts):>3} texts) ...", end="", flush=True)
        vectors = super()._post_batch(texts, input_type)
        print(f" ok  {time.monotonic() - started:5.1f}s", flush=True)
        return vectors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--books", nargs="+", default=DEFAULT_BOOKS)
    args = parser.parse_args()

    if not settings.voyage_api_key:
        print("VOYAGE_API_KEY is not set", file=sys.stderr)
        return 1

    engine = _engine(args.database_url)
    client = ProgressClient(api_key=settings.voyage_api_key, model=settings.voyage_model)
    started = time.monotonic()
    total = 0
    try:
        with Session(engine) as db:
            for book_id in args.books:
                count = (
                    db.query(Principle).filter(Principle.book_id == book_id).count()
                )
                batches = -(-count // 50)
                print(f"\n{book_id}: {count} principles, {batches} batches", flush=True)
                embedded = generate_embeddings_for_book(db, book_id, client)
                # Commit per book, not once at the end: a dropped Supabase
                # connection mid-run then costs one book, not the whole run,
                # and a re-run is safe because this overwrites unconditionally.
                db.commit()
                total += embedded
                print(f"  committed {embedded} principles", flush=True)
    finally:
        engine.dispose()

    print(f"\ndone: {total} principles in {time.monotonic() - started:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
