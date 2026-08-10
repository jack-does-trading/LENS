from __future__ import annotations

from pathlib import Path

import fitz
import pytest


def _write_pdf(path: Path, pages_text: list[str], toc: list[list] | None = None) -> Path:
    doc = fitz.open()
    try:
        for text in pages_text:
            page = doc.new_page()
            page.insert_text((72, 72), text)
        if toc:
            doc.set_toc(toc)
        doc.save(path)
    finally:
        doc.close()
    return path


@pytest.fixture
def pdf_with_toc(tmp_path: Path) -> Path:
    """A tiny 'fake book' with an embedded, publisher-style TOC."""
    pages_text = [
        "Chapter One\nSelf-esteem begins with self-acceptance.",
        "More of chapter one content goes here for page two.",
        "Chapter Two\nLiving consciously means paying attention to reality.",
    ]
    toc = [
        [1, "Chapter One", 1],
        [1, "Chapter Two", 3],
    ]
    return _write_pdf(tmp_path / "toc_book.pdf", pages_text, toc)


@pytest.fixture
def pdf_with_nested_toc(tmp_path: Path) -> Path:
    """A 'fake book' with Part (level 1) > Chapter (level 2) nesting, matching
    the real Six Pillars of Self-Esteem PDF's TOC structure."""
    pages_text = [
        "Part One\nIntroductory material.",
        "Chapter One\nSelf-esteem begins with self-acceptance.",
        "More of chapter one content goes here for page three.",
        "Chapter Two\nLiving consciously means paying attention to reality.",
    ]
    toc = [
        [1, "Part One", 1],
        [2, "Chapter One", 2],
        [2, "Chapter Two", 4],
    ]
    return _write_pdf(tmp_path / "nested_toc_book.pdf", pages_text, toc)


@pytest.fixture
def pdf_with_headings_no_toc(tmp_path: Path) -> Path:
    """A 'fake book' with no embedded TOC but regex-detectable headings."""
    pages_text = [
        "Chapter 1\nThe practice of living consciously starts here.",
        "Chapter 2\nThe practice of self-acceptance follows naturally.",
    ]
    return _write_pdf(tmp_path / "headings_book.pdf", pages_text, toc=None)


@pytest.fixture
def pdf_plain_no_structure(tmp_path: Path) -> Path:
    """A 'fake book' with neither TOC nor headings — fixed-size fallback only."""
    pages_text = [f"Plain unstructured page {i} with some filler words here." for i in range(3)]
    return _write_pdf(tmp_path / "plain_book.pdf", pages_text, toc=None)
