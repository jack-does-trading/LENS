from __future__ import annotations

import logging
import random
import re
import time
from typing import Callable, Protocol
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_HOST = "http://localhost:11434"

# By default this tool never makes a network call off the user's own machine
# (see architecture §2/§8 and §5's rejection of hosted extraction APIs).
# Enforced here, not just by convention: constructing an OllamaModelClient
# against anything else raises. GroqModelClient below is a deliberate, opt-in
# exception to that default — see its docstring — not a loosening of this
# check for OllamaModelClient itself.
_ALLOWED_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}

_HTTP_PAYLOAD_TOO_LARGE = 413


class PayloadTooLargeError(RuntimeError):
    """Raised when the backend rejects a request as too large (HTTP 413).

    This is a raw request-body size cap enforced independently of any
    token/context-window limit a client already accounts for — observed in
    practice on Groq for prompts that fit comfortably inside the chosen
    context-length tier. There's no generic way to shrink an opaque prompt
    string here; callers that know what they're sending (a batch of
    candidates, a chunk of book text, ...) should catch this and retry with
    less content per call.
    """


class LocalModelClient(Protocol):
    """Interface a local-model backend must satisfy.

    Deliberately minimal and swappable: pipeline code depends on this Protocol,
    not on any concrete client, so tests can inject a fake with deterministic
    canned responses instead of depending on a real, non-deterministic local model.
    """

    def generate(
        self, prompt: str, *, context_length: int | None = None, timeout: float | None = None
    ) -> str:
        """Return the raw text completion for `prompt`.

        `context_length`, if given, overrides the client's default context
        window for this call only. `timeout`, if given, overrides the
        client's default request timeout for this call only. Both exist for
        the aggregation pass, whose prompt/output size scales with how many
        chunks the book was split into and can't be sized correctly at
        client-construction time.
        """
        ...


class OllamaModelClient:
    """Talks to a local Ollama server only."""

    def __init__(
        self,
        model: str,
        host: str = DEFAULT_OLLAMA_HOST,
        timeout: float = 300.0,
        temperature: float = 0.2,
        context_length: int = 8192,
    ) -> None:
        if not model:
            raise ValueError(
                "model name is required with no default — pass --model explicitly"
            )
        hostname = urlparse(host).hostname
        if hostname not in _ALLOWED_HOSTNAMES:
            raise ValueError(
                f"refusing to talk to non-localhost host {host!r}; "
                "this tool only ever calls a local model on this machine"
            )
        self.model = model
        self._host = host.rstrip("/")
        self._timeout = timeout
        # Lower than Ollama's default (0.8) — this pipeline needs consistently
        # well-formed structured JSON, not creative variation.
        self._temperature = temperature
        # Ollama's own default context window (2048-4096 tokens) is a runtime
        # default, not a hard model limit — and it's smaller than some single
        # book chapters once tokenized. Request a much larger window explicitly
        # so a chapter-sized chunk doesn't get silently truncated/context-shifted.
        self._context_length = context_length

    def generate(
        self, prompt: str, *, context_length: int | None = None, timeout: float | None = None
    ) -> str:
        response = requests.post(
            f"{self._host}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": self._temperature,
                    "num_ctx": context_length or self._context_length,
                },
            },
            timeout=timeout or self._timeout,
        )
        if response.status_code == _HTTP_PAYLOAD_TOO_LARGE:
            raise PayloadTooLargeError(
                "Ollama rejected the request as too large (413 Payload Too Large)"
            )
        response.raise_for_status()
        payload = response.json()
        return payload.get("response", "")


