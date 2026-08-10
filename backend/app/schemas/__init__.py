from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.constraints import (
    CORE_THESIS_MAX_WORDS,
    MAX_QUOTE_WORDS,
    MAX_QUOTES_PER_PRINCIPLE,
    PRINCIPLE_SUMMARY_MAX_WORDS,
    count_words,
    find_quotes,
)
from app.models import (
    BookTone,
    ExtractionMethod,
    ReviewStatus,
    SuggestionStatus,
    VerificationStatus,
)


class TrackedMetricSchema(BaseModel):
    id: str
    label: str
    description: str


class BookRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    book_id: str
    title: str
    author: str
    core_thesis: str
    tone: BookTone
    tracked_metrics: list[TrackedMetricSchema]
    review_status: ReviewStatus
    extraction_method: ExtractionMethod
    version: int
    created_at: datetime
    updated_at: datetime


class PrincipleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    principle_id: str
    book_id: str
    name: str
    summary: str
    source_chapter: str | None
    applies_to_tags: list[str]
    embedding_id: str | None
    review_status: ReviewStatus
    created_at: datetime
    updated_at: datetime


class LogEntrySchema(BaseModel):
    time: str
    action: str
    category: str


class DailyLogCreate(BaseModel):
    user_id: UUID
    date: date
    chosen_book_id: str
    entries: list[LogEntrySchema] = Field(default_factory=list)
    mood: int | None = Field(default=None, ge=1, le=5)


class DailyLogUpdate(BaseModel):
    chosen_book_id: str | None = None
    entries: list[LogEntrySchema] | None = None
    mood: int | None = Field(default=None, ge=1, le=5)


class DailyLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    log_id: UUID
    user_id: UUID
    date: date
    chosen_book_id: str
    entries: list[dict[str, Any]]
    mood: int | None
    created_at: datetime
    updated_at: datetime


def _validate_word_cap(value: str, max_words: int, field_name: str) -> str:
    word_count = count_words(value)
    if word_count > max_words:
        raise ValueError(
            f"{field_name} exceeds {max_words}-word cap ({word_count} words)"
        )
    return value


class BookWriteBase(BaseModel):
    """Internal write schema; used by tests/seeds, not exposed via read-only routes."""

    book_id: str
    title: str
    author: str
    core_thesis: str
    tone: BookTone = BookTone.pragmatic
    tracked_metrics: list[TrackedMetricSchema] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.pending_review
    version: int = 1

    @field_validator("core_thesis")
    @classmethod
    def validate_core_thesis_word_cap(cls, value: str) -> str:
        return _validate_word_cap(value, CORE_THESIS_MAX_WORDS, "core_thesis")


class PrincipleWriteBase(BaseModel):
    """Internal write schema; used by tests/seeds, not exposed via read-only routes."""

    principle_id: str
    book_id: str
    name: str
    summary: str
    source_chapter: str | None = None
    applies_to_tags: list[str] = Field(default_factory=list)
    embedding_id: str | None = None
    review_status: ReviewStatus = ReviewStatus.pending_review

    @field_validator("summary")
    @classmethod
    def validate_summary_word_cap(cls, value: str) -> str:
        return _validate_word_cap(value, PRINCIPLE_SUMMARY_MAX_WORDS, "summary")


def _validate_quote_rules(summary: str) -> str:
    quotes = find_quotes(summary)
    if len(quotes) > MAX_QUOTES_PER_PRINCIPLE:
        raise ValueError(
            f"summary contains {len(quotes)} quoted spans, exceeds the "
            f"{MAX_QUOTES_PER_PRINCIPLE}-quote-per-principle limit"
        )
    for quote in quotes:
        quote_word_count = count_words(quote)
        if quote_word_count > MAX_QUOTE_WORDS:
            raise ValueError(
                f"summary contains a {quote_word_count}-word quote, exceeds the "
                f'{MAX_QUOTE_WORDS}-word quote limit: "{quote}"'
            )
    return summary


class DraftPrincipleSubmission(BaseModel):
    """One principle within a Draft Extraction submission (architecture §2).

    Matches the shape the local extraction tool (and, on Path A, a human
    editor) already produces. review_status/embedding_id are never
    caller-supplied here — the ingestion endpoint forces
    review_status=pending_review and leaves embedding_id null until publish.
    """

    principle_id: str
    name: str
    summary: str
    source_chapter: str | None = None
    applies_to_tags: list[str] = Field(default_factory=list)

    @field_validator("summary")
    @classmethod
    def validate_summary_rules(cls, value: str) -> str:
        value = _validate_word_cap(value, PRINCIPLE_SUMMARY_MAX_WORDS, "summary")
        return _validate_quote_rules(value)


class DraftSubmissionCreate(BaseModel):
    """Draft Extraction submission payload (architecture §2 / §8): the single
    entry point ingestion uses regardless of whether the draft came from a
    human editor (Path A) or the local extraction tool (Path B). Always lands
    as review_status=pending_review — nothing here can publish directly."""

    book_id: str
    title: str
    author: str
    core_thesis: str
    tone: BookTone = BookTone.pragmatic
    tracked_metrics: list[TrackedMetricSchema] = Field(default_factory=list)
    extraction_method: ExtractionMethod
    principles: list[DraftPrincipleSubmission] = Field(min_length=1)

    @field_validator("core_thesis")
    @classmethod
    def validate_core_thesis_rules(cls, value: str) -> str:
        return _validate_word_cap(value, CORE_THESIS_MAX_WORDS, "core_thesis")

    @field_validator("principles")
    @classmethod
    def validate_unique_principle_ids(
        cls, value: list["DraftPrincipleSubmission"]
    ) -> list["DraftPrincipleSubmission"]:
        seen: set[str] = set()
        for principle in value:
            if principle.principle_id in seen:
                raise ValueError(f"duplicate principle_id in submission: {principle.principle_id!r}")
            seen.add(principle.principle_id)
        return value


class DraftSubmissionRead(BaseModel):
    book: BookRead
    principles: list[PrincipleRead]


class AnalysisRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    analysis_id: UUID
    log_id: UUID
    retrieved_principle_ids: list[str]
    reflection: str
    verification_status: VerificationStatus
    created_at: datetime


class SuggestionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    suggestion_id: UUID
    analysis_id: UUID
    principle_id: str
    text: str
    explanation: str
    status: SuggestionStatus
    created_at: datetime
    resolved_at: datetime | None


class StreakProgressRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    streak_id: UUID
    user_id: UUID
    book_id: str
    metric_id: str
    current_streak_days: int
    longest_streak_days: int
    trend_series: list[dict[str, Any]]
    updated_at: datetime


class UserCreate(BaseModel):
    """No email/PII collected for this MVP -- there's no auth yet (a known,
    documented Phase 1 gap), so the safest thing is to not ask for or store
    anything sensitive at all rather than half-implement encryption."""

    timezone: str = "UTC"


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    timezone: str
    active_book_id: str | None
    created_at: datetime


class AnalysisCreate(BaseModel):
    log_id: UUID


class SuggestionUpdate(BaseModel):
    status: SuggestionStatus
