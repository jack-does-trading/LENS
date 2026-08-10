from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tqdm import tqdm

from local_extraction.aggregation import aggregate_candidates_hierarchical
from local_extraction.chunking import Chunk, build_chunks, load_pdf_pages
from local_extraction.extraction import ChunkExtractionError, extract_candidates_from_chunk
from local_extraction.model_client import LocalModelClient
from local_extraction.schema import BOOK_TONES, CandidatePrinciple
from local_extraction.validation import ValidationIssue, validate_draft

logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    pdf_path: Path
    model: str
    book_id: str
    title: str
    author: str
    tone: str
    output_path: Path
    scratch_dir: Path
    max_chunk_words: int = 3000
    overlap_words: int = 150
    overwrite_output: bool = False

    def __post_init__(self) -> None:
        if self.tone not in BOOK_TONES:
            raise ValueError(f"tone must be one of {BOOK_TONES}, got {self.tone!r}")


class OutputExistsError(FileExistsError):
    """Raised when config.output_path already exists and overwrite_output is False.

    A previously-written draft may already be mid-review by a human — never
    silently clobber it. Pass overwrite_output=True (CLI: --overwrite) to
    replace it deliberately."""


class ValidationFailed(RuntimeError):
    """Raised when the aggregated draft fails validation. No output file is
    written when this is raised — the caller must fix the model/prompt and
    re-run, never patch the rejected output by hand into place."""

    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        message = "aggregated draft failed validation:\n" + "\n".join(
            f"  - {issue}" for issue in issues
        )
        super().__init__(message)


_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    slug = _SLUG_PATTERN.sub("-", text.lower()).strip("-")
    return slug or "principle"


def assign_principle_ids(principles: list[CandidatePrinciple]) -> list[str]:
    """Deterministic, deduped slug ids, one per principle, in input order."""
    seen: dict[str, int] = {}
    ids: list[str] = []
    for principle in principles:
        base = slugify(principle.name)
        count = seen.get(base, 0)
        seen[base] = count + 1
        ids.append(base if count == 0 else f"{base}-{count + 1}")
    return ids


def _write_scratch(scratch_dir: Path, name: str, content: str) -> None:
    scratch_dir.mkdir(parents=True, exist_ok=True)
    (scratch_dir / name).write_text(content, encoding="utf-8")


def _chunk_content_hash(chunk: Chunk) -> str:
    """Fingerprint of exactly what would be sent to the model for this chunk.

    Used to decide whether a cached per-chunk extraction is still valid: if
    the PDF, --max-chunk-words, --overlap-words, or the chunking strategy
    change such that this chunk's actual text differs, the hash changes too
    and the cache is correctly treated as stale — no separate "did the config
    change" bookkeeping needed.
    """
    fingerprint = f"{chunk.source_chapter or ''}\x00{chunk.text}"
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def _chunk_cache_path(scratch_dir: Path, chunk_index: int) -> Path:
    return scratch_dir / f"chunk_{chunk_index:03d}_candidates.json"


def _load_cached_candidates(cache_path: Path, expected_hash: str) -> list[CandidatePrinciple] | None:
    """Return cached candidates for this chunk if the cache file exists and
    matches expected_hash, else None (meaning: recompute). Any corrupt or
    old-format cache file is treated as a miss, never as an error."""
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("chunk_text_hash") != expected_hash:
        return None
    candidates_payload = payload.get("candidates")
    if not isinstance(candidates_payload, list):
        return None
    try:
        return [
            CandidatePrinciple(
                name=entry["name"],
                summary=entry["summary"],
                source_chapter=entry.get("source_chapter"),
                applies_to_tags=list(entry.get("applies_to_tags", [])),
            )
            for entry in candidates_payload
        ]
    except (KeyError, TypeError):
        return None


