"""site_visits -- session-grained counters behind the shelf's "visits"/"online" readouts

Revision ID: 007
Revises: 006
Create Date: 2026-09-14

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No user_id/IP/user-agent column on purpose -- the shelf only needs two
    # counts, and anything more would be PII this app has deliberately avoided
    # collecting everywhere else.
    op.create_table(
        "site_visits",
        sa.Column(
            "visit_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("session_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "first_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("session_id", name="uq_site_visits_session"),
    )
    # "Online" is `last_seen > now() - interval`, i.e. a range scan over a
    # table that grows with every visitor forever; the unique index on
    # session_id above serves the upsert, this one serves the read.
    op.create_index("ix_site_visits_last_seen", "site_visits", ["last_seen"])


def downgrade() -> None:
    op.drop_index("ix_site_visits_last_seen", table_name="site_visits")
    op.drop_table("site_visits")
