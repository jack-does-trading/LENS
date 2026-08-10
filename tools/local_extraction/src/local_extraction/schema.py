from __future__ import annotations

from dataclasses import dataclass, field

# Mirrors architecture §4's tone enum for books. Kept as a plain tuple (not an
# import from the backend app) since this tool must stay fully standalone.
BOOK_TONES = ("pragmatic", "philosophical", "spiritual", "scientific", "narrative")


@dataclass(frozen=True)
class CandidatePrinciple:
    name: str
    summary: str
    source_chapter: str | None
    applies_to_tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AggregatedDraft:
    core_thesis: str
    principles: list[CandidatePrinciple]
