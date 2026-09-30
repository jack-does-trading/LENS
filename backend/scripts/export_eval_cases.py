#!/usr/bin/env python3
"""Mine the golden set's raw material out of a Lens database.

    python scripts/export_eval_cases.py --database-url postgresql://...

Writes unlabelled cases to `eval/cases/staging.jsonl` (gitignored). Labelling
them -- deciding which principles *should* have surfaced -- is a separate,
human step: `python scripts/label_eval_cases.py`.

`--database-url` is required and has no default on purpose. `scripts/mark_reviewed.py`
reads `settings.database_url` implicitly, which in this repo points at the live
Supabase instance; a tool that silently reaches production because someone
forgot a flag is the failure mode that has already cost this project once.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import Analysis, DailyLog  # noqa: E402
from eval.cases import (  # noqa: E402
    GOLDEN_PATH,
    STAGING_PATH,
    EvalCase,
    case_id_for,
    hash_log_id,
    load_cases,
    save_cases,
)


def export(database_url: str, limit: int | None = None) -> tuple[int, int]:
    """Returns (newly staged, skipped as already known)."""
    already = {c.source_log_id_hash for c in load_cases(GOLDEN_PATH)}
    already |= {c.source_log_id_hash for c in load_cases(STAGING_PATH)}
    staged = load_cases(STAGING_PATH)

    engine = create_engine(database_url)
    new = 0
    with Session(engine) as db:
        stmt = select(DailyLog).order_by(DailyLog.created_at)
        if limit:
            stmt = stmt.limit(limit)
        for log in db.scalars(stmt):
            if not log.entries:
                # No entries means nothing for retrieval to match on; such a
                # log can't discriminate between a good and a bad retriever.
                continue
            log_hash = hash_log_id(log.log_id)
            if log_hash in already:
                continue
            # Only the one column, not the whole Analysis entity: this script
            # is pointed at databases that may lag the ORM (production sits on
            # the previous migration until the next deploy), and an exporter
            # that breaks on an unrelated new column is an exporter you can't
            # use when you need it.
            retrieved = db.scalar(
                select(Analysis.retrieved_principle_ids).where(Analysis.log_id == log.log_id)
            )
            staged.append(
                EvalCase(
                    case_id=case_id_for(log_hash),
                    source_log_id_hash=log_hash,
                    book_id=log.chosen_book_id,
                    entries=list(log.entries),
                    mood=log.mood,
                    retrieved_at_export=list(retrieved or []),
                )
            )
            new += 1
    engine.dispose()

    save_cases(STAGING_PATH, staged)
    return new, len(already)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, help="SQLAlchemy URL to export from")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    new, skipped = export(args.database_url, args.limit)
    print(f"staged {new} new case(s) to {STAGING_PATH.relative_to(Path.cwd())}")
    print(f"skipped {skipped} already exported")
    if new:
        print("\nNext: python scripts/label_eval_cases.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
