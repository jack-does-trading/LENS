import logging
import time
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.embeddings import EmbeddingClient, VoyageEmbeddingClient
from app.llm import GroqLLMClient, LLMClient, OllamaLLMClient
from app.models import Analysis, Book, DailyLog, Principle, ReviewStatus, Suggestion, VerificationStatus
from app.retrieval import LogEntryLike, retrieve_principles
from app.schemas import AnalysisCreate, AnalysisRead
from app.synthesis import PROMPT_VERSION as SYNTHESIS_PROMPT_VERSION
from app.synthesis import fallback_analysis, synthesize_analysis
from app.telemetry import InstrumentedLLMClient, log_event
from app.tracing import AnalysisTrace, config_from_settings, export_analysis
from app.verification import PROMPT_VERSION as VERIFICATION_PROMPT_VERSION
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
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    llm_client: LLMClient = Depends(get_llm_client),
    embedding_client: EmbeddingClient = Depends(get_embedding_client),
) -> Analysis:
    """Orchestrates Step A (retrieval) -> Step B (synthesis) -> Step C
    (verification) for one daily log, per architecture SS1/SS3. Retries
    synthesis once on a verification failure, then falls back to a
    non-LLM template -- never returns unverified LLM output.
    """
    started_at = time.monotonic()
    log = db.get(DailyLog, payload.log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="Daily log not found")
    if db.query(Analysis).filter(Analysis.log_id == log.log_id).first() is not None:
        raise HTTPException(status_code=409, detail="Analysis already exists for this daily log")

    book = db.get(Book, log.chosen_book_id)
    if book is None or book.review_status != ReviewStatus.human_reviewed:
        raise HTTPException(status_code=400, detail="Book is not reviewed/available for analysis")

    trace_config = config_from_settings(settings)

    entries = [LogEntryLike(category=e.get("category", ""), action=e.get("action", "")) for e in log.entries]
    retrieval_started = time.monotonic()
    principle_ids = retrieve_principles(db, book.book_id, entries, embedding_client, mood=log.mood)
    retrieval_ms = int((time.monotonic() - retrieval_started) * 1000)
    if not principle_ids:
        raise HTTPException(status_code=422, detail="No relevant principles could be retrieved for this log")

    by_id = {p.principle_id: p for p in db.query(Principle).filter(Principle.principle_id.in_(principle_ids)).all()}
    principles = [by_id[pid] for pid in principle_ids if pid in by_id]

    # Wrapped here rather than in get_llm_client() so telemetry survives the
    # dependency override tests use -- a metric that disappears whenever the
    # client is swapped is a metric you can't test.
    #
    # capture_text is tied to the trace config, and only to it: prompts hold the
    # user's journal entries verbatim, so keeping them in memory is something
    # this request does because an exporter is about to need them, never by
    # default. Tracing off, or masking on, and nothing retains the text at all.
    capture_text = trace_config is not None and not trace_config.mask_inputs
    traced = (
        llm_client
        if isinstance(llm_client, InstrumentedLLMClient)
        else InstrumentedLLMClient(llm_client, capture_text=capture_text)
    )

    result, verification = None, None
    retry_issues: list[str] | None = None
    attempts_used = 0
    for attempt in range(MAX_SYNTHESIS_ATTEMPTS):
        attempts_used = attempt + 1
        try:
            result = synthesize_analysis(
                traced, book, principles, log.entries, log.mood, retry_issues=retry_issues
            )
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "analysis.synthesis_raised",
                log_id=str(log.log_id),
                attempt=attempts_used,
                max_attempts=MAX_SYNTHESIS_ATTEMPTS,
                error=f"{type(exc).__name__}: {exc}",
            )
            result, verification = None, None
            retry_issues = [f"the previous response was rejected: {exc}"]
            continue
        verification = verify_analysis(traced, result["reflection"], result["suggestions"], principles)
        if verification.passed:
            break
        log_event(
            logger,
            logging.WARNING,
            "analysis.verification_failed",
            log_id=str(log.log_id),
            attempt=attempts_used,
            max_attempts=MAX_SYNTHESIS_ATTEMPTS,
            issues=verification.issues,
        )
        retry_issues = verification.issues

    if result is None or verification is None or not verification.passed:
        # The single most important log line in the app: this is the moment a
        # real answer silently becomes a template one. Without it an outage
        # looks exactly like normal operation (see README, "Two bugs made every
        # Groq call fail silently").
        log_event(
            logger,
            logging.WARNING,
            "analysis.fallback_used",
            log_id=str(log.log_id),
            attempts=attempts_used,
            issues=retry_issues or [],
            llm_errors=traced.errors,
        )
        result = fallback_analysis(principles)
        verification_status = VerificationStatus.fallback_used
        final_issues = retry_issues or []
    else:
        log_event(
            logger,
            logging.INFO,
            "analysis.passed",
            log_id=str(log.log_id),
            attempts=attempts_used,
            llm_calls=len(traced.calls),
            llm_ms=traced.total_duration_ms,
        )
        verification_status = VerificationStatus.passed
        final_issues = []

    analysis = Analysis(
        log_id=log.log_id,
        retrieved_principle_ids=[p.principle_id for p in principles],
        reflection=result["reflection"],
        verification_status=verification_status,
        synthesis_attempts=attempts_used,
        verification_issues=final_issues,
        llm_provider=settings.llm_provider,
        llm_model=(
            settings.groq_model if settings.llm_provider == "groq" else settings.ollama_model
        ),
        prompt_version=f"{SYNTHESIS_PROMPT_VERSION}+{VERIFICATION_PROMPT_VERSION}",
        latency_ms=int((time.monotonic() - started_at) * 1000),
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

    # Queued, not awaited. BackgroundTasks runs after the response is sent, so a
    # slow or dead Langfuse cannot add latency to an analysis, let alone fail
    # one -- see app/tracing.py on why this component alone fails open.
    if trace_config is not None:
        background.add_task(
            export_analysis,
            trace_config,
            AnalysisTrace(
                log_id=str(log.log_id),
                book_id=book.book_id,
                provider=analysis.llm_provider or "",
                model=analysis.llm_model or "",
                prompt_version=analysis.prompt_version or "",
                retrieved_principle_ids=list(principle_ids),
                retrieval_ms=retrieval_ms,
                entries_text=[e.get("action", "") for e in log.entries],
                calls=list(traced.calls),
                attempts=attempts_used,
                verification_status=verification_status.value,
                verification_issues=list(final_issues),
                latency_ms=analysis.latency_ms or 0,
                fallback_used=verification_status is VerificationStatus.fallback_used,
            ),
        )
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
