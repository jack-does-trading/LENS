import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.embeddings import EmbeddingClient, VoyageEmbeddingClient
from app.llm import GroqLLMClient, LLMClient, OllamaLLMClient
from app.models import Analysis, Book, DailyLog, Principle, ReviewStatus, Suggestion, VerificationStatus
from app.retrieval import LogEntryLike, retrieve_principles
from app.schemas import AnalysisCreate, AnalysisRead
from app.synthesis import fallback_analysis, synthesize_analysis
from app.verification import verify_analysis

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analyses", tags=["analyses"])

# Each attempt costs two local-model calls (synthesis + entailment check),
# but both run against the user's own local Ollama instance -- free and
# fast enough that spending more attempts before giving up on the fallback
# template is a good trade. Raised from 3: the retry loop already feeds the
# model back its exact verification.issues each attempt (see
# synthesis._build_retry_reminder), so extra attempts have a real shot at
# self-correcting rather than just repeating the same mistake.
MAX_SYNTHESIS_ATTEMPTS = 5


def get_llm_client() -> LLMClient:
    if settings.llm_provider == "groq":
        if not settings.groq_api_key:
            raise HTTPException(
                status_code=503, detail="LLM_PROVIDER=groq but GROQ_API_KEY is not set"
            )
        return GroqLLMClient(model=settings.groq_model, api_key=settings.groq_api_key)
    return OllamaLLMClient(host=settings.ollama_host, model=settings.ollama_model)


def get_embedding_client() -> EmbeddingClient:
    if not settings.voyage_api_key:
        raise HTTPException(status_code=503, detail="Retrieval is not configured: set VOYAGE_API_KEY")
    return VoyageEmbeddingClient(api_key=settings.voyage_api_key, model=settings.voyage_model)


@router.post("", response_model=AnalysisRead, status_code=201)
def create_analysis(
    payload: AnalysisCreate,
    db: Session = Depends(get_db),
    llm_client: LLMClient = Depends(get_llm_client),
    embedding_client: EmbeddingClient = Depends(get_embedding_client),
) -> Analysis:
    """Orchestrates Step A (retrieval) -> Step B (synthesis) -> Step C
    (verification) for one daily log, per architecture SS1/SS3. Retries
    synthesis once on a verification failure, then falls back to a
    non-LLM template -- never returns unverified LLM output.
    """
    log = db.get(DailyLog, payload.log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="Daily log not found")
    if db.query(Analysis).filter(Analysis.log_id == log.log_id).first() is not None:
        raise HTTPException(status_code=409, detail="Analysis already exists for this daily log")

    book = db.get(Book, log.chosen_book_id)
    if book is None or book.review_status != ReviewStatus.human_reviewed:
        raise HTTPException(status_code=400, detail="Book is not reviewed/available for analysis")

    entries = [LogEntryLike(category=e.get("category", ""), action=e.get("action", "")) for e in log.entries]
    principle_ids = retrieve_principles(db, book.book_id, entries, embedding_client, mood=log.mood)
    if not principle_ids:
        raise HTTPException(status_code=422, detail="No relevant principles could be retrieved for this log")

    by_id = {p.principle_id: p for p in db.query(Principle).filter(Principle.principle_id.in_(principle_ids)).all()}
    principles = [by_id[pid] for pid in principle_ids if pid in by_id]

    result, verification = None, None
    retry_issues: list[str] | None = None
    for attempt in range(MAX_SYNTHESIS_ATTEMPTS):
        try:
            result = synthesize_analysis(
                llm_client, book, principles, log.entries, log.mood, retry_issues=retry_issues
            )
        except Exception as exc:
            logger.warning(
                "synthesis attempt %d/%d for log %s raised: %s", attempt + 1, MAX_SYNTHESIS_ATTEMPTS, log.log_id, exc
            )
            result, verification = None, None
            retry_issues = [f"the previous response was rejected: {exc}"]
            continue
        verification = verify_analysis(llm_client, result["reflection"], result["suggestions"], principles)
        if verification.passed:
            break
        logger.warning(
            "verification failed on attempt %d/%d for log %s: %s",
            attempt + 1,
            MAX_SYNTHESIS_ATTEMPTS,
            log.log_id,
            verification.issues,
        )
        retry_issues = verification.issues

    if result is None or verification is None or not verification.passed:
        logger.info(
            "falling back to non-LLM template for log %s after %d attempts", log.log_id, MAX_SYNTHESIS_ATTEMPTS
        )
        result = fallback_analysis(principles)
        verification_status = VerificationStatus.fallback_used
    else:
        verification_status = VerificationStatus.passed

    analysis = Analysis(
        log_id=log.log_id,
        retrieved_principle_ids=[p.principle_id for p in principles],
        reflection=result["reflection"],
        verification_status=verification_status,
    )
    db.add(analysis)
    db.flush()
    for s in result["suggestions"]:
        if s["principle_id"] not in by_id:
            continue
        db.add(
            Suggestion(
                analysis_id=analysis.analysis_id,
                principle_id=s["principle_id"],
                text=s["text"],
                explanation=s["explanation"],
            )
        )
    db.commit()
    db.refresh(analysis)
    return analysis


@router.get("/{analysis_id}", response_model=AnalysisRead)
def get_analysis(analysis_id: UUID, db: Session = Depends(get_db)) -> Analysis:
    analysis = db.get(Analysis, analysis_id)
    if analysis is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return analysis


@router.delete("/{analysis_id}", status_code=204)
def delete_analysis(analysis_id: UUID, db: Session = Depends(get_db)) -> None:
    """Lets a caller clear out a stale analysis (e.g. after logging another
    entry the same day) so a fresh one can be regenerated for the same log
    -- analyses.log_id is unique, so a new POST would otherwise 409. Cascades
    to the analysis's suggestions via ON DELETE CASCADE (see HANDOFF.md 9.8).
    """
    analysis = db.get(Analysis, analysis_id)
    if analysis is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    db.delete(analysis)
    db.commit()


@router.get("", response_model=list[AnalysisRead])
def list_analyses(log_id: UUID = Query(...), db: Session = Depends(get_db)) -> list[Analysis]:
    return db.query(Analysis).filter(Analysis.log_id == log_id).all()
