import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Book, Principle, ReviewStatus
from app.schemas import DraftSubmissionCreate, DraftSubmissionRead

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


def require_ingestion_api_key(x_ingestion_api_key: str | None = Header(default=None)) -> None:
    """Gate the ingestion write path with a single static shared secret.

    This is the only write endpoint in the app that isn't scoped to a
    specific user (daily_logs writes are scoped by user_id) — it can create
    or replace shared catalogue rows, so it stays behind a key even though
    Phase 1 otherwise has no auth (see HANDOFF.md 9.10). Fails closed: if
    INGESTION_API_KEY isn't configured, every request is rejected rather than
    silently left open.
    """
    if not settings.ingestion_api_key:
        raise HTTPException(
            status_code=503,
            detail="Ingestion is not configured: set INGESTION_API_KEY to enable this endpoint.",
        )
    if not x_ingestion_api_key or not secrets.compare_digest(
        x_ingestion_api_key, settings.ingestion_api_key
    ):
        raise HTTPException(status_code=401, detail="Missing or invalid X-Ingestion-Api-Key header")


@router.post(
    "/draft-submissions",
    response_model=DraftSubmissionRead,
    status_code=201,
    dependencies=[Depends(require_ingestion_api_key)],
)
def submit_draft(
    payload: DraftSubmissionCreate,
    replace: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict:
    """Draft Extraction submission endpoint (architecture §2 / §8).

    Accepts the same draft JSON shape produced by a human editor (Path A) or
    the local extraction tool (Path B) and stores it as
    review_status=pending_review — this endpoint never publishes anything;
    that's a separate, not-yet-built review/approval step. A book_id that
    already exists is rejected with 409 unless replace=true is passed
    explicitly, mirroring the local extraction tool's own
    OutputExistsError/--overwrite guard: a previously-submitted draft may
    still be mid human-review, and must never be silently clobbered.
    """
    existing = db.get(Book, payload.book_id)
    if existing is not None and not replace:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Book {payload.book_id!r} already exists. Pass ?replace=true "
                "to resubmit and overwrite it."
            ),
        )

    if existing is not None:
        db.query(Principle).filter(Principle.book_id == payload.book_id).delete()
        book = existing
        book.title = payload.title
        book.author = payload.author
        book.core_thesis = payload.core_thesis
        book.tone = payload.tone
        book.tracked_metrics = [metric.model_dump() for metric in payload.tracked_metrics]
        book.extraction_method = payload.extraction_method
        book.review_status = ReviewStatus.pending_review
    else:
        book = Book(
            book_id=payload.book_id,
            title=payload.title,
            author=payload.author,
            core_thesis=payload.core_thesis,
            tone=payload.tone,
            tracked_metrics=[metric.model_dump() for metric in payload.tracked_metrics],
            extraction_method=payload.extraction_method,
            review_status=ReviewStatus.pending_review,
            version=1,
        )
        db.add(book)

    principles = [
        Principle(
            principle_id=p.principle_id,
            book_id=payload.book_id,
            name=p.name,
            summary=p.summary,
            source_chapter=p.source_chapter,
            applies_to_tags=p.applies_to_tags,
            embedding_id=None,
            review_status=ReviewStatus.pending_review,
        )
        for p in payload.principles
    ]
    db.add_all(principles)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "Draft submission conflicts with existing data (e.g. a "
                "principle_id already in use by a published suggestion, or a "
                "duplicate principle_id across books)."
            ),
        ) from exc

    db.refresh(book)
    for principle in principles:
        db.refresh(principle)

    return {"book": book, "principles": principles}