def _write_chunk_cache(
    cache_path: Path, content_hash: str, candidates: list[CandidatePrinciple]
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "chunk_text_hash": content_hash,
                "candidates": [_candidate_to_dict(c) for c in candidates],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def run_pipeline(
    config: PipelineConfig, client: LocalModelClient, show_progress: bool = False
) -> dict:
    """Run the full local extraction pipeline.

    Writes ONLY the validated draft JSON to config.output_path. Raw PDF text
    and raw per-chunk/aggregation model output are written to
    config.scratch_dir for local debugging only, and are never read by
    anything downstream of this function or referenced by the output file.

    Raises ValidationFailed (without writing config.output_path) if the
    aggregated draft violates any hard rule. Never truncates or auto-fixes
    the model's output.

    `show_progress` prints a live progress bar across all chunks (and the
    aggregation step) to stderr — off by default so library callers and tests
    get silent, deterministic behavior; the CLI turns it on.

    Re-running with an unchanged PDF/chunking config reuses each chunk's
    cached extraction from config.scratch_dir instead of re-calling the model
    (see _chunk_content_hash) — only chunks whose actual text changed (PDF,
    --max-chunk-words, --overlap-words, or chunking strategy) are
    recomputed. Raises OutputExistsError up front if config.output_path
    already exists and config.overwrite_output is False, before spending any
    time on chunking/extraction/aggregation.
    """
    if config.output_path.exists() and not config.overwrite_output:
        raise OutputExistsError(
            f"{config.output_path} already exists. A previously-written draft may "
            "still be under human review — refusing to overwrite it silently. "
            "Pass overwrite_output=True (CLI: --overwrite) if you mean to replace it."
        )

    page_texts, toc_entries = load_pdf_pages(config.pdf_path)
    _write_scratch(config.scratch_dir, "raw_pdf_text.txt", "\n\n".join(page_texts))

    chunks, chunking_report = build_chunks(
        page_texts,
        toc_entries=toc_entries,
        max_chunk_words=config.max_chunk_words,
        overlap_words=config.overlap_words,
    )
    logger.info(
        "chunking strategy=%s reason=%s chapters=%d final_chunks=%d",
        chunking_report.strategy,
        chunking_report.reason,
        chunking_report.chapter_count,
        chunking_report.final_chunk_count,
    )

    all_candidates: list[CandidatePrinciple] = []
    reused_count = 0
    failed_chunks: list[int] = []
    progress = tqdm(
        chunks,
        desc="Extracting principles",
        unit="chunk",
        disable=not show_progress,
    )
    for chunk in progress:
        progress.set_postfix_str((chunk.source_chapter or f"chunk {chunk.index}")[:40])
        cache_path = _chunk_cache_path(config.scratch_dir, chunk.index)
        content_hash = _chunk_content_hash(chunk)
        cached_candidates = _load_cached_candidates(cache_path, content_hash)
        if cached_candidates is not None:
            logger.info("chunk %d: reusing cached extraction (content unchanged)", chunk.index)
            candidates = cached_candidates
            reused_count += 1
        else:
            try:
                candidates = extract_candidates_from_chunk(client, config.title, config.author, chunk)
            except ChunkExtractionError as exc:
                # Same principle as a batch merge failure in
                # aggregate_candidates_hierarchical: one chunk's model output
                # being unrecoverably malformed (extract_candidates_from_chunk
                # already exhausted its own retries and refused to fabricate
                # or patch it) is a reason to lose that chunk's candidates,
                # not the rest of the book — 23 good chunks are worth far
                # more than aborting on the 24th. No cache entry is written,
                # so a future re-run retries this chunk fresh rather than
                # being stuck replaying a failure that never happened.
                failed_chunks.append(chunk.index)
                logger.warning(
                    "chunk %d: extraction failed after exhausting retries (%s); "
                    "skipping — no candidates drawn from this chunk",
                    chunk.index,
                    exc,
                )
                continue
            _write_chunk_cache(cache_path, content_hash, candidates)
        all_candidates.extend(candidates)
    progress.close()
    if show_progress and reused_count:
        tqdm.write(f"Reused {reused_count}/{len(chunks)} cached chunk extractions (content unchanged).")
    if failed_chunks:
        logger.warning(
            "%d/%d chunks skipped after exhausting extraction retries: %s",
            len(failed_chunks),
            len(chunks),
            failed_chunks,
        )
        if show_progress:
            tqdm.write(
                f"WARNING: {len(failed_chunks)}/{len(chunks)} chunks skipped after exhausting "
                f"extraction retries (chunk indices: {failed_chunks}) — no candidates drawn "
                "from them. Re-run later to retry just these chunks (already-cached ones "
                "won't be re-sent)."
            )

    if show_progress:
        tqdm.write(f"Aggregating {len(all_candidates)} candidate principles...")
    draft = aggregate_candidates_hierarchical(
        client, config.title, config.author, all_candidates, show_progress=show_progress
    )
    if show_progress:
        tqdm.write("Aggregation done; validating draft...")
    _write_scratch(
        config.scratch_dir,
        "aggregated_draft_raw.json",
        json.dumps(
            {
                "core_thesis": draft.core_thesis,
                "principles": [_candidate_to_dict(p) for p in draft.principles],
            },
            indent=2,
        ),
    )

    issues = validate_draft(draft)
    if issues:
        raise ValidationFailed(issues)

    principle_ids = assign_principle_ids(draft.principles)
    output_payload = {
        "book": {
            "book_id": config.book_id,
            "title": config.title,
            "author": config.author,
            "core_thesis": draft.core_thesis,
            "tone": config.tone,
            # Left for the human reviewer to define — a local model shouldn't
            # invent which dimensions the user's streaks should track.
            "tracked_metrics": [],
            "review_status": "pending_review",
            "extraction_method": "local_model",
            "version": 1,
        },
        "principles": [
            {
                "principle_id": principle_id,
                "book_id": config.book_id,
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
            "chunking_strategy": chunking_report.strategy,
            "chunking_reason": chunking_report.reason,
            "chapter_count": chunking_report.chapter_count,
            "chunk_count": chunking_report.final_chunk_count,
            "model": config.model,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    config.output_path.parent.mkdir(parents=True, exist_ok=True)
    config.output_path.write_text(json.dumps(output_payload, indent=2), encoding="utf-8")
    logger.info("wrote validated draft to %s", config.output_path)
    return output_payload


def _candidate_to_dict(candidate: CandidatePrinciple) -> dict:
    return {
        "name": candidate.name,
        "summary": candidate.summary,
        "source_chapter": candidate.source_chapter,
        "applies_to_tags": candidate.applies_to_tags,
    }
