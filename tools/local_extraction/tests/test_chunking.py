from __future__ import annotations

import pytest

from local_extraction.chunking import (
    Chunk,
    _sanitize_title,
    build_chunks,
    chunk_fixed_size,
    chunks_from_page_ranges,
    detect_headings,
    load_pdf_pages,
    resegment_oversized_chunks,
    select_chapter_level_entries,
)


def test_chunk_fixed_size_splits_with_overlap() -> None:
    text = " ".join(f"word{i}" for i in range(1, 101))  # 100 words
    windows = chunk_fixed_size(text, max_words=40, overlap_words=10)

    # 100 words, step = 30: windows start at 0, 30, 60 — the window starting at
    # 60 already reaches word100, so the loop stops there rather than emitting
    # a redundant tiny trailing window.
    assert len(windows) == 3
    assert windows[0].split()[0] == "word1"
    assert windows[0].split()[-1] == "word40"
    # second window overlaps the last 10 words of the first
    assert windows[1].split()[0] == "word31"
    assert windows[2].split()[-1] == "word100"


def test_chunk_fixed_size_empty_text_returns_no_chunks() -> None:
    assert chunk_fixed_size("   ", max_words=40, overlap_words=10) == []


def test_chunk_fixed_size_rejects_overlap_gte_max() -> None:
    with pytest.raises(ValueError):
        chunk_fixed_size("some text here", max_words=10, overlap_words=10)


def test_detect_headings_finds_chapter_and_part_labels() -> None:
    pages = [
        "Chapter 1\nSome opening text.",
        "Just body text, no heading here.",
        "Part II\nA new section begins.",
    ]
    headings = detect_headings(pages)
    assert [title for title, _ in headings] == ["Chapter 1", "Part II"]
    assert [page for _, page in headings] == [0, 2]


def test_detect_headings_returns_empty_when_no_headings_present() -> None:
    pages = ["Just some plain narrative text.", "More plain text, still no headings."]
    assert detect_headings(pages) == []


def test_chunks_from_page_ranges_spans_to_next_boundary_or_end() -> None:
    pages = ["p0 chapter one text", "p1 more chapter one", "p2 chapter two text"]
    boundaries = [("Chapter One", 0), ("Chapter Two", 2)]

    chunks = chunks_from_page_ranges(pages, boundaries)

    assert len(chunks) == 2
    assert chunks[0].source_chapter == "Chapter One"
    assert "p0" in chunks[0].text and "p1" in chunks[0].text and "p2" not in chunks[0].text
    assert chunks[1].source_chapter == "Chapter Two"
    assert "p2" in chunks[1].text


def test_resegment_oversized_chunks_leaves_small_chunks_untouched() -> None:
    chunks = [Chunk(text="a short chapter", source_chapter="Ch 1", index=0)]

    result = resegment_oversized_chunks(chunks, max_chunk_words=100, overlap_words=10)

    assert result == chunks


def test_resegment_oversized_chunks_splits_a_long_chapter() -> None:
    long_text = " ".join(f"word{i}" for i in range(1, 251))  # 250 words
    chunks = [Chunk(text=long_text, source_chapter="Long Chapter", index=0)]

    result = resegment_oversized_chunks(chunks, max_chunk_words=100, overlap_words=10)

    assert len(result) > 1
    assert all(c.source_chapter == "Long Chapter" for c in result)
    assert all(len(c.text.split()) <= 100 for c in result)
    # index is renumbered sequentially across the whole resegmented list
    assert [c.index for c in result] == list(range(len(result)))


def test_resegment_oversized_chunks_renumbers_index_across_mixed_chunks() -> None:
    long_text = " ".join(f"word{i}" for i in range(1, 251))
    chunks = [
        Chunk(text="short one", source_chapter="Ch 1", index=0),
        Chunk(text=long_text, source_chapter="Ch 2", index=1),
        Chunk(text="short two", source_chapter="Ch 3", index=2),
    ]

    result = resegment_oversized_chunks(chunks, max_chunk_words=100, overlap_words=10)

    assert [c.index for c in result] == list(range(len(result)))
    assert result[0].source_chapter == "Ch 1"
    assert result[-1].source_chapter == "Ch 3"


def test_build_chunks_resegments_an_oversized_toc_chapter() -> None:
    long_text = " ".join(f"word{i}" for i in range(1, 251))
    pages = [long_text, "Chapter Two short text here"]
    toc_entries = [("Chapter One", 0), ("Chapter Two", 1)]

    chunks, report = build_chunks(
        pages, toc_entries=toc_entries, max_chunk_words=100, overlap_words=10
    )

    assert report.strategy == "toc"
    assert report.chapter_count == 2  # 2 chapters detected...
    assert report.final_chunk_count > 2  # ...but chapter one got split further
    assert len(chunks) == report.final_chunk_count
    assert all(len(c.text.split()) <= 100 for c in chunks)


def test_build_chunks_prefers_toc_when_available() -> None:
    pages = ["ch1 page a", "ch1 page b", "ch2 page a"]
    toc_entries = [("Chapter 1", 0), ("Chapter 2", 2)]

    chunks, report = build_chunks(pages, toc_entries=toc_entries)

    assert report.strategy == "toc"
    assert report.chapter_count == 2
    assert [c.source_chapter for c in chunks] == ["Chapter 1", "Chapter 2"]


def test_build_chunks_falls_back_to_headings_when_no_toc() -> None:
    pages = ["Chapter 1\nintro text", "Chapter 2\nsecond chapter text"]

    chunks, report = build_chunks(pages, toc_entries=None)

    assert report.strategy == "heading_regex"
    assert report.chapter_count == 2


