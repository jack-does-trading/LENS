from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DailyLog, StreakProgress
from app.schemas import StreakProgressRead
from app.streaks import compute_streaks

router = APIRouter(prefix="/streaks", tags=["streaks"])

# Books don't reliably define tracked_metrics yet (architecture's own example
# book has an empty list) -- "did you log anything today" is a sane default
# single metric until a book-defined metric schema is actually used anywhere.
DEFAULT_METRIC_ID = "consistency"


@router.get("", response_model=StreakProgressRead)
def get_streak(
    user_id: UUID = Query(...), book_id: str = Query(...), db: Session = Depends(get_db)
) -> StreakProgress:
    log_dates = [
        row[0]
        for row in db.query(DailyLog.date)
        .filter(DailyLog.user_id == user_id, DailyLog.chosen_book_id == book_id)
        .all()
    ]
    current, longest = compute_streaks(log_dates)

    streak = (
        db.query(StreakProgress)
        .filter(
            StreakProgress.user_id == user_id,
            StreakProgress.book_id == book_id,
            StreakProgress.metric_id == DEFAULT_METRIC_ID,
        )
        .first()
    )
    if streak is None:
        streak = StreakProgress(user_id=user_id, book_id=book_id, metric_id=DEFAULT_METRIC_ID)
        db.add(streak)
    streak.current_streak_days = current
    streak.longest_streak_days = longest
    db.commit()
    db.refresh(streak)
    return streak