DEFAULT_GROQ_HOST = "https://api.groq.com/openai/v1"
# Free-tier default: 30 RPM across Groq's free models (some allow 60, but 30
# is the safe common denominator — see the model list in the tool's README).
# This is only a starting guess, used until the first real response tells us
# the account's actual limits (see _update_rate_limit_state below) — per-model
# free-tier limits vary a lot (e.g. tokens-per-minute ranges from 6K to 70K
# across Groq's free models), and RPM alone isn't the binding constraint for
# most of them; a single book-chapter chunk can eat most of a small model's
# whole per-minute token budget in one request.
DEFAULT_GROQ_REQUESTS_PER_MINUTE = 30
_GROQ_MAX_429_RETRIES = 5
# Fallback backoff for the rare case Groq's 429 response doesn't include a
# Retry-After header (it normally always does — this is defense in depth,
# not the primary mechanism). Exponential with a cap and jitter, standard
# practice to avoid hammering a server that's already saying "not yet" and
# to avoid every retry landing on the exact same schedule.
_FALLBACK_BACKOFF_BASE_SECONDS = 2.0
_FALLBACK_BACKOFF_MAX_SECONDS = 60.0

# Matches Go's time.Duration.String() format, which is what Groq's
# x-ratelimit-reset-* headers use (e.g. "7.66s", "1m0s", "2h3m4s") — NOT a
# plain number of seconds and NOT an epoch timestamp.
_GO_DURATION_RE = re.compile(
    r"^(?:(?P<hours>\d+(?:\.\d+)?)h)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)m(?!s))?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)s)?"
    r"(?:(?P<millis>\d+(?:\.\d+)?)ms)?$"
)


def _parse_duration_seconds(value: object) -> float | None:
    """Parse a Groq rate-limit reset header into seconds from now.

    Accepts Go-duration strings ("7.66s", "1m0s") and, defensively, a bare
    number of seconds. Returns None — never raises — for anything else
    (missing header, unexpected type/format), so a header this tool doesn't
    understand just means "no proactive info available", not a crash.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    match = _GO_DURATION_RE.match(value.strip())
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds, millis = (
        float(match.group(name) or 0.0)
        for name in ("hours", "minutes", "seconds", "millis")
    )
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def _header_value(headers: object, name: str) -> object:
    """`headers.get(name)`, tolerating anything that isn't dict-like."""
    try:
        return headers.get(name)  # type: ignore[union-attr]
    except AttributeError:
        return None


