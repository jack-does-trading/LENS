"""change analyses.reflection from a JSONB list of points to a single paragraph

Revision ID: 005
Revises: 004
Create Date: 2026-08-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Reflection moves from a list of repetitive per-principle sentences to
    # one paragraph assessing the day's situation, with the related
    # principles now shown separately in the UI (via retrieved_principle_ids
    # + GET /principles/{id}) instead of being embedded in the text itself.
    # Dev-only DB, existing reflection values are dropped rather than
    # migrated (single trusted local user, trivially regenerated).
    op.drop_column("analyses", "reflection")
    op.add_column(
        "analyses",
        sa.Column("reflection", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("analyses", "reflection")
    op.add_column(
        "analyses",
        sa.Column("reflection", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
