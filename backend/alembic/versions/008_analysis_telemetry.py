"""analysis telemetry -- record HOW an analysis was reached, not just the outcome

Revision ID: 008
Revises: 007
Create Date: 2026-09-19

`analyses.verification_status` is only `passed` | `fallback_used`, so it cannot
distinguish an answer that passed on the first synthesis attempt from one that
only passed on the fifth, and it says nothing about which model produced it or
which verification rule rejected the earlier attempts. That is the measurement
gap behind the README's own lesson: a fail-closed design converts an outage
into a quality regression, and the fallback is indistinguishable from success
unless something records the path taken.

Every column is nullable. Rows written before this migration genuinely have no
telemetry, and backfilling a zero would be a lie -- null must read as
"unknown", not as "zero attempts".

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("analyses", sa.Column("synthesis_attempts", sa.Integer(), nullable=True))
    # JSONB, not ARRAY(String): the issue strings are already structured-ish
    # ("suggestion cites unknown principle_id 'x'") and JSONB leaves room to
    # store richer per-issue objects later without another migration.
    op.add_column("analyses", sa.Column("verification_issues", postgresql.JSONB(), nullable=True))
    op.add_column("analyses", sa.Column("llm_provider", sa.String(), nullable=True))
    op.add_column("analyses", sa.Column("llm_model", sa.String(), nullable=True))
    op.add_column("analyses", sa.Column("prompt_version", sa.String(), nullable=True))
    op.add_column("analyses", sa.Column("latency_ms", sa.Integer(), nullable=True))
    # The quality endpoint's only query is "fallback rate over the last N
    # days", i.e. a range scan on created_at filtered by status. Small table
    # today, but this is the index that keeps it cheap as analyses accumulate.
    op.create_index(
        "ix_analyses_created_at_status",
        "analyses",
        ["created_at", "verification_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_analyses_created_at_status", table_name="analyses")
    op.drop_column("analyses", "latency_ms")
    op.drop_column("analyses", "prompt_version")
    op.drop_column("analyses", "llm_model")
    op.drop_column("analyses", "llm_provider")
    op.drop_column("analyses", "verification_issues")
    op.drop_column("analyses", "synthesis_attempts")