def _parse_float_header(headers: object, name: str) -> float | None:
    """Best-effort float read of a response header. Never raises."""
    value = _header_value(headers, name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_duration_header(headers: object, name: str) -> float | None:
    """Best-effort Go-duration read of a response header. Never raises."""
    return _parse_duration_seconds(_header_value(headers, name))


def _fallback_backoff_seconds(attempt: int) -> float:
    """Exponential backoff with a cap and jitter, for retryable failures
    that come with no server-provided wait time to honor (a 429 missing
    Retry-After, or a connection-level failure like a DNS blip)."""
    return (
        min(_FALLBACK_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _FALLBACK_BACKOFF_MAX_SECONDS)
        + random.uniform(0, 1)
    )


class GroqModelClient:
    """Talks to Groq's hosted OpenAI-compatible chat-completions API.

    Deliberate, opt-in exception to this tool's local-only default (see
    module docstring above and architecture §5/§8): choosing this client
    means real book chapter text leaves the machine and is sent to Groq for
    every extraction and aggregation call. Only ever constructed when the
    CLI is invoked with `--provider groq` — never the default path.

    Rate-limited client-side two ways, combined:

    1. A flat `requests_per_minute` floor (free-tier default: 30 RPM) applied
       from the very first call, before this client knows anything about the
       account's real limits.
    2. Adaptive throttling from that point on: every Groq response carries
       `x-ratelimit-remaining-{requests,tokens}` / `x-ratelimit-reset-{requests,tokens}`
       headers reporting the account's actual, current quota. This client
       tracks that state and, if a window is already exhausted, sleeps until
       it resets *before* sending the next request — instead of firing
       blind and eating a guaranteed 429. This matters because the flat RPM
       floor alone is a bad proxy for the real constraint: several free
       models cap tokens-per-minute far tighter than requests-per-minute
       (e.g. 6K TPM for llama-3.1-8b-instant), and one book-chapter chunk
       can consume most of that in a single request regardless of spacing.

    Reactively retries on HTTP 429 honoring `Retry-After` (falling back to
    capped exponential backoff with jitter on the rare response that omits
    it), up to `_GROQ_MAX_429_RETRIES` — a 429 past that (e.g. the free
    tier's daily request/token cap, not just the per-minute one) is raised
    rather than retried forever, since daily-cap backoffs can be hours long.

    Also retries (same backoff, same attempt budget) on a transient network
    failure below the HTTP layer — DNS resolution blip, connection reset,
    read timeout — rather than letting one Wi-Fi hiccup kill a multi-hour,
    multi-book run; the last such error past the retry budget is raised.
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        host: str = DEFAULT_GROQ_HOST,
        timeout: float = 300.0,
        temperature: float = 0.2,
        requests_per_minute: float = DEFAULT_GROQ_REQUESTS_PER_MINUTE,
    ) -> None:
        if not model:
            raise ValueError(
                "model name is required with no default — pass --model explicitly, "
                "e.g. llama-3.3-70b-versatile"
            )
        if not api_key:
            raise ValueError(
                "a Groq API key is required — pass --groq-api-key or set the "
                "GROQ_API_KEY environment variable"
            )
        self.model = model
        self._api_key = api_key
        self._host = host.rstrip("/")
        self._timeout = timeout
        self._temperature = temperature
        self._min_interval = 60.0 / requests_per_minute
        self._last_request_at: float | None = None
        # Adaptive rate-limit state, learned from response headers — None
        # until the first real response. See _update_rate_limit_state.
        self._known_remaining_requests: float | None = None
        self._known_requests_reset_at: float | None = None
        self._known_remaining_tokens: float | None = None
        self._known_tokens_reset_at: float | None = None

    def _update_rate_limit_state(self, headers: object) -> None:
        """Learn the account's real quota from a response's rate-limit
        headers, if present and parseable. Best-effort: any missing/odd
        header just leaves the previous known state (or None) untouched."""
        remaining_requests = _parse_float_header(headers, "x-ratelimit-remaining-requests")
        reset_requests = _parse_duration_header(headers, "x-ratelimit-reset-requests")
        if remaining_requests is not None and reset_requests is not None:
            self._known_remaining_requests = remaining_requests
            self._known_requests_reset_at = time.monotonic() + reset_requests

        remaining_tokens = _parse_float_header(headers, "x-ratelimit-remaining-tokens")
        reset_tokens = _parse_duration_header(headers, "x-ratelimit-reset-tokens")
        if remaining_tokens is not None and reset_tokens is not None:
            self._known_remaining_tokens = remaining_tokens
            self._known_tokens_reset_at = time.monotonic() + reset_tokens

    def _throttle(self) -> None:
        # Cheap early-out in the common case (first call, or an account
        # we've never seen exhausted-quota headers from) so this stays a
        # no-op rather than always paying for time.monotonic().
        if (
            self._last_request_at is None
            and self._known_requests_reset_at is None
            and self._known_tokens_reset_at is None
        ):
            return

        now = time.monotonic()
        sleep_for = 0.0
        reason: str | None = None

        if self._last_request_at is not None:
            interval_wait = self._min_interval - (now - self._last_request_at)
            if interval_wait > sleep_for:
                sleep_for, reason = interval_wait, None

        if (
            self._known_remaining_requests is not None
            and self._known_remaining_requests <= 0
            and self._known_requests_reset_at is not None
        ):
            wait = self._known_requests_reset_at - now
            if wait > sleep_for:
                sleep_for, reason = wait, "request"

        if (
            self._known_remaining_tokens is not None
            and self._known_remaining_tokens <= 0
            and self._known_tokens_reset_at is not None
        ):
            wait = self._known_tokens_reset_at - now
            if wait > sleep_for:
                sleep_for, reason = wait, "token"

        if sleep_for > 0:
            if reason is not None:
                logger.info(
                    "Groq %s quota exhausted for this window — waiting %.1fs "
                    "for it to reset before sending the next request",
                    reason,
                    sleep_for,
                )
            time.sleep(sleep_for)

    def generate(
        self, prompt: str, *, context_length: int | None = None, timeout: float | None = None
    ) -> str:
        # context_length is part of the shared Protocol (Ollama's num_ctx
        # knob) but Groq manages context server-side per model and exposes no
        # equivalent request parameter — accepted for interface compatibility
        # and intentionally unused here.
        del context_length
        request_timeout = timeout or self._timeout

        for attempt in range(1, _GROQ_MAX_429_RETRIES + 1):
            self._throttle()
            self._last_request_at = time.monotonic()
            try:
                response = requests.post(
                    f"{self._host}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": self._temperature,
                    },
                    timeout=request_timeout,
                )
            except requests.exceptions.RequestException as exc:
                # Transient network failure (DNS blip, connection reset,
                # read timeout, ...) below the HTTP layer entirely — no
                # response, so no Retry-After to honor. Previously this
                # propagated straight out of generate() and killed the
                # whole multi-hour, multi-book run on one Wi-Fi hiccup.
                # Retried the same way as a missing-Retry-After 429: capped
                # exponential backoff with jitter, same attempt budget.
                if attempt >= _GROQ_MAX_429_RETRIES:
                    raise
                backoff = _fallback_backoff_seconds(attempt)
                logger.warning(
                    "Groq request failed (%s: %s, attempt %d/%d): retrying in %.0fs",
                    type(exc).__name__,
                    exc,
                    attempt,
                    _GROQ_MAX_429_RETRIES,
                    backoff,
                )
                time.sleep(backoff)
                continue

            self._update_rate_limit_state(response.headers)
            if response.status_code == 429 and attempt < _GROQ_MAX_429_RETRIES:
                retry_after = _parse_duration_header(response.headers, "retry-after")
                if retry_after is None:
                    # Groq normally always sends Retry-After on a 429 — this
                    # is defense in depth for the rare response that omits
                    # it, so we back off instead of hammering it again
                    # immediately.
                    retry_after = _fallback_backoff_seconds(attempt)
                # Silent otherwise: without this, a rate-limit backoff and a
                # genuine hang look identical in the CLI's log output — the
                # last thing printed is extraction.py's "requested candidate
                # extraction" line, then nothing, for as long as retry_after.
                logger.warning(
                    "Groq rate-limited (429, attempt %d/%d): retrying in %.0fs",
                    attempt,
                    _GROQ_MAX_429_RETRIES,
                    retry_after,
                )
                time.sleep(retry_after)
                continue
            if response.status_code == _HTTP_PAYLOAD_TOO_LARGE:
                raise PayloadTooLargeError(
                    "Groq rejected the request as too large (413 Payload Too "
                    f"Large, model={self.model}) — the prompt fit the chosen "
                    "context length but exceeded Groq's raw request-body "
                    "size cap, a separate, undocumented limit"
                )
            response.raise_for_status()
            payload = response.json()
            return payload["choices"][0]["message"]["content"]

        raise AssertionError("unreachable — loop always returns or raises")


class FakeModelClient:
    """Test double for LocalModelClient. Never touches the network.

    Provide either a fixed queue of `responses` (consumed in order, one per
    `generate` call) or a `responder(prompt) -> str` callable for
    prompt-dependent behavior.
    """

    def __init__(
        self,
        responses: list[str] | None = None,
        responder: Callable[[str], str] | None = None,
    ) -> None:
        if responses is None and responder is None:
            raise ValueError("provide either `responses` or `responder`")
        self._responses = list(responses) if responses is not None else None
        self._responder = responder
        self.prompts_seen: list[str] = []
        self.context_lengths_seen: list[int | None] = []
        self.timeouts_seen: list[float | None] = []

    def generate(
        self, prompt: str, *, context_length: int | None = None, timeout: float | None = None
    ) -> str:
        self.prompts_seen.append(prompt)
        self.context_lengths_seen.append(context_length)
        self.timeouts_seen.append(timeout)
        if self._responder is not None:
            return self._responder(prompt)
        if not self._responses:
            raise AssertionError("FakeModelClient ran out of canned responses")
        return self._responses.pop(0)
