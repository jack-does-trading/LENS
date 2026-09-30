#!/usr/bin/env python3
"""Rewrite golden-set entry text so eval/cases/golden.jsonl can be committed.

    python scripts/redact_eval_cases.py --dry-run
    python scripts/redact_eval_cases.py

Backs up the current file to eval/backups/ first (gitignored), rewrites each
entry's action text with one LLM call, and checks every rewrite for verbatim
leakage before writing anything.

It does NOT touch expected_principle_ids: the labels are the expensive part and
the rewrite is supposed to preserve the meaning they were assigned to. Verify
that afterwards with

    python scripts/measure_retrieval.py --database-url ... --sweep

and compare against the pre-redaction numbers. A large drift means the rewrites
changed the situations, not that retrieval changed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.cases import GOLDEN_PATH, load_cases, save_cases  # noqa: E402
from eval.redact import PROMPT_VERSION, RedactionError, rewrite_entry  # noqa: E402
from scripts.label_eval_cases import _build_client  # noqa: E402

BACKUP_DIR = Path(__file__).resolve().parents[1] / "eval" / "backups"
# 3 words can be an unavoidable collision ("i feel like"); 4+ consecutive words
# in common means the model echoed rather than rewrote.
MAX_SHARED_PHRASE = 3
# Keep the best of a few samples; an echo is usually one unlucky generation.
REWRITE_ATTEMPTS = 4


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--provider", choices=["groq", "ollama"], default=None)
    ap.add_argument("--force", action="store_true", help="write even if a rewrite echoes the input")
    args = ap.parse_args()

    cases = load_cases(GOLDEN_PATH)
    todo = [c for c in cases if c.redaction != "redacted"]
    if not todo:
        print("every case is already redacted")
        return 0
    print(f"{len(todo)} of {len(cases)} cases to rewrite\n")

    client = _build_client(args.provider)
    suspicious: list[str] = []
    for case in todo:
        print(f"{case.case_id}  [{case.book_id}]")
        new_entries = []
        for entry in case.entries:
            action = entry.get("action", "")
            category = entry.get("category", "")
            if not action.strip():
                new_entries.append(entry)
                continue
            # Retry an echoed rewrite rather than failing the run or forcing it
            # through: generation is stochastic, so the usual cause is one
            # unlucky sample, and asking again is cheaper than a human pass.
            rw = None
            for attempt in range(1, REWRITE_ATTEMPTS + 1):
                try:
                    candidate = rewrite_entry(client, action, category)
                except RedactionError as exc:
                    print(f"  !! {exc}")
                    return 1
                if rw is None or candidate.longest_shared_phrase < rw.longest_shared_phrase:
                    rw = candidate
                if rw.longest_shared_phrase <= MAX_SHARED_PHRASE:
                    break
                if attempt < REWRITE_ATTEMPTS:
                    print(f"     retry {attempt}: {candidate.longest_shared_phrase} words shared")
            assert rw is not None
            shared = rw.longest_shared_phrase
            flag = "" if shared <= MAX_SHARED_PHRASE else f"  <-- {shared} words shared"
            if flag:
                suspicious.append(case.case_id)
            print(f"  -  {action[:72]}")
            print(f"  +  {rw.rewritten[:72]}{flag}")
            new_entries.append({**entry, "action": rw.rewritten})
        if not args.dry_run:
            case.entries = new_entries
            case.redaction = "redacted"
            case.label_notes = (case.label_notes + f" [rewritten {PROMPT_VERSION}]").strip()
        print()

    if suspicious and not args.force:
        print(f"refusing to write: {len(suspicious)} rewrite(s) share more than "
              f"{MAX_SHARED_PHRASE} consecutive words with the original: "
              f"{', '.join(sorted(set(suspicious)))}")
        print("re-run to try again, or --force if you have read them and they are fine")
        return 1

    if args.dry_run:
        print("dry run -- nothing written")
        return 0

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_DIR / f"golden_verbatim_{stamp}.jsonl"
    backup.write_text(GOLDEN_PATH.read_text())
    save_cases(GOLDEN_PATH, cases)
    print(f"backup  -> {backup}")
    print(f"written -> {GOLDEN_PATH}  ({len(cases)} cases, all redacted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
