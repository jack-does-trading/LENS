"""Submit a local-extraction draft JSON to the ingestion endpoint, and
optionally mark it human_reviewed afterward.

The draft JSON is the {"book": {...}, "principles": [...], "extraction_metadata": {...}}
shape tools/local_extraction writes to its output/ directory. This script
flattens it into the shape POST /api/ingestion/draft-submissions expects and
sends it, then (only if --mark-reviewed is passed) flips review_status to
human_reviewed directly in the DB, since no review/approval endpoint exists
yet.

--mark-reviewed is NOT implied by running this script or by --replace. Pass
it only after you have actually read the book's chapters and checked the
draft's principles against them (architecture Sec 2's mandatory human review
gate) -- never as a default/automatic step.

Usage:
    python scripts/submit_local_draft.py <path-to-draft.json> [--replace] [--mark-reviewed] [--base-url URL]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.models import Book, Principle, ReviewStatus  # noqa: E402


def flatten_draft(draft_path: Path) -> dict:
    raw = json.loads(draft_path.read_text(encoding="utf-8"))
    book = raw["book"]
    return {
        "book_id": book["book_id"],
        "title": book["title"],
        "author": book["author"],
        "core_thesis": book["core_thesis"],
        "tone": book["tone"],
        "tracked_metrics": book.get("tracked_metrics", []),
        "extraction_method": book["extraction_method"],
        "principles": [
            {
                "principle_id": p["principle_id"],
                "name": p["name"],
                "summary": p["summary"],
                "source_chapter": p.get("source_chapter"),
                "applies_to_tags": p.get("applies_to_tags", []),
            }
            for p in raw["principles"]
        ],
    }


def submit(payload: dict, base_url: str, api_key: str, replace: bool) -> dict:
    url = f"{base_url}/api/ingestion/draft-submissions"
    if replace:
        url += "?replace=true"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Ingestion-Api-Key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        print(f"Submission failed: HTTP {exc.code}\n{body}", file=sys.stderr)
        sys.exit(1)


def mark_reviewed(book_id: str) -> int:
    """Flip review_status to human_reviewed for a book and all its principles,
    then (re)generate embeddings for those principles -- architecture SS2's
    Publish step: "review_status flips to human_reviewed, a version
    increments, embeddings are (re)generated ... and the entry becomes
    retrievable." Returns the number of principles embedded.
    """
    from app.database import SessionLocal
    from app.embeddings import VoyageEmbeddingClient, generate_embeddings_for_book

    if not settings.voyage_api_key:
        raise ValueError("VOYAGE_API_KEY is not set (check backend/.env).")

    with SessionLocal() as db:
        book = db.get(Book, book_id)
        if book is None:
            raise ValueError(f"no book found with book_id={book_id!r}; nothing was changed")

        book.review_status = ReviewStatus.human_reviewed
        book.version += 1
        db.query(Principle).filter(Principle.book_id == book_id).update(
            {"review_status": ReviewStatus.human_reviewed.value}
        )
        db.flush()

        client = VoyageEmbeddingClient(api_key=settings.voyage_api_key, model=settings.voyage_model)
        embedded_count = generate_embeddings_for_book(db, book_id, client)
        db.commit()
        return embedded_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("draft_path", type=Path)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument(
        "--mark-reviewed",
        action="store_true",
        help="Only pass this after you've actually reviewed the draft against the book.",
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()

    if not settings.ingestion_api_key:
        print("INGESTION_API_KEY is not set (check backend/.env).", file=sys.stderr)
        sys.exit(1)

    payload = flatten_draft(args.draft_path)
    result = submit(payload, args.base_url, settings.ingestion_api_key, args.replace)
    book = result["book"]
    print(f"Submitted {book['book_id']!r}: {len(result['principles'])} principles, "
          f"review_status={book['review_status']!r}, version={book['version']}")

    if args.mark_reviewed:
        embedded_count = mark_reviewed(book["book_id"])
        print(
            f"Marked {book['book_id']!r} as human_reviewed (version incremented), "
            f"{embedded_count} principle(s) embedded."
        )


if __name__ == "__main__":
    main()
