"""Mark an already-submitted book (and all its principles) as human_reviewed.

Use this once a draft is already sitting in the DB (via
submit_local_draft.py or otherwise) and you've since actually read the book
and checked its principles against it -- this is the mandatory human review
gate (architecture Sec 2), not something to run automatically or as part of
submission.

Usage:
    python scripts/mark_reviewed.py <book_id>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from submit_local_draft import mark_reviewed  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("book_id")
    args = parser.parse_args()

    embedded_count = mark_reviewed(args.book_id)
    print(
        f"Marked {args.book_id!r} (and its principles) as human_reviewed, version incremented, "
        f"{embedded_count} principle(s) embedded."
    )


if __name__ == "__main__":
    main()
