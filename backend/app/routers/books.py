from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Book, DailyLog, Principle
from app.schemas import BookRead, DailyLogCreate, DailyLogRead, DailyLogUpdate, PrincipleRead

router = APIRouter(prefix="/books", tags=["books"])


@router.get("", response_model=list[BookRead])
def list_books(db: Session = Depends(get_db)) -> list[Book]:
    return db.query(Book).order_by(Book.title).all()


@router.get("/{book_id}", response_model=BookRead)
def get_book(book_id: str, db: Session = Depends(get_db)) -> Book:
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    return book
