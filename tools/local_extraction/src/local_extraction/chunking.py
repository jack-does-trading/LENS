from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHUNK_WORDS = 3000
DEFAULT_OVERLAP_WORDS = 150

# Below this many detected boundaries, a strategy isn't considered reliable
# enough to trust over the next fallback.
MIN_TOC_CHAPTERS = 2
MIN_HEADING_CHAPTERS = 2

_HEADING_PATTERN = re.compile(
    r"^\s*(chapter|part)\s+([0-9]+|[ivxlcdm]+)\b",
    re.IGNORECASE,
)

_WHITESPACE_RUN_PATTERN = re.compile(r"\s+")


def _sanitize_title(title: str) -> str:
    """Clean a chapter/part title pulled from a PDF's embedded TOC or a
    detected heading line.

    Real-world PDFs have been observed embedding literal carriage-return
    characters inside bookmark titles — sometimes trailing, sometimes mid-word
    (e.g. "The Practice of L\\riving Purposefully"), a PDF-authoring artifact,
    not anything this tool or the model introduced. Deleting stray whitespace
    control characters outright (not replacing with a space) is correct for
    both cases: it fixes the trailing case and reassembles the split word.
    Left uncleaned, this title flows through to every downstream principle's
    source_chapter field, and — since a local model doesn't reliably preserve
    an exact string verbatim across separate calls — the same chapter can end
    up split across two "different" labels in the final output.
    """
    return _WHITESPACE_RUN_PATTERN.sub(" ", title.replace("\r", "").replace("\n", "")).strip()


@dataclass(frozen=True)
class Chunk:
    text: str
    source_chapter: str | None
    index: int


@dataclass(frozen=True)
class ChunkingReport:
    strategy: str
    reason: str
    chapter_count: int
    final_chunk_count: int


def chunk_fixed_size(
    text: str,
    max_words: int = DEFAULT_MAX_CHUNK_WORDS,
    overlap_words: int = DEFAULT_OVERLAP_WORDS,
) -> list[str]:
    """Split `text` into overlapping fixed-size word windows.

    Used as the last-resort chunking strategy when neither an embedded TOC
    nor detectable chapter headings are reliable enough to chunk by chapter.
    """
    if overlap_words >= max_words:
        raise ValueError("overlap_words must be smaller than max_words")

    words = text.split()
    if not words:
        return []

    windows: list[str] = []
    step = max_words - overlap_words
    start = 0
    while start < len(words):
        window = words[start : start + max_words]
        windows.append(" ".join(window))
        if start + max_words >= len(words):
            break
        start += step
    return windows


def detect_headings(page_texts: list[str]) -> list[tuple[str, int]]:
    """Return (heading_title, page_index) pairs found via a chapter/part heading regex.

    page_index is 0-based, indexing into page_texts. This is the fallback
    boundary-detection strategy used when the PDF has no usable embedded TOC.
    """
    headings: list[tuple[str, int]] = []
    for page_index, page_text in enumerate(page_texts):
        for line in page_text.splitlines():
            stripped = line.strip()
            if _HEADING_PATTERN.match(stripped):
                headings.append((_sanitize_title(stripped), page_index))
                break
    return headings


def chunks_from_page_ranges(
    page_texts: list[str],
    boundaries: list[tuple[str, int]],
) -> list[Chunk]:
    """Build one chunk per chapter, spanning from its start page up to (but not
    including) the next chapter's start page, given (title, start_page_index)
    boundaries.
    """
    chunks: list[Chunk] = []
    for i, (title, start_page) in enumerate(boundaries):
        end_page = boundaries[i + 1][1] if i + 1 < len(boundaries) else len(page_texts)
        chapter_text = "\n".join(page_texts[start_page:end_page]).strip()
        if chapter_text:
            chunks.append(Chunk(text=chapter_text, source_chapter=title, index=i))
    return chunks


def resegment_oversized_chunks(
    chunks: list[Chunk],
    max_chunk_words: int,
    overlap_words: int,
) -> list[Chunk]:
    """Split any chunk whose text exceeds max_chunk_words into smaller
    overlapping sub-chunks, so no single model call ever has to process an
    entire (possibly very long) chapter at once.

    This matters independently of chunking *strategy*: a TOC- or heading-
    detected "chapter" can still legitimately span many thousands of words,
    and architecture §2 calls for keeping "each individual model call scoped
    to a small span of source text rather than the whole work" — not just
    "smaller than the whole book." It's also a practical safety net on
    memory-constrained hardware, where a very large single call would force
    a correspondingly large model context window.

    Chunks already within the limit pass through unchanged. Sub-chunks of the
    same source chapter keep that chapter's label (not split into "part N"
    labels) — reviewers see chapter-level provenance, not sub-chunk boundaries.
    `index` is renumbered sequentially across the returned list.
    """
    resegmented: list[Chunk] = []
    for chunk in chunks:
        if len(chunk.text.split()) <= max_chunk_words:
            resegmented.append(chunk)
            continue
        for window in chunk_fixed_size(chunk.text, max_chunk_words, overlap_words):
            resegmented.append(Chunk(text=window, source_chapter=chunk.source_chapter, index=-1))

    return [
        Chunk(text=c.text, source_chapter=c.source_chapter, index=i)
        for i, c in enumerate(resegmented)
    ]


