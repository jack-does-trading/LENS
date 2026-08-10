from __future__ import annotations

from local_extraction.schema import AggregatedDraft, CandidatePrinciple
from local_extraction.validation import (
    CORE_THESIS_MAX_WORDS,
    MAX_QUOTE_WORDS,
    PRINCIPLE_SUMMARY_MAX_WORDS,
    count_words,
    find_quotes,
    validate_core_thesis,
    validate_draft,
    validate_principle_summary,
)


def _words(n: int) -> str:
    return " ".join(f"word{i}" for i in range(1, n + 1))


def test_count_words_basic() -> None:
    assert count_words("one two three") == 3
    assert count_words("   ") == 0
    assert count_words("") == 0


def test_find_quotes_extracts_straight_and_curly_quotes() -> None:
    text = 'She said "hello there" and later “goodbye now”.'
    assert find_quotes(text) == ["hello there", "goodbye now"]


def test_find_quotes_returns_empty_when_no_quotes() -> None:
    assert find_quotes("no quotes in this sentence at all") == []


def test_validate_core_thesis_accepts_boundary() -> None:
    assert validate_core_thesis(_words(CORE_THESIS_MAX_WORDS)) == []


def test_validate_core_thesis_rejects_over_cap() -> None:
    issues = validate_core_thesis(_words(CORE_THESIS_MAX_WORDS + 1))
    assert len(issues) == 1
    assert "core_thesis" in issues[0].field
    assert "exceeds" in issues[0].message


def test_validate_principle_summary_accepts_boundary() -> None:
    assert validate_principle_summary("Identity", _words(PRINCIPLE_SUMMARY_MAX_WORDS)) == []


def test_validate_principle_summary_rejects_over_word_cap() -> None:
    issues = validate_principle_summary("Identity", _words(PRINCIPLE_SUMMARY_MAX_WORDS + 1))
    assert any("word cap" in i.message or "exceeds" in i.message for i in issues)


def test_validate_principle_summary_rejects_quote_over_15_words() -> None:
    long_quote = " ".join(f"w{i}" for i in range(1, MAX_QUOTE_WORDS + 2))  # 16 words
    summary = f'A short paraphrase. "{long_quote}" is what the author wrote.'

    issues = validate_principle_summary("Some Principle", summary)

    assert len(issues) == 1
    assert "quote" in issues[0].message.lower()
    assert "exceeds" in issues[0].message


def test_validate_principle_summary_accepts_single_short_quote() -> None:
    summary = 'A short paraphrase. "Live consciously" is the key idea here.'
    assert validate_principle_summary("Some Principle", summary) == []


def test_validate_principle_summary_rejects_more_than_one_quote() -> None:
    summary = 'First point: "live consciously" matters. Second point: "accept yourself" too.'

    issues = validate_principle_summary("Some Principle", summary)

    assert any("quote" in i.message.lower() and "limit" in i.message.lower() for i in issues)


def test_validate_draft_aggregates_all_issues() -> None:
    draft = AggregatedDraft(
        core_thesis=_words(CORE_THESIS_MAX_WORDS + 5),
        principles=[
            CandidatePrinciple(
                name="Bad Principle",
                summary=_words(PRINCIPLE_SUMMARY_MAX_WORDS + 1),
                source_chapter="ch1",
            ),
            CandidatePrinciple(
                name="Good Principle",
                summary="A perfectly fine, short, paraphrased summary.",
                source_chapter="ch2",
            ),
        ],
    )

    issues = validate_draft(draft)

    assert any("core_thesis" in i.field for i in issues)
    assert any("Bad Principle" in i.field for i in issues)
    assert not any("Good Principle" in i.field for i in issues)


def test_validate_draft_returns_empty_for_fully_valid_draft() -> None:
    draft = AggregatedDraft(
        core_thesis="A short, valid core thesis.",
        principles=[
            CandidatePrinciple(
                name="Valid Principle",
                summary="A short, valid, paraphrased summary with one quote: \"be present\".",
                source_chapter="ch1",
            ),
        ],
    )

    assert validate_draft(draft) == []
