from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Principle
from app.schemas import PrincipleRead

router = APIRouter(prefix="/principles", tags=["principles"])


@router.get("", response_model=list[PrincipleRead])
def list_principles(
    book_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Principle]:
    query = db.query(Principle)
    if book_id is not None:
        query = query.filter(Principle.book_id == book_id)
    return query.order_by(Principle.name).all()


@router.get("/{principle_id}", response_model=PrincipleRead)
def get_principle(principle_id: str, db: Session = Depends(get_db)) -> Principle:
    principle = db.get(Principle, principle_id)
    if principle is None:
        raise HTTPException(status_code=404, detail="Principle not found")
    return principle
