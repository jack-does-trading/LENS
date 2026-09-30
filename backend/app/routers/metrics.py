"""Operational quality metrics for the grounded-advice pipeline.

This endpoint exists because of a real incident. Two bugs made every Groq call
fail -- a Cloudflare 1010 block on urllib's default User-Agent, and an
entailment parse that couldn't survive Groq's markdown fences -- and the app
went on returning perfectly reasonable answers the whole time, because the
fail-closed fallback template *is* a reasonable answer. `/health` returned 200
(it never touches the pipeline), nothing 500'd, and there was no number
anywhere that would have shown it.

`fallback_rate` is that number.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Analysis, VerificationStatus
from app.schemas import QualityMetricsRead
from app.telemetry import classify_issue

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/quality", response_model=QualityMetricsRead)
def quality_metrics(
    window_days: int = Query(7, ge=1, le=365),
    db: Session = Depends(get_db),
) -> QualityMetricsRead:
    cutoff = func.now() - timedelta(days=window_days)
    rows = db.execute(
        select(
            Analysis.verification_status,
            Analysis.synthesis_attempts,
            Analysis.verification_issues,
            Analysis.llm_provider,
            Analysis.llm_model,
        ).where(Analysis.created_at >= cutoff)
    ).all()

    total = len(rows)
    if total == 0:
        return QualityMetricsRead(
            window_days=window_days,
            total_analyses=0,
            fallback_used=0,
            fallback_rate=0.0,
            mean_synthesis_attempts=None,
            first_attempt_pass_rate=None,
            issue_counts={},
            by_model={},
        )

    fallbacks = sum(1 for r in rows if r.verification_status == VerificationStatus.fallback_used)

    # Rows predating migration 008 have no attempt count. They are excluded
    # from the mean rather than counted as zero -- an unknown is not a one.
    attempts = [r.synthesis_attempts for r in rows if r.synthesis_attempts is not None]
    passed_rows = [r for r in rows if r.verification_status == VerificationStatus.passed]
    first_try = [r for r in passed_rows if r.synthesis_attempts == 1]

    issues: Counter[str] = Counter()
    for r in rows:
        for issue in r.verification_issues or []:
            issues[classify_issue(issue)] += 1

    models: Counter[str] = Counter()
    for r in rows:
        if r.llm_model:
            models[f"{r.llm_provider or '?'}/{r.llm_model}"] += 1

    return QualityMetricsRead(
        window_days=window_days,
        total_analyses=total,
        fallback_used=fallbacks,
        fallback_rate=round(fallbacks / total, 4),
        mean_synthesis_attempts=(round(sum(attempts) / len(attempts), 2) if attempts else None),
        # Of the analyses that passed, how many did so without burning a retry.
        # A falling number here is an early warning that a prompt or model
        # change is degrading quality long before it shows up as a fallback.
        first_attempt_pass_rate=(
            round(len(first_try) / len(passed_rows), 4) if passed_rows else None
        ),
        issue_counts=dict(issues.most_common()),
        by_model=dict(models.most_common()),
    )
