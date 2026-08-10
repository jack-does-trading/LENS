from __future__ import annotations

import re
from dataclasses import dataclass

from local_extraction.schema import AggregatedDraft

# Mirrors backend/app/constraints.py caps (architecture §4). Reimplemented
# locally, not imported, since this tool must stay fully standalone from the
# app service.
CORE_THESIS_MAX_WORDS = 150
PRINCIPLE_SUMMARY_MAX_WORDS = 200
MAX_QUOTE_WORDS = 15
MAX_QUOTES_PER_PRINCIPLE = 1

# Matches text within straight or curly double quotes.
_QUOTE_PATTERN = re.compile(r'["“]([^"”]+)["”]')


def count_words(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    return len(stripped.split())


def find_quotes(text: str) -> list[str]:
    return _QUOTE_PATTERN.findall(text)


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.field}: {self.message}"


def validate_core_thesis(core_thesis: str) -> list[ValidationIssue]:
    word_count = count_words(core_thesis)
    if word_count > CORE_THESIS_MAX_WORDS:
        return [
            ValidationIssue(
                "core_thesis",
                f"{word_count} words exceeds the {CORE_THESIS_MAX_WORDS}-word cap",
            )
        ]
    return []


def validate_principle_summary(principle_name: str, summary: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    field = f"principle[{principle_name!r}].summary"

    word_count = count_words(summary)
    if word_count > PRINCIPLE_SUMMARY_MAX_WORDS:
        issues.append(
            ValidationIssue(
                field, f"{word_count} words exceeds the {PRINCIPLE_SUMMARY_MAX_WORDS}-word cap"
            )
        )

    quotes = find_quotes(summary)
    if len(quotes) > MAX_QUOTES_PER_PRINCIPLE:
        issues.append(
            ValidationIssue(
                field,
                f"contains {len(quotes)} quoted spans, exceeds the "
                f"{MAX_QUOTES_PER_PRINCIPLE}-quote-per-principle limit",
            )
        )
    for quote in quotes:
        quote_word_count = count_words(quote)
        if quote_word_count > MAX_QUOTE_WORDS:
            issues.append(
                ValidationIssue(
                    field,
                    f"quoted span ({quote_word_count} words) exceeds the "
                    f"{MAX_QUOTE_WORDS}-word quote limit: \"{quote}\"",
                )
            )
    return issues


def validate_draft(draft: AggregatedDraft) -> list[ValidationIssue]:
    """Validate an aggregated draft against every hard rule before it's allowed
    to be written anywhere. Returns the list of issues found (empty = valid).

    This function only reports; callers must reject on any non-empty result
    rather than truncating or otherwise auto-fixing the model's output.
    """
    issues = list(validate_core_thesis(draft.core_thesis))
    for principle in draft.principles:
        issues.extend(validate_principle_summary(principle.name, principle.summary))
    return issues
