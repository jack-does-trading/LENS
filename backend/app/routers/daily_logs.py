from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DailyLog
from app.schemas import DailyLogCreate, DailyLogRead, DailyLogUpdate

router = APIRouter(prefix="/daily-logs", tags=["daily-logs"])


@router.get("", response_model=list[DailyLogRead])
def list_daily_logs(
    user_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[DailyLog]:
    query = db.query(DailyLog)
    if user_id is not None:
        query = query.filter(DailyLog.user_id == user_id)
    return query.order_by(DailyLog.date.desc()).all()


@router.get("/{log_id}", response_model=DailyLogRead)
def get_daily_log(log_id: UUID, db: Session = Depends(get_db)) -> DailyLog:
    log = db.get(DailyLog, log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="Daily log not found")
    return log


@router.post("", response_model=DailyLogRead, status_code=201)
def create_daily_log(payload: DailyLogCreate, db: Session = Depends(get_db)) -> DailyLog:
    log = DailyLog(
        user_id=payload.user_id,
        date=payload.date,
        chosen_book_id=payload.chosen_book_id,
        entries=[entry.model_dump() for entry in payload.entries],
        mood=payload.mood,
    )
    db.add(log)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Daily log write conflict (e.g. unknown user or book)") from exc
    db.refresh(log)
    return log


@router.put("/{log_id}", response_model=DailyLogRead)
def update_daily_log(
    log_id: UUID,
    payload: DailyLogUpdate,
    db: Session = Depends(get_db),
) -> DailyLog:
    log = db.get(DailyLog, log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="Daily log not found")

    updates = payload.model_dump(exclude_unset=True)

    for field, value in updates.items():
        setattr(log, field, value)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Daily log conflict") from exc
    db.refresh(log)
    return log


@router.delete("/{log_id}", status_code=204)
def delete_daily_log(log_id: UUID, db: Session = Depends(get_db)) -> None:
    log = db.get(DailyLog, log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="Daily log not found")
    db.delete(log)
    db.commit()
