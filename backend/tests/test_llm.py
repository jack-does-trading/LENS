import io
import json as json_module
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest

from app.llm import (
    GroqLLMClient,
    LLMError,
    _parse_duration_seconds,
)


class _FakeHttpResponse:
    """Minimal stand-in for urllib.request.urlopen()'s context-manager
    result -- just enough for GroqLLMClient.generate's usage."""

    def __init__(self, payload: dict, headers: dict[str, str] | None = None) -> None:
        self._body = json_module.dumps(payload).encode("utf-8")
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _http_error(code: int, body: bytes = b"{}", headers: dict[str, str] | None = None) -> HTTPError:
    hdrs = Message()
    for key, value in (headers or {}).items():
        hdrs[key] = value
    return HTTPError("https://api.groq.com/openai/v1/chat/completions", code, "error", hdrs, io.BytesIO(body))


def _chat_response(text: str, headers: dict[str, str] | None = None) -> _FakeHttpResponse:
    return _FakeHttpResponse({"choices": [{"message": {"content": text}}]}, headers=headers)


def test_parse_duration_seconds_handles_go_duration_format() -> None:
    assert _parse_duration_seconds("7.66s") == pytest.approx(7.66)
    assert _parse_duration_seconds("1m0s") == pytest.approx(60.0)
    assert _parse_duration_seconds("2h3m4s") == pytest.approx(2 * 3600 + 3 * 60 + 4)


def test_parse_duration_seconds_returns_none_for_unparseable_input() -> None:
    assert _parse_duration_seconds(None) is None
    assert _parse_duration_seconds("") is None
    assert _parse_duration_seconds("not-a-duration") is None


def test_groq_client_rejects_empty_model() -> None:
    with pytest.raises(ValueError):
        GroqLLMClient(model="", api_key="key")


def test_groq_client_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError):
        GroqLLMClient(model="llama-3.3-70b-versatile", api_key="")