def build_chunks(
    page_texts: list[str],
    toc_entries: list[tuple[str, int]] | None = None,
    max_chunk_words: int = DEFAULT_MAX_CHUNK_WORDS,
    overlap_words: int = DEFAULT_OVERLAP_WORDS,
) -> tuple[list[Chunk], ChunkingReport]:
    """Chunk a book's page texts, in order of preference:

    1. Embedded PDF TOC (most reliable — publisher-provided chapter boundaries).
    2. Regex-detected "Chapter N" / "Part N" headings in the page text.
    3. Fixed-size overlapping word windows (chunking is required per architecture
       §2 — every book gets chunked one way or another, never sent whole).

    Whichever strategy wins, any resulting chunk still larger than
    max_chunk_words is further resegmented (see resegment_oversized_chunks) —
    a "chapter" boundary is a starting point, not a license to send an entire
    long chapter to the model in one call.

    Returns the chunk list plus a report recording which strategy was used and
    why, so the caller can log it (per the task's "log which strategy was
    used" requirement).
    """
    if toc_entries and len(toc_entries) >= MIN_TOC_CHAPTERS:
        chunks = chunks_from_page_ranges(page_texts, toc_entries)
        if chunks:
            final_chunks = resegment_oversized_chunks(chunks, max_chunk_words, overlap_words)
            report = ChunkingReport(
                strategy="toc",
                reason=f"embedded PDF TOC yielded {len(toc_entries)} chapter-level entries",
                chapter_count=len(chunks),
                final_chunk_count=len(final_chunks),
            )
            logger.info(
                "chunking strategy=%s reason=%r chapters=%d final_chunks=%d",
                report.strategy, report.reason, len(chunks), len(final_chunks),
            )
            return final_chunks, report

    headings = detect_headings(page_texts)
    if len(headings) >= MIN_HEADING_CHAPTERS:
        chunks = chunks_from_page_ranges(page_texts, headings)
        if chunks:
            final_chunks = resegment_oversized_chunks(chunks, max_chunk_words, overlap_words)
            report = ChunkingReport(
                strategy="heading_regex",
                reason=(
                    "no usable embedded TOC; regex heading detection found "
                    f"{len(headings)} chapter/part headings"
                ),
                chapter_count=len(chunks),
                final_chunk_count=len(final_chunks),
            )
            logger.info(
                "chunking strategy=%s reason=%r chapters=%d final_chunks=%d",
                report.strategy, report.reason, len(chunks), len(final_chunks),
            )
            return final_chunks, report

    full_text = "\n".join(page_texts)
    windows = chunk_fixed_size(full_text, max_chunk_words, overlap_words)
    chunks = [
        Chunk(text=window, source_chapter=None, index=i) for i, window in enumerate(windows)
    ]
    report = ChunkingReport(
        strategy="fixed_size_overlap",
        reason=(
            "no reliable embedded TOC or chapter headings found; falling back to "
            f"fixed-size chunks of {max_chunk_words} words with {overlap_words}-word overlap"
        ),
        chapter_count=len(chunks),
        final_chunk_count=len(chunks),
    )
    logger.info(
        "chunking strategy=%s reason=%r chapters=%d final_chunks=%d",
        report.strategy, report.reason, len(chunks), len(chunks),
    )
    return chunks, report


def select_chapter_level_entries(
    toc_all_levels: list[tuple[int, str, int]],
) -> list[tuple[str, int]]:
    """Pick the TOC level that best represents chapter-sized boundaries.

    Many publisher TOCs nest Part (level 1) > Chapter (level 2) > subsection
    (level 3+). Chunking by level 1 in that case would span whole parts
    (several chapters at once) rather than one chapter at a time, which is
    the granularity architecture §2 actually calls for ("chapter-by-chapter
    or section-by-section"). This picks the *deepest* level that still has
    enough entries to be trusted as real chapter boundaries, falling back to
    shallower levels if the deepest one is too sparse (e.g. a lone appendix
    subsection).
    """
    if not toc_all_levels:
        return []
    levels_present = sorted({level for level, _, _ in toc_all_levels}, reverse=True)
    for level in levels_present:
        entries = [
            (_sanitize_title(title), max(page_1_indexed - 1, 0))
            for lvl, title, page_1_indexed in toc_all_levels
            if lvl == level
        ]
        if len(entries) >= MIN_TOC_CHAPTERS:
            return entries
    return []


def load_pdf_pages(pdf_path: Path) -> tuple[list[str], list[tuple[str, int]]]:
    """Load a PDF's per-page text and its best-guess chapter-level TOC entries.

    Returns (page_texts, toc_entries); toc_entries is a list of
    (title, 0-based page_index) at whichever TOC level looks most like actual
    chapters (see select_chapter_level_entries).

    The `fitz` (PyMuPDF) import is local to this function so the rest of the
    module — and everything that depends only on `build_chunks`/`chunk_fixed_size`
    — stays importable and unit-testable on a machine without PyMuPDF installed.
    """
    import fitz  # PyMuPDF

    document = fitz.open(pdf_path)
    try:
        page_texts = [page.get_text() for page in document]
        toc = document.get_toc(simple=True)  # [[level, title, page_1_indexed], ...]
        toc_entries = select_chapter_level_entries(
            [(level, title, page_1_indexed) for level, title, page_1_indexed in toc]
        )
        return page_texts, toc_entries
    finally:
        document.close()
