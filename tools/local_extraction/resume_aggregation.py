"""One-off resume script: reuses already-extracted chunk candidates from
scratch/ (from a prior run that got through all per-chunk extraction but
failed at aggregation) instead of re-running 48 extraction calls.

Not part of the permanent CLI surface — a throwaway utility for this
specific interrupted run.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from local_extraction.aggregation import aggregate_candidates
from local_extraction.model_client import OllamaModelClient
from local_extraction.pipeline import ValidationFailed, assign_principle_ids
from local_extraction.schema import CandidatePrinciple
from local_extraction.validation import validate_draft

BOOK_ID = "six-pillars-of-self-esteem"
TITLE = "The Six Pillars of Self-Esteem"
AUTHOR = "Nathaniel Branden"
TONE = "pragmatic"
MODEL = "llama3.2:3b"

scratch = Path("scratch") / BOOK_ID
output_path = Path("output") / f"{BOOK_ID}.json"

files = sorted(scratch.glob("chunk_*_candidates.json"))
print(f"reusing {len(files)} already-extracted chunk files from {scratch}")

all_candidates: list[CandidatePrinciple] = []
for f in files:
    for entry in json.loads(f.read_text()):
        all_candidates.append(
            CandidatePrinciple(
                name=entry["name"],
                summary=entry["summary"],
                source_chapter=entry.get("source_chapter"),
                applies_to_tags=entry.get("applies_to_tags", []),
            )
        )
print(f"reconstructed {len(all_candidates)} candidate principles")

client = OllamaModelClient(model=MODEL)

print("running aggregation pass (this may take a while)...")
try:
    draft = aggregate_candidates(client, TITLE, AUTHOR, all_candidates)
except Exception as exc:
    print(f"aggregation failed: {exc}", file=sys.stderr)
    raise

(scratch / "aggregated_draft_raw.json").write_text(
    json.dumps(
        {
            "core_thesis": draft.core_thesis,
            "principles": [
                {
                    "name": p.name,
                    "summary": p.summary,
                    "source_chapter": p.source_chapter,
                    "applies_to_tags": p.applies_to_tags,
                }
                for p in draft.principles
            ],
        },
        indent=2,
    ),
    encoding="utf-8",
)

issues = validate_draft(draft)
if issues:
    print("REJECTED — aggregated draft failed validation, nothing written to output:", file=sys.stderr)
    for issue in issues:
        print(f"  - {issue}", file=sys.stderr)
    raise ValidationFailed(issues)

principle_ids = assign_principle_ids(draft.principles)
output_payload = {
    "book": {
        "book_id": BOOK_ID,
        "title": TITLE,
        "author": AUTHOR,
        "core_thesis": draft.core_thesis,
        "tone": TONE,
        "tracked_metrics": [],
        "review_status": "pending_review",
        "extraction_method": "local_model",
        "version": 1,
    },
    "principles": [
        {
            "principle_id": principle_id,
            "book_id": BOOK_ID,
            "name": principle.name,
            "summary": principle.summary,
            "source_chapter": principle.source_chapter,
            "applies_to_tags": principle.applies_to_tags,
            "embedding_id": None,
            "review_status": "pending_review",
        }
        for principle_id, principle in zip(principle_ids, draft.principles)
    ],
    "extraction_metadata": {
        "chunking_strategy": "toc",
        "chunking_reason": "embedded PDF TOC yielded 21 chapter-level entries",
        "chapter_count": 21,
        "chunk_count": len(files),
        "model": MODEL,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    },
}

output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text(json.dumps(output_payload, indent=2), encoding="utf-8")
print(f"wrote validated draft to {output_path}")
