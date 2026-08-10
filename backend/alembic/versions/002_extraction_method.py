"""add extraction_method to books

Revision ID: 002
Revises: 001
Create Date: 2026-07-30

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "books",
        sa.Column(
            "extraction_method",
            sa.String(),
            server_default="human_written",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("books", "extraction_method")
