"""Langfuse export for the analysis pipeline.

Phase 0 (app/telemetry.py) made the pipeline's own behaviour visible in the
database and the logs. This module ships the same information somewhere a human
can actually look at one request end to end: prompt, response, retry, verdict,
latency, in a tree.

Three decisions here are deliberate and each inverts something the rest of this
backend does. All three are worth reading before changing anything.

**1. Tracing fails OPEN.** Everything else in this pipeline fails closed --
verification refuses output it cannot ground, ingestion refuses an unsigned
request, the synthesis loop falls back to a template rather than return
unverified text. That stance is right when the failure would hand a user a bad
answer. It is exactly wrong here: a Langfuse outage that turned into a fallback
analysis would trade a real feature for a diagnostic. So every call in this
module is wrapped, and a broken exporter can only ever cost you the trace.

**2. It speaks HTTP directly, over urllib, rather than using the langfuse SDK.**
The whole backend's outbound HTTP is stdlib by design (Voyage in
app/embeddings.py, Groq in app/llm.py, the extraction tools) and this is the one
place where the alternative would have a real cost: the SDK would have to go in
requirements.txt, because unlike the rest of the eval harness this code runs in
production, and it pulls an OpenTelemetry tree into a free-tier Render build for
what is one authenticated POST. The ingestion endpoint is a documented public
API. Going direct also means every failure mode is in this file, which matters
for a component whose contract is "never raise".

**3. Sending is the caller's job, after the response.** `export_analysis` builds
a batch and hands it back; the router queues it on FastAPI's BackgroundTasks so
the POST happens after the user already has their analysis. Tracing inside the
request path would put a third-party network call on the critical path of a
feature that fails open -- which is a contradiction, not a design.

**Privacy.** Prompts contain users' journal entries verbatim, so enabling this
sends personal data to a third party. Architecture section 9 already made that
tradeoff once, for Groq, and wrote it up honestly rather than quietly; section
10 carries the second instance. `LANGFUSE_MASK_INPUTS=1` is the mitigation:
structure, ids, scores, latencies and token counts still go, and every piece of
user text is replaced by a salted-looking digest. You keep every operational
metric and lose only the ability to read the actual prompt in the UI. It is not
the default in either direction -- `langfuse_enabled` is off until configured,
and masking is a separate switch, because "send nothing" and "send structure"
are different decisions and collapsing them into one flag would hide one of them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.telemetry import ENTAILMENT, SYNTHESIS, LLMCall, safe

logger = logging.getLogger(__name__)

INGESTION_PATH = "/api/public/ingestion"
# Short on purpose. This runs after the response has been sent, but it still
# occupies a worker, and a Langfuse that has stopped answering must not be able
# to hold one open.
DEFAULT_TIMEOUT = 5.0

MASK_PREFIX = "masked:"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mask(text: str) -> str:
    """Replace user text with a stable digest.

    Stable rather than random so the same entry traces to the same token across
    requests -- you can still see "this user logged the same thing twice" and
    still group by prompt, which is most of what the text was for operationally.
    Truncated to 16 hex characters: enough to distinguish entries, not enough to
    be worth attacking, and it is a digest of already-short text either way, so
    this is a privacy improvement and not a privacy guarantee. Say so out loud
    rather than implying the original is unrecoverable by someone determined.
    """
    return MASK_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class TraceConfig:
    """Resolved from app.config.settings, but taken as an argument so tests and
    scripts can construct one without touching the environment."""

    public_key: str
    secret_key: str
    host: str = "https://cloud.langfuse.com"
    mask_inputs: bool = True
    environment: str = "production"
    timeout: float = DEFAULT_TIMEOUT

    @property
    def is_usable(self) -> bool:
        return bool(self.public_key and self.secret_key and self.host)

    def text(self, value: str | None) -> str | None:
        if value is None:
            return None
        return mask(value) if self.mask_inputs else value


def config_from_settings(settings: Any) -> TraceConfig | None:
    """None when tracing is off or unconfigured -- the router treats that as
    "do nothing", so a deployment with no Langfuse keys behaves exactly as it
    did before this module existed."""
    if not getattr(settings, "langfuse_enabled", False):
        return None
    cfg = TraceConfig(
        public_key=settings.langfuse_public_key or "",
        secret_key=settings.langfuse_secret_key or "",
        host=settings.langfuse_host,
        mask_inputs=settings.langfuse_mask_inputs,
        environment=settings.langfuse_environment,
    )
    if not cfg.is_usable:
        logger.warning(
            "LANGFUSE_ENABLED=1 but keys/host are incomplete; tracing stays off"
        )
        return None
    return cfg


@dataclass
class AnalysisTrace:
    """One analysis request, as a tree.

        trace: analysis  {log_id, book_id, provider, model, prompt_version}
        |-- span:       retrieval        -> principle_ids, latency
        |-- generation: synthesis #1     -> prompt, response, latency
        |-- span:       verification #1  -> rule issues, entailment verdict
        |-- generation: synthesis #2     -> the retry, carrying retry_issues
        `-- score:      verification_status, synthesis_attempts, latency_ms

    Synthesis and verification calls are `generation` events rather than plain
    spans because that is the event type Langfuse costs and tokenises; the
    retrieval step is a span because no model runs in it (architecture section
    3's Step A is deliberately LLM-free, and a trace that implied otherwise
    would misrepresent the design).
    """

    log_id: str
    book_id: str
    provider: str
    model: str
    prompt_version: str
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: str = field(default_factory=_now)
    retrieved_principle_ids: list[str] = field(default_factory=list)
    retrieval_ms: int | None = None
    entries_text: list[str] = field(default_factory=list)
    calls: list[LLMCall] = field(default_factory=list)
    attempts: int = 0
    verification_status: str = ""
    verification_issues: list[str] = field(default_factory=list)
    latency_ms: int = 0
    fallback_used: bool = False

    def _event(self, event_type: str, body: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(uuid.uuid4()),
            "type": event_type,
            "timestamp": _now(),
            "body": body,
        }

    def to_batch(self, cfg: TraceConfig) -> list[dict[str, Any]]:
        events = [
            self._event(
                "trace-create",
                {
                    "id": self.trace_id,
                    "name": "analysis",
                    "timestamp": self.started_at,
                    "environment": cfg.environment,
                    "tags": [f"env:{cfg.environment}", f"provider:{self.provider}"],
                    "input": {"entries": [cfg.text(t) for t in self.entries_text]},
                    "metadata": {
                        # log_id is a UUID the app already treats as an opaque
                        # handle, so it identifies the request without carrying
                        # content. It is what makes a trace joinable back to the
                        # analyses row, which is the entire point of exporting.
                        "log_id": self.log_id,
                        "book_id": self.book_id,
                        "provider": self.provider,
                        "model": self.model,
                        "prompt_version": self.prompt_version,
                        "masked": cfg.mask_inputs,
                    },
                },
            )
        ]

        if self.retrieved_principle_ids or self.retrieval_ms is not None:
            events.append(
                self._event(
                    "span-create",
                    {
                        "id": str(uuid.uuid4()),
                        "traceId": self.trace_id,
                        "name": "retrieval",
                        "startTime": self.started_at,
                        "environment": cfg.environment,
                        # No `input`: the retrieval query text is the user's day,
                        # and it already appears once on the trace. Repeating it
                        # per span multiplies the exposure for no added insight.
                        "output": {"principle_ids": self.retrieved_principle_ids},
                        "metadata": {
                            "latency_ms": self.retrieval_ms,
                            "returned": len(self.retrieved_principle_ids),
                            "llm_used": False,
                        },
                    },
                )
            )

        synthesis_n = verification_n = 0
        for call in self.calls:
            if call.kind == SYNTHESIS:
                synthesis_n += 1
                name = f"synthesis #{synthesis_n}"
            else:
                verification_n += 1
                name = f"entailment #{verification_n}"
            events.append(
                self._event(
                    "generation-create",
                    {
                        "id": str(uuid.uuid4()),
                        "traceId": self.trace_id,
                        "name": name,
                        "environment": cfg.environment,
                        "model": self.model,
                        "input": cfg.text(call.prompt),
                        "output": cfg.text(call.response),
                        "level": "ERROR" if call.error else "DEFAULT",
                        "statusMessage": call.error,
                        "metadata": {
                            "kind": call.kind,
                            "latency_ms": call.duration_ms,
                            "prompt_chars": call.prompt_chars,
                            "response_chars": call.response_chars,
                        },
                    },
                )
            )

        # Scores, not metadata. A score is what Langfuse can chart and alert on,
        # and "what fraction of analyses fell back this week" is the exact
        # question that went unanswered through the Groq outage.
        for name, value, comment in (
            ("verification_passed", 0.0 if self.fallback_used else 1.0, self.verification_status),
            ("synthesis_attempts", float(self.attempts), None),
            ("latency_ms", float(self.latency_ms), None),
        ):
            body = {
                "id": str(uuid.uuid4()),
                "traceId": self.trace_id,
                "name": name,
                "value": value,
                "dataType": "NUMERIC",
                "environment": cfg.environment,
            }
            if comment:
                body["comment"] = comment
            events.append(self._event("score-create", body))

        if self.verification_issues:
            # The issue *names*, never the interpolated strings: those embed ids,
            # counts and excerpts of the generated text. app.telemetry's
            # classify_issue folds them into the small set of rules that exist,
            # which is also what makes them countable.
            from app.telemetry import classify_issue

            events.append(
                self._event(
                    "score-create",
                    {
                        "id": str(uuid.uuid4()),
                        "traceId": self.trace_id,
                        "name": "verification_issues",
                        "value": float(len(self.verification_issues)),
                        "dataType": "NUMERIC",
                        "environment": cfg.environment,
                        "comment": ",".join(
                            sorted({classify_issue(i) for i in self.verification_issues})
                        ),
                    },
                )
            )
        return events


class LangfuseExporter:
    """One authenticated POST to the ingestion endpoint. Never raises."""

    def __init__(self, cfg: TraceConfig) -> None:
        self._cfg = cfg
        token = base64.b64encode(f"{cfg.public_key}:{cfg.secret_key}".encode()).decode()
        self._auth = f"Basic {token}"

    @property
    def url(self) -> str:
        return self._cfg.host.rstrip("/") + INGESTION_PATH

    def _post(self, batch: list[dict[str, Any]]) -> int:
        request = urllib.request.Request(
            self.url,
            data=json.dumps({"batch": batch}, default=str).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": self._auth},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._cfg.timeout) as response:
            return response.status

    @safe("langfuse export")
    def send(self, trace: AnalysisTrace) -> int | None:
        """Returns the HTTP status, or None if anything at all went wrong.

        `safe` swallows every exception -- including a timeout, a 401 from
        rotated keys, and a JSON encoding bug in this file -- and logs it at
        WARNING. That is the fail-open contract, and it is why this method has
        no retry: a retry inside a background task that already cannot report
        failure just spends a worker twice.
        """
        batch = trace.to_batch(self._cfg)
        try:
            return self._post(batch)
        except urllib.error.HTTPError as exc:
            # Read the body before re-raising into `safe`, so the log line says
            # *why* Langfuse rejected the batch instead of just "HTTP 400".
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


@safe("langfuse trace build")
def export_analysis(cfg: TraceConfig | None, trace: AnalysisTrace) -> int | None:
    """What the router queues. A no-op when tracing is unconfigured."""
    if cfg is None:
        return None
    return LangfuseExporter(cfg).send(trace)
