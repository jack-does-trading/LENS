#!/usr/bin/env python3
"""How often does the tag-match arm actually fire on real logged situations?

    python scripts/measure_tag_coverage.py --database-url ...

Read-only. Run it before and after scripts/retag_principles.py -- this is the
number that says whether the retag worked.

Background: retrieval fuses tag matching (weighted 2x, because an author-assigned
tag is precise) with embedding similarity (weighted 1x). Measured against the 16
real situations in eval/cases/staging.jsonl, the tag arm fired on 1 of 16 entries,
because 47% of published principles carried the literal placeholders `tag-one`,
`tag-two` from the extraction prompt's example schema. For those books retrieval
is embedding-only and the 2x weighting never applies.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from eval.cases import GOLDEN_PATH, STAGING_PATH, load_cases  # noqa: E402
from eval.tagging import PLACEHOLDER  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database-url", required=True)
    args = ap.parse_args()

    # Both files: tag coverage is about the categories users typed, which has
    # nothing to do with whether a case has been labelled yet.
    cases = load_cases(GOLDEN_PATH) + load_cases(STAGING_PATH)
    if not cases:
        print("no cases; run scripts/export_eval_cases.py first")
        return 1

    engine = create_engine(args.database_url)
    with engine.connect() as c:
        rows = c.execute(text(
            "select book_id, lower(trim(tag)) from principles, unnest(applies_to_tags) tag "
            "where review_status = 'human_reviewed'")).all()
    engine.dispose()

    tags_by_book: dict[str, set[str]] = {}
    for book, tag in rows:
        tags_by_book.setdefault(book, set()).add(tag)

    placeholders = sum(1 for _, t in rows if PLACEHOLDER.match(t))
    print(f"corpus: {len(rows)} tag assignments, {len(set(t for _, t in rows))} distinct")
    print(f"        {placeholders} placeholders ({placeholders / len(rows):.0%})\n")

    hits = total = 0
    missed: Counter[str] = Counter()
    for case in cases:
        for e in case.entries:
            cat = e.get("category", "").strip().lower()
            if not cat:
                continue
            total += 1
            if cat in tags_by_book.get(case.book_id, set()):
                hits += 1
            else:
                missed[f"{cat} ({case.book_id})"] += 1

    print(f"tag arm fires on {hits}/{total} real entries ({hits / total:.0%})")
    if missed:
        print("\nstill missing:")
        for label, n in missed.most_common(12):
            print(f"  {n}x  {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