def test_groq_client_returns_message_content_on_success() -> None:
    client = GroqLLMClient(model="llama-3.3-70b-versatile", api_key="key")
    with patch("app.llm.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _chat_response('{"reflection": "..."}')
        result = client.generate("some prompt")
    assert result == '{"reflection": "..."}'


def test_groq_client_sends_bearer_auth_and_model() -> None:
    client = GroqLLMClient(model="llama-3.3-70b-versatile", api_key="secret-key")
    with patch("app.llm.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _chat_response("ok")
        client.generate("hello")
    request = mock_urlopen.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer secret-key"
    body = json_module.loads(request.data.decode("utf-8"))
    assert body["model"] == "llama-3.3-70b-versatile"
    assert body["messages"] == [{"role": "user", "content": "hello"}]


def test_groq_client_requests_json_object_mode() -> None:
    # Unlike OllamaLLMClient's forced format="json", Groq has no default
    # JSON guarantee -- request it explicitly (see app/llm.py's comment on
    # this, and app/json_extraction.py for the parsing-side half of the fix).
    client = GroqLLMClient(model="m", api_key="k")
    with patch("app.llm.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _chat_response("ok")
        client.generate("hello")
    request = mock_urlopen.call_args.args[0]
    body = json_module.loads(request.data.decode("utf-8"))
    assert body["response_format"] == {"type": "json_object"}


def test_groq_client_throttles_between_calls() -> None:
    client = GroqLLMClient(model="m", api_key="k", requests_per_minute=30)
    with (
        patch("app.llm.urllib.request.urlopen") as mock_urlopen,
        patch("app.llm.time.sleep") as mock_sleep,
        patch("app.llm.time.monotonic", side_effect=[0.0, 0.1, 0.1]),
    ):
        mock_urlopen.side_effect = [_chat_response("a"), _chat_response("b")]
        client.generate("first")
        client.generate("second")
    # min_interval = 60/30 = 2s; second call starts 0.1s after the first ->
    # waits ~1.9s.
    mock_sleep.assert_called_once()
    (waited,) = mock_sleep.call_args.args
    assert waited == pytest.approx(1.9, abs=0.01)


def test_groq_client_retries_on_429_honoring_retry_after_header() -> None:
    client = GroqLLMClient(model="m", api_key="k")
    with (
        patch("app.llm.urllib.request.urlopen") as mock_urlopen,
        patch("app.llm.time.sleep") as mock_sleep,
        # Plenty of wall-clock time between attempts, so the flat RPM
        # throttle contributes no extra wait -- isolates the 429-specific
        # backoff this test is actually checking (same subtlety as
        # test_embeddings.py's equivalent Voyage AI retry tests).
        patch("app.llm.time.monotonic", side_effect=[0.0, 100.0, 100.0]),
    ):
        mock_urlopen.side_effect = [
            _http_error(429, headers={"retry-after": "2s"}),
            _chat_response("ok"),
        ]
        result = client.generate("prompt")
    assert result == "ok"
    mock_sleep.assert_called_once_with(2.0)


def test_groq_client_falls_back_to_backoff_when_retry_after_missing() -> None:
    client = GroqLLMClient(model="m", api_key="k")
    with (
        patch("app.llm.urllib.request.urlopen") as mock_urlopen,
        patch("app.llm.time.sleep") as mock_sleep,
        patch("app.llm.time.monotonic", side_effect=[0.0, 100.0, 100.0]),
        patch("app.llm.random.uniform", return_value=0.5),
    ):
        mock_urlopen.side_effect = [_http_error(429), _chat_response("ok")]
        result = client.generate("prompt")
    assert result == "ok"
    mock_sleep.assert_called_once_with(pytest.approx(2.5))


def test_groq_client_raises_after_exhausting_429_retries() -> None:
    client = GroqLLMClient(model="m", api_key="k")
    with (
        patch("app.llm.urllib.request.urlopen") as mock_urlopen,
        patch("app.llm.time.sleep"),
    ):
        mock_urlopen.side_effect = [_http_error(429) for _ in range(10)]
        with pytest.raises(LLMError, match="429"):
            client.generate("prompt")


def test_groq_client_retries_on_503_then_succeeds() -> None:
    # Groq's free tier returns transient 5xx under load -- treated the same
    # as a 429, not raised immediately like other 4xx codes are.
    client = GroqLLMClient(model="m", api_key="k")
    with (
        patch("app.llm.urllib.request.urlopen") as mock_urlopen,
        patch("app.llm.time.sleep") as mock_sleep,
        patch("app.llm.time.monotonic", side_effect=[0.0, 100.0, 100.0]),
        patch("app.llm.random.uniform", return_value=0.5),
    ):
        mock_urlopen.side_effect = [_http_error(503), _chat_response("ok")]
        result = client.generate("prompt")
    assert result == "ok"
    mock_sleep.assert_called_once_with(pytest.approx(2.5))


def test_groq_client_raises_immediately_on_non_retryable_4xx() -> None:
    client = GroqLLMClient(model="m", api_key="k")
    with (
        patch("app.llm.urllib.request.urlopen") as mock_urlopen,
        patch("app.llm.time.sleep") as mock_sleep,
    ):
        mock_urlopen.side_effect = [_http_error(401, body=b'{"error": "bad key"}')]
        with pytest.raises(LLMError, match="401"):
            client.generate("prompt")
    mock_sleep.assert_not_called()


def test_groq_client_retries_on_network_error_then_succeeds() -> None:
    client = GroqLLMClient(model="m", api_key="k")
    with (
        patch("app.llm.urllib.request.urlopen") as mock_urlopen,
        patch("app.llm.time.sleep") as mock_sleep,
        patch("app.llm.time.monotonic", side_effect=[0.0, 100.0, 100.0]),
    ):
        mock_urlopen.side_effect = [URLError("DNS lookup failed"), _chat_response("ok")]
        result = client.generate("prompt")
    assert result == "ok"
    mock_sleep.assert_called_once()


def test_groq_client_raises_after_exhausting_network_error_retries() -> None:
    client = GroqLLMClient(model="m", api_key="k")
    with (
        patch("app.llm.urllib.request.urlopen") as mock_urlopen,
        patch("app.llm.time.sleep"),
    ):
        mock_urlopen.side_effect = [URLError("still down") for _ in range(10)]
        with pytest.raises(LLMError, match="still down"):
            client.generate("prompt")


def test_groq_client_proactively_waits_when_token_quota_exhausted() -> None:
    client = GroqLLMClient(model="m", api_key="k")
    with (
        patch("app.llm.urllib.request.urlopen") as mock_urlopen,
        patch("app.llm.time.sleep") as mock_sleep,
        patch("app.llm.time.monotonic", side_effect=[0.0, 0.0, 5.0, 5.0]),
    ):
        mock_urlopen.side_effect = [
            _chat_response("a", headers={"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "30s"}),
            _chat_response("b"),
        ]
        client.generate("first")
        client.generate("second")
    # First response reports the token window is already exhausted, reset
    # in 30s from t=0 -> known_tokens_reset_at = 30. Second call's throttle
    # check happens at t=5 -> should wait ~25s for it to clear.
    mock_sleep.assert_called_once()
    (waited,) = mock_sleep.call_args.args
    assert waited == pytest.approx(25.0, abs=0.5)


def test_groq_client_raises_llm_error_on_unexpected_response_shape() -> None:
    client = GroqLLMClient(model="m", api_key="k")
    with patch("app.llm.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _FakeHttpResponse({"unexpected": "shape"})
        with pytest.raises(LLMError, match="unexpected Groq response shape"):
            client.generate("prompt")
