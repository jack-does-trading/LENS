import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.database import Base


class BookTone(str, enum.Enum):
    pragmatic = "pragmatic"
    philosophical = "philosophical"
    spiritual = "spiritual"
    scientific = "scientific"
    narrative = "narrative"


class ReviewStatus(str, enum.Enum):
    human_reviewed = "human_reviewed"
    pending_review = "pending_review"


class ExtractionMethod(str, enum.Enum):
    human_written = "human_written"
    local_model = "local_model"


class VerificationStatus(str, enum.Enum):
    passed = "passed"
    fallback_used = "fallback_used"


class SuggestionStatus(str, enum.Enum):
    pending = "pending"
    done = "done"
    skipped = "skipped"


class Book(Base):
    __tablename__ = "books"
    __table_args__ = (
        CheckConstraint(
            "word_count(core_thesis) <= 150",
            name="ck_books_core_thesis_word_cap",
        ),
    )

    book_id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    author: Mapped[str] = mapped_column(String, nullable=False)
    core_thesis: Mapped[str] = mapped_column(Text, nullable=False)
    tone: Mapped[BookTone] = mapped_column(
        String, nullable=False, server_default=BookTone.pragmatic.value
    )
    tracked_metrics: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    review_status: Mapped[ReviewStatus] = mapped_column(
        String, nullable=False, server_default=ReviewStatus.pending_review.value
    )
    extraction_method: Mapped[ExtractionMethod] = mapped_column(
        String, nullable=False, server_default=ExtractionMethod.human_written.value
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    principles: Mapped[list["Principle"]] = relationship(back_populates="book")


class Principle(Base):
    __tablename__ = "principles"
    __table_args__ = (
        CheckConstraint(
            "word_count(summary) <= 200",
            name="ck_principles_summary_word_cap",
        ),
    )

    principle_id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(
        String, ForeignKey("books.book_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    source_chapter: Mapped[str | None] = mapped_column(String, nullable=True)
    applies_to_tags: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default=text("'{}'::text[]")
    )
    embedding_id: Mapped[str | None] = mapped_column(String, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.embedding_dimension), nullable=True
    )
    review_status: Mapped[ReviewStatus] = mapped_column(
        String, nullable=False, server_default=ReviewStatus.pending_review.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    book: Mapped["Book"] = relationship(back_populates="principles")


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    auth_provider_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    active_book_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("books.book_id", ondelete="SET NULL"), nullable=True
    )
    timezone: Mapped[str] = mapped_column(String, nullable=False, server_default="UTC")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DailyLog(Base):
    __tablename__ = "daily_logs"
    __table_args__ = (
        # No longer unique per (user_id, date) -- a user can log multiple
        # independent situations the same day, each getting its own analysis
        # (migration 006). "date" is retained as metadata for streaks/history,
        # not as a uniqueness key.
        CheckConstraint(
            "mood IS NULL OR (mood >= 1 AND mood <= 5)",
            name="ck_daily_logs_mood_range",
        ),
    )

    log_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    chosen_book_id: Mapped[str] = mapped_column(
        String, ForeignKey("books.book_id", ondelete="RESTRICT"), nullable=False
    )
    entries: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    mood: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (UniqueConstraint("log_id", name="uq_analyses_log_id"),)

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    log_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("daily_logs.log_id", ondelete="CASCADE"),
        nullable=False,
    )
    retrieved_principle_ids: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default=text("'{}'::text[]")
    )
    reflection: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    verification_status: Mapped[VerificationStatus] = mapped_column(
        String, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Suggestion(Base):
    __tablename__ = "suggestions"

    suggestion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analyses.analysis_id", ondelete="CASCADE"),
        nullable=False,
    )
    principle_id: Mapped[str] = mapped_column(
        String, ForeignKey("principles.principle_id", ondelete="RESTRICT"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[SuggestionStatus] = mapped_column(
        String, nullable=False, server_default=SuggestionStatus.pending.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class StreakProgress(Base):
    __tablename__ = "streaks_progress"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "book_id", "metric_id", name="uq_streaks_progress_user_book_metric"
        ),
    )

    streak_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    book_id: Mapped[str] = mapped_column(
        String, ForeignKey("books.book_id", ondelete="CASCADE"), nullable=False
    )
    metric_id: Mapped[str] = mapped_column(String, nullable=False)
    current_streak_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    longest_streak_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    trend_series: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
