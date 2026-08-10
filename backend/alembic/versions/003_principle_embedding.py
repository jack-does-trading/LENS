"""add embedding vector column to principles

Revision ID: 003
Revises: 002
Create Date: 2026-07-30

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Matches settings.embedding_dimension (app/config.py) for Voyage AI's
# "voyage-3" model. If the embedding model ever changes to a different output
# dimension, this column needs a follow-up migration -- pgvector requires a
# fixed dimension per column.
EMBEDDING_DIMENSION = 1024


def upgrade() -> None:
    op.add_column(
        "principles",
        sa.Column("embedding", Vector(EMBEDDING_DIMENSION), nullable=True),
    )
    # IVFFlat index for cosine-distance nearest-neighbor search, scoped per
    # book_id at query time via a WHERE clause (architecture SS3: "scoped
    # query, not cross-book"). Corpus is small (hundreds-low thousands of
    # rows per architecture's own tech-stack rationale), so a single index
    # across all books is fine -- no per-book partitioning needed.
    # lists=10 suits the current tiny corpus (low hundreds of rows); pgvector
    # docs recommend roughly rows/1000 (up to 1M rows) -- revisit once the
    # catalogue grows past a few thousand principles.
    op.execute(
        "CREATE INDEX ix_principles_embedding_cosine ON principles "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_principles_embedding_cosine")
    op.drop_column("principles", "embedding")
