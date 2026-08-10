"""drop UNIQUE(user_id, date) on daily_logs -- multiple independent situations per day

Revision ID: 006
Revises: 005
Create Date: 2026-08-03

"""

from typing import Sequence, Union

from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Product shift: Lens moves from "one daily journal entry per user per
    # day" to "ask for advice on a situation, any number of times per day,
    # each independent." The one-row-per-day constraint was what forced
    # same-day submissions to merge into one shared analysis -- removing it
    # lets each situation get its own daily_logs row and its own analysis
    # (analyses.log_id is already unique 1:1, which already matches "one
    # situation -> one advice session" with no change needed there).
    # compute_streaks() (app/streaks.py) already dedupes by distinct date via
    # set(), so streak counting is unaffected by multiple rows sharing a date.
    op.drop_constraint("uq_daily_logs_user_date", "daily_logs", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint("uq_daily_logs_user_date", "daily_logs", ["user_id", "date"])
