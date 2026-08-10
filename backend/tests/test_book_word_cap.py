import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.models import Book
from app.schemas import BookWriteBase


def _overlong_core_thesis(word_count: int = 151) -> str:
    return " ".join(f"word{i}" for i in range(1, word_count + 1))


def test_core_thesis_over_word_cap_rejected_by_pydantic() -> None:
    with pytest.raises(ValidationError, match="core_thesis exceeds 150-word cap"):
        BookWriteBase(
            book_id="over-cap-book",
            title="Over Cap",
            author="Some Author",
            core_thesis=_overlong_core_thesis(),
        )


def test_core_thesis_over_word_cap_rejected_by_db(db_session) -> None:
    book = Book(
        book_id="over-cap-book",
        title="Over Cap",
        author="Some Author",
        core_thesis=_overlong_core_thesis(),
    )
    db_session.add(book)

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()


def test_valid_core_thesis_is_accepted(db_session) -> None:
    core_thesis = " ".join(f"word{i}" for i in range(1, 151))
    book = Book(
        book_id="valid-cap-book",
        title="Valid Cap",
        author="Some Author",
        core_thesis=core_thesis,
    )
    db_session.add(book)
    db_session.commit()

    stored = db_session.get(Book, book.book_id)
    assert stored is not None
    assert len(stored.core_thesis.split()) == 150
