"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-07-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION word_count(input_text text)
        RETURNS integer
        LANGUAGE sql
        IMMUTABLE
        STRICT
        AS $$
            SELECT COALESCE(
                array_length(
                    regexp_split_to_array(trim(input_text), E'\\\\s+'),
                    1
                ),
                0
            );
        $$;
        """
    )

    op.create_table(
        "books",
        sa.Column("book_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("core_thesis", sa.Text(), nullable=False),
        sa.Column(
            "tone",
            sa.String(),
            server_default="pragmatic",
            nullable=False,
        ),
        sa.Column(
            "tracked_metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "review_status",
            sa.String(),
            server_default="pending_review",
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "word_count(core_thesis) <= 150",
            name="ck_books_core_thesis_word_cap",
        ),
        sa.PrimaryKeyConstraint("book_id"),
    )

    op.create_table(
        "principles",
        sa.Column("principle_id", sa.String(), nullable=False),
        sa.Column("book_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source_chapter", sa.String(), nullable=True),
        sa.Column(
            "applies_to_tags",
            postgresql.ARRAY(sa.String()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("embedding_id", sa.String(), nullable=True),
        sa.Column(
            "review_status",
            sa.String(),
            server_default="pending_review",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "word_count(summary) <= 200",
            name="ck_principles_summary_word_cap",
        ),
        sa.ForeignKeyConstraint(["book_id"], ["books.book_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("principle_id"),
    )

    op.create_table(
        "users",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("auth_provider_id", sa.String(), nullable=False),
        sa.Column("active_book_id", sa.String(), nullable=True),
        sa.Column("timezone", sa.String(), server_default="UTC", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["active_book_id"], ["books.book_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("auth_provider_id"),
    )

    op.create_table(
        "daily_logs",
        sa.Column("log_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("chosen_book_id", sa.String(), nullable=False),
        sa.Column(
            "entries",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("mood", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mood IS NULL OR (mood >= 1 AND mood <= 5)",
            name="ck_daily_logs_mood_range",
        ),
        sa.ForeignKeyConstraint(["chosen_book_id"], ["books.book_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("log_id"),
        sa.UniqueConstraint("user_id", "date", name="uq_daily_logs_user_date"),
    )

    op.create_table(
        "streaks_progress",
        sa.Column("streak_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("book_id", sa.String(), nullable=False),
        sa.Column("metric_id", sa.String(), nullable=False),
        sa.Column("current_streak_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column("longest_streak_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "trend_series",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["book_id"], ["books.book_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("streak_id"),
        sa.UniqueConstraint(
            "user_id", "book_id", "metric_id", name="uq_streaks_progress_user_book_metric"
        ),
    )

    op.create_table(
        "analyses",
        sa.Column("analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("log_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "retrieved_principle_ids",
            postgresql.ARRAY(sa.String()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("analysis_text", sa.Text(), nullable=False),
        sa.Column("verification_status", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["log_id"], ["daily_logs.log_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("analysis_id"),
        sa.UniqueConstraint("log_id", name="uq_analyses_log_id"),
    )

    op.create_table(
        "suggestions",
        sa.Column("suggestion_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principle_id", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.analysis_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["principle_id"], ["principles.principle_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("suggestion_id"),
    )


def downgrade() -> None:
    op.drop_table("suggestions")
    op.drop_table("analyses")
    op.drop_table("streaks_progress")
    op.drop_table("daily_logs")
    op.drop_table("users")
    op.drop_table("principles")
    op.drop_table("books")
    op.execute("DROP FUNCTION IF EXISTS word_count(text)")
    op.execute("DROP EXTENSION IF EXISTS vector")
