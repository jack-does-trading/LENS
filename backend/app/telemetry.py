"""Pipeline telemetry: structured logging + an instrumented LLM client.

Why this module exists, in the README's own words:

    a fail-closed design is the right call, but it converts an outage into a
    quality regression. If your safe fallback is indistinguishable from
    success, add a log line or a metric at the moment you fall back.

Two Groq bugs once made every LLM call in this app fail, and nothing looked
broken, because the fallback template is a perfectly reasonable answer. The
pieces here are the "log line or a metric" half of that lesson:

  * `InstrumentedLLMClient` wraps any `LLMClient` and times every call,
    classifying it as synthesis or entailment. It is a Protocol implementer,
    so `app/synthesis.py` and `app/verification.py` need no changes at all --
    it goes in once, at `get_llm_client()`.
  * `JsonFormatter` / `log_event` turn the pipeline's printf log calls into
    records a log aggregator can filter and count.

Nothing here may raise into the request path. Telemetry that can break an
analysis is worse than no telemetry -- see `_safe` below.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.llm import LLMClient
from app.verification import ENTAILMENT_PROMPT_TEMPLATE

logger = logging.getLogger(__name__)

# Derived from the real template rather than hardcoded, so editing the
# entailment prompt can't silently start mislabelling every verification call
# as a synthesis one. The first line is stable and distinctive
# ("You are a fact-checker verifying that a generated reflection + suggestions").
_ENTAILMENT_MARKER = ENTAILMENT_PROMPT_TEMPLATE.split("\n", 1)[0].strip()

SYNTHESIS = "synthesis"
ENTAILMENT = "entailment"


def classify_prompt(prompt: str) -> str:
    """Which pipeline stage a prompt belongs to.

    Both stages go through the same one-method `LLMClient.generate`, so the
    prompt itself is the only available discriminator without changing the
    signatures of `synthesize_analysis`/`verify_analysis` -- which would couple
    Step B and Step C to telemetry, exactly the merge the architecture forbids.
    """
    return ENTAILMENT if prompt.lstrip().startswith(_ENTAILMENT_MARKER) else SYNTHESIS


@dataclass
class LLMCall:
    """One `generate()` round trip.

    `prompt`/`response` are only populated when the client was built with
    `capture_text=True`. They hold the user's journal entries verbatim, so the
    default is to keep sizes and timings but not the text itself; Phase 5's
    Langfuse exporter is what opts in, deliberately and per-deployment.
    """

    kind: str
    duration_ms: int
    prompt_chars: int
    response_chars: int
    error: str | None = None
    prompt: str | None = None
    response: str | None = None


class InstrumentedLLMClient:
    """`LLMClient` decorator that records timing and outcome of every call.

    Structural typing means this satisfies the `LLMClient` Protocol without
    inheriting from anything, and wrapping is transparent: exceptions are
    recorded and then re-raised unchanged, so the router's existing retry
    behaviour is untouched.
    """

    def __init__(self, inner: LLMClient, *, capture_text: bool = False) -> None:
        self._inner = inner
        self._capture_text = capture_text
        self.calls: list[LLMCall] = []

    def generate(self, prompt: str, *, timeout: float | None = None) -> str:
        kind = classify_prompt(prompt)
        started = time.monotonic()
        try:
            response = self._inner.generate(prompt, timeout=timeout)
        except Exception as exc:
            self._record(kind, started, prompt, "", error=f"{type(exc).__name__}: {exc}")
            raise
        self._record(kind, started, prompt, response)
        return response

    def _record(
        self, kind: str, started: float, prompt: str, response: str, error: str | None = None
    ) -> None:
        self.calls.append(
            LLMCall(
                kind=kind,
                duration_ms=int((time.monotonic() - started) * 1000),
                prompt_chars=len(prompt),
                response_chars=len(response),
                error=error,
                prompt=prompt if self._capture_text else None,
                response=response if self._capture_text else None,
            )
        )

    # --- read-side helpers, used by the router and by eval reporting -------

    def count(self, kind: str) -> int:
        return sum(1 for c in self.calls if c.kind == kind)

    @property
    def total_duration_ms(self) -> int:
        return sum(c.duration_ms for c in self.calls)

    @property
    def errors(self) -> list[str]:
        return [c.error for c in self.calls if c.error]


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with `log_event`'s fields promoted to the top
    level so a log aggregator can filter on `event` and `verification_status`
    directly rather than regexing a formatted message.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "lens_fields", {}) or {})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def log_event(log: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Emit a structured record. `event` is a stable machine-readable name
    (e.g. "analysis.fallback_used"), never an interpolated sentence -- the
    whole point is that it can be counted without parsing.
    """
    log.log(level, event, extra={"lens_fields": fields})


def configure_json_logging(level: int = logging.INFO) -> None:
    """Attach `JsonFormatter` to the root logger.

    Off unless `LENS_JSON_LOGS=1` (see app/main.py): tests and local dev read
    better with plain text, and unconditionally reconfiguring the root logger
    at import time would fight uvicorn's own handler setup.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def safe(fn_description: str):
    """Decorator: swallow and log any exception from a telemetry call.

    Deliberately the inverse of the pipeline's fail-closed stance. Verification
    failing closed protects the user from an ungrounded answer; telemetry
    failing closed would mean a logging bug costs a user their analysis, which
    trades a real feature for a diagnostic. Telemetry fails open, always.
    """

    def decorator(fn):
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 -- intentional catch-all
                logger.warning("telemetry: %s failed: %s", fn_description, exc)
                return None

        return wrapper

    return decorator


# --- issue classification --------------------------------------------------
# verification.py's issue strings interpolate ids, labels and counts, so they
# are unique per occurrence and useless as histogram keys. These patterns fold
# them back into the small set of *rules* that actually exist, which is what a
# quality dashboard (and the eval harness) needs to count.
_ISSUE_PATTERNS: list[tuple[str, str]] = [
    ("exceeds the top-", "too_many_suggestions"),
    ("cites unknown principle_id", "unknown_principle_id"),
    ("has empty text", "empty_suggestion_text"),
    ("has empty explanation", "empty_explanation"),
    ("is written in first person", "first_person_voice"),
    ("-quote limit", "too_many_quotes"),
    ("-word limit", "quote_too_long"),
    ("entailment check", "entailment_failed"),
    ("the previous response was rejected", "synthesis_error"),
]


def classify_issue(issue: str) -> str:
    """Map a verification issue string to a stable rule name."""
    for needle, name in _ISSUE_PATTERNS:
        if needle in issue:
            return name
    return "other"