def test_build_chunks_falls_back_to_fixed_size_when_no_structure() -> None:
    pages = [f"plain unstructured content page {i} filler" for i in range(3)]

    chunks, report = build_chunks(
        pages, toc_entries=None, max_chunk_words=6, overlap_words=1
    )

    assert report.strategy == "fixed_size_overlap"
    assert all(c.source_chapter is None for c in chunks)
    assert len(chunks) > 1


def test_build_chunks_ignores_sparse_toc_below_minimum() -> None:
    # A single TOC entry isn't enough to trust as chapter boundaries.
    pages = ["Chapter 1\nsome text", "Chapter 2\nsome more text"]
    toc_entries = [("Preface", 0)]

    chunks, report = build_chunks(pages, toc_entries=toc_entries)

    # Falls through to heading detection since TOC has only 1 entry.
    assert report.strategy == "heading_regex"


def test_sanitize_title_strips_trailing_carriage_return() -> None:
    assert _sanitize_title("Chapter 1: Some Title\r") == "Chapter 1: Some Title"


def test_sanitize_title_deletes_mid_word_carriage_return_without_inserting_space() -> None:
    # Observed in a real PDF: a carriage return landed inside a word rather
    # than at the title's edge. Deleting (not replacing with a space) is what
    # reassembles the word correctly.
    assert _sanitize_title("The Practice of L\riving Purposefully") == "The Practice of Living Purposefully"


def test_sanitize_title_collapses_internal_whitespace_runs() -> None:
    assert _sanitize_title("Chapter  1:   Some   Title") == "Chapter 1: Some Title"


def test_select_chapter_level_entries_sanitizes_titles() -> None:
    toc = [(2, "Chapter 1: Title One\r", 0), (2, "Chapter 2: Title Two\r", 5)]

    entries = select_chapter_level_entries(toc)

    assert [title for title, _ in entries] == ["Chapter 1: Title One", "Chapter 2: Title Two"]


def test_select_chapter_level_entries_prefers_deeper_level_when_populated() -> None:
    # Part (level 1) > Chapter (level 2), matching common publisher TOC nesting.
    toc = [
        (1, "Part 1", 0),
        (2, "Chapter 1", 0),
        (2, "Chapter 2", 5),
        (2, "Chapter 3", 10),
        (1, "Part 2", 15),
        (2, "Chapter 4", 15),
        (2, "Chapter 5", 20),
    ]

    entries = select_chapter_level_entries(toc)

    assert [title for title, _ in entries] == [
        "Chapter 1",
        "Chapter 2",
        "Chapter 3",
        "Chapter 4",
        "Chapter 5",
    ]


def test_select_chapter_level_entries_falls_back_when_deepest_level_too_sparse() -> None:
    # Only a single level-2 entry (e.g. one lone appendix subsection) isn't
    # enough to trust; level 1 has enough entries, so use that instead.
    toc = [
        (1, "Chapter 1", 0),
        (1, "Chapter 2", 5),
        (1, "Chapter 3", 10),
        (2, "Appendix subsection", 10),
    ]

    entries = select_chapter_level_entries(toc)

    assert [title for title, _ in entries] == ["Chapter 1", "Chapter 2", "Chapter 3"]


def test_select_chapter_level_entries_empty_toc_returns_empty() -> None:
    assert select_chapter_level_entries([]) == []


def test_load_pdf_pages_extracts_text_and_toc(pdf_with_toc) -> None:
    page_texts, toc_entries = load_pdf_pages(pdf_with_toc)

    assert len(page_texts) == 3
    assert any("Self-esteem" in t for t in page_texts)
    assert toc_entries == [("Chapter One", 0), ("Chapter Two", 2)]


def test_load_pdf_pages_picks_chapter_level_from_nested_toc(pdf_with_nested_toc) -> None:
    page_texts, toc_entries = load_pdf_pages(pdf_with_nested_toc)

    assert [title for title, _ in toc_entries] == ["Chapter One", "Chapter Two"]


def test_load_pdf_pages_no_toc_returns_empty_toc_entries(pdf_plain_no_structure) -> None:
    page_texts, toc_entries = load_pdf_pages(pdf_plain_no_structure)

    assert len(page_texts) == 3
    assert toc_entries == []


def test_end_to_end_toc_pdf_chunks_by_chapter(pdf_with_toc) -> None:
    page_texts, toc_entries = load_pdf_pages(pdf_with_toc)
    chunks, report = build_chunks(page_texts, toc_entries=toc_entries)

    assert report.strategy == "toc"
    assert [c.source_chapter for c in chunks] == ["Chapter One", "Chapter Two"]


def test_end_to_end_headings_pdf_chunks_by_detected_heading(pdf_with_headings_no_toc) -> None:
    page_texts, toc_entries = load_pdf_pages(pdf_with_headings_no_toc)
    chunks, report = build_chunks(page_texts, toc_entries=toc_entries)

    assert report.strategy == "heading_regex"
    assert len(chunks) == 2


def test_end_to_end_plain_pdf_falls_back_to_fixed_size(pdf_plain_no_structure) -> None:
    page_texts, toc_entries = load_pdf_pages(pdf_plain_no_structure)
    chunks, report = build_chunks(
        page_texts, toc_entries=toc_entries, max_chunk_words=5, overlap_words=1
    )

    assert report.strategy == "fixed_size_overlap"
    assert all(c.source_chapter is None for c in chunks)
