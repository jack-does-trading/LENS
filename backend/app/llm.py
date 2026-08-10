from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.error
import urllib.request
from typing import Protocol
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class LLMClient(Protocol):
    def generate(self, prompt: str, *, timeout: float | None = None) -> str: ...


class LLMError(RuntimeError):
    """Raised when the LLM provider fails or is unreachable."""


class OllamaLLMClient:
    """Local Ollama client for Step B/C LLM calls. Refuses any non-localhost
    host -- a user's daily log text and book principles must never leave
    their own machine through this client (architecture SS1's privacy stance
    on personal data), same rule tools/local_extraction applies to the book
    PDF itself.
    """

    def __init__(self, host: str, model: str, timeout: float = 60.0) -> None:
        parsed = urlparse(host)
        if parsed.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError(f"OllamaLLMClient only allows localhost hosts, got {host!r}")
        self._host = host.rstrip("/")
        self._model = model
        self._timeout = timeout

    def generate(self, prompt: str, *, timeout: float | None = None) -> str:
        request = urllib.request.Request(
            f"{self._host}/api/generate",
            data=json.dumps(
                {
                    "model": self._model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0.2},
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc
        text = payload.get("response")
        if not isinstance(text, str):
            raise LLMError(f"unexpected Ollama response shape: {payload!r}")
        return text


DEFAULT_GROQ_HOST = "https://api.groq.com/openai/v1"
# Free-tier safe common denominator across Groq's free models (some allow
# 60) -- only a starting guess, used until the first real response reports
# the account's actual limits via headers. Same reasoning as
# tools/local_extraction/model_client.py's GroqModelClient, ported here
# rather than imported: this app and that CLI are deliberately kept fully
# standalone from each other (architecture SS2/SS8).
_DEFAULT_GROQ_REQUESTS_PER_MINUTE = 30
_GROQ_MAX_429_RETRIES = 5
_FALLBACK_BACKOFF_BASE_SECONDS = 2.0
_FALLBACK_BACKOFF_MAX_SECONDS = 60.0

# Matches Go's time.Duration.String() format, which is what Groq's
# x-ratelimit-reset-* headers use (e.g. "7.66s", "1m0s") -- NOT a plain
# number of seconds and NOT an epoch timestamp.
_GO_DURATION_RE = re.compile(
    r"^(?:(?P<hours>\d+(?:\.\d+)?)h)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)m(?!s))?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)s)?"
    r"(?:(?P<millis>\d+(?:\.\d+)?)ms)?$"
)


def _parse_duration_seconds(value: object) -> float | None:
    """Parse a Groq rate-limit header into seconds from now. Accepts
    Go-duration strings ("7.66s", "1m0s") and, defensively, a bare number of
    seconds. Returns None -- never raises -- for anything unparseable."""
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
        float(match.group(name) or 0.0) for name in ("hours", "minutes", "seconds", "millis")
    )
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def _header_value(headers: object, name: str) -> object:
    try:
        return headers.get(name)  # type: ignore[union-attr]
    except AttributeError:
        return None


def _parse_float_header(headers: object, name: str) -> float | None:
    value = _header_value(headers, name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_duration_header(headers: object, name: str) -> float | None:
    return _parse_duration_seconds(_header_value(headers, name))


def _fallback_backoff_seconds(attempt: int) -> float:
    return (
        min(_FALLBACK_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _FALLBACK_BACKOFF_MAX_SECONDS)
        + random.uniform(0, 1)
    )


class GroqLLMClient:
    """Talks to Groq's hosted OpenAI-compatible chat-completions API for
    Step B/C (synthesis + verification) LLM calls.

    Deliberate, opt-in exception to OllamaLLMClient's local-only privacy
    guarantee above: choosing this client means every daily-log entry a user
    writes, plus the retrieved principle summaries, are sent to Groq's
    servers on every single analysis -- not a one-time offline book
    extraction, but live personal journal content on an ongoing basis. Only
    ever constructed when LLM_PROVIDER=groq is explicitly set (see
    app/routers/analyses.py's get_llm_client) -- never the default.

    Rate-limiting/retry logic is a direct port of
    tools/local_extraction/model_client.py's GroqModelClient (same
    two-tier throttle: a flat requests-per-minute floor plus adaptive
    waiting once response headers reveal the account's real remaining
    quota; same 429 Retry-After handling with capped-backoff fallback; same
    retry-on-transient-network-failure). See that module's docstring for the
    full reasoning -- reimplemented here rather than imported since this app
    and that CLI are deliberately standalone from each other.
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        host: str = DEFAULT_GROQ_HOST,
        timeout: float = 60.0,
        temperature: float = 0.2,
        requests_per_minute: float = _DEFAULT_GROQ_REQUESTS_PER_MINUTE,
    ) -> None:
        if not model:
            raise ValueError("model name is required")
        if not api_key:
            raise ValueError("a Groq API key is required (GROQ_API_KEY)")
        self._model = model
        self._api_key = api_key
        self._host = host.rstrip("/")
        self._timeout = timeout
        self._temperature = temperature
        self._min_interval = 60.0 / requests_per_minute
        self._last_request_at: float | None = None
        self._known_remaining_requests: float | None = None
        self._known_requests_reset_at: float | None = None
        self._known_remaining_tokens: float | None = None
        self._known_tokens_reset_at: float | None = None

    def _update_rate_limit_state(self, headers: object) -> None:
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
                    "Groq %s quota exhausted for this window -- waiting %.1fs before the next request",
                    reason,
                    sleep_for,
                )
            time.sleep(sleep_for)

    def generate(self, prompt: str, *, timeout: float | None = None) -> str:
        request_timeout = timeout or self._timeout

        for attempt in range(1, _GROQ_MAX_429_RETRIES + 1):
            self._throttle()
            self._last_request_at = time.monotonic()
            request = urllib.request.Request(
                f"{self._host}/chat/completions",
                data=json.dumps(
                    {
                        "model": self._model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": self._temperature,
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=request_timeout) as response:
                    self._update_rate_limit_state(response.headers)
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                self._update_rate_limit_state(exc.headers)
                if exc.code == 429 and attempt < _GROQ_MAX_429_RETRIES:
                    retry_after = _parse_duration_header(exc.headers, "retry-after")
                    if retry_after is None:
                        retry_after = _fallback_backoff_seconds(attempt)
                    # Silent otherwise: without this, a rate-limit backoff and
                    # a genuine hang look identical from the outside.
                    logger.warning(
                        "Groq rate-limited (429, attempt %d/%d): retrying in %.0fs",
                        attempt,
                        _GROQ_MAX_429_RETRIES,
                        retry_after,
                    )
                    time.sleep(retry_after)
                    continue
                body = exc.read().decode("utf-8")
                raise LLMError(f"Groq request failed: HTTP {exc.code}: {body}") from exc
            except urllib.error.URLError as exc:
                # Transient network failure below the HTTP layer (DNS blip,
                # connection reset, read timeout) -- no response, so no
                # Retry-After to honor. Retried the same way as a
                # missing-Retry-After 429, same attempt budget.
                if attempt >= _GROQ_MAX_429_RETRIES:
                    raise LLMError(f"Groq request failed: {exc}") from exc
                backoff = _fallback_backoff_seconds(attempt)
                logger.warning(
                    "Groq request failed (%s, attempt %d/%d): retrying in %.0fs",
                    exc,
                    attempt,
                    _GROQ_MAX_429_RETRIES,
                    backoff,
                )
                time.sleep(backoff)
                continue

            try:
                return payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError(f"unexpected Groq response shape: {payload!r}") from exc

        raise AssertionError("unreachable -- loop always returns or raises")


class FakeLLMClient:
    """Test double: returns a queued canned response per call, records prompts."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts_seen: list[str] = []

    def generate(self, prompt: str, *, timeout: float | None = None) -> str:
        self.prompts_seen.append(prompt)
        if not self._responses:
            raise LLMError("FakeLLMClient has no more queued responses")
        return self._responses.pop(0)
