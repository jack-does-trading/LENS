from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Suggestion
from app.schemas import SuggestionRead, SuggestionUpdate

router = APIRouter(prefix="/suggestions", tags=["suggestions"])


@router.get("", response_model=list[SuggestionRead])
def list_suggestions(
    analysis_id: UUID | None = Query(default=None), db: Session = Depends(get_db)
) -> list[Suggestion]:
    query = db.query(Suggestion)
    if analysis_id is not None:
        query = query.filter(Suggestion.analysis_id == analysis_id)
    return query.order_by(Suggestion.created_at).all()


@router.put("/{suggestion_id}", response_model=SuggestionRead)
def update_suggestion(
    suggestion_id: UUID, payload: SuggestionUpdate, db: Session = Depends(get_db)
) -> Suggestion:
    suggestion = db.get(Suggestion, suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    suggestion.status = payload.status
    suggestion.resolved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(suggestion)
    return suggestion
