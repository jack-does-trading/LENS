from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from local_extraction.model_client import (
    FakeModelClient,
    GroqModelClient,
    OllamaModelClient,
    PayloadTooLargeError,
    _parse_duration_seconds,
)


def test_ollama_client_requires_model_name() -> None:
    with pytest.raises(ValueError, match="model name is required"):
        OllamaModelClient(model="")


def test_ollama_client_rejects_non_localhost_host() -> None:
    with pytest.raises(ValueError, match="non-localhost"):
        OllamaModelClient(model="llama3.1", host="http://example.com:11434")


def test_ollama_client_accepts_localhost_variants() -> None:
    OllamaModelClient(model="llama3.1", host="http://localhost:11434")
    OllamaModelClient(model="llama3.1", host="http://127.0.0.1:11434")


def test_ollama_client_sends_context_length_and_temperature_options() -> None:
    client = OllamaModelClient(
        model="llama3.2:3b", temperature=0.2, context_length=8192
    )
    mock_response = MagicMock()
    mock_response.json.return_value = {"response": "ok"}

    with patch("local_extraction.model_client.requests.post", return_value=mock_response) as mock_post:
        result = client.generate("some prompt")

    assert result == "ok"
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["options"]["num_ctx"] == 8192
    assert kwargs["json"]["options"]["temperature"] == 0.2
    assert kwargs["json"]["model"] == "llama3.2:3b"
    assert kwargs["json"]["prompt"] == "some prompt"
    assert kwargs["timeout"] == 300.0  # client's default, no override given


def test_ollama_client_context_length_and_timeout_overrides_apply_per_call() -> None:
    client = OllamaModelClient(model="llama3.2:3b", context_length=8192, timeout=300.0)
    mock_response = MagicMock()
    mock_response.json.return_value = {"response": "ok"}

    with patch("local_extraction.model_client.requests.post", return_value=mock_response) as mock_post:
        client.generate("prompt", context_length=32768, timeout=1800.0)

    _, kwargs = mock_post.call_args
    assert kwargs["json"]["options"]["num_ctx"] == 32768
    assert kwargs["timeout"] == 1800.0


def test_ollama_client_raises_payload_too_large_on_413() -> None:
    client = OllamaModelClient(model="llama3.2:3b")
    mock_response = MagicMock(status_code=413)

    with patch("local_extraction.model_client.requests.post", return_value=mock_response):
        with pytest.raises(PayloadTooLargeError):
            client.generate("prompt")


def test_ollama_client_default_context_length_is_memory_conscious() -> None:
    # Sized to comfortably fit --max-chunk-words (3000 words) plus prompt
    # overhead without pushing RAM usage too far on constrained hardware —
    # see README for the measured RSS trade-off at larger context sizes.
    client = OllamaModelClient(model="llama3.2:3b")
    assert client._context_length == 8192


def test_groq_client_requires_model_name() -> None:
    with pytest.raises(ValueError, match="model name is required"):
        GroqModelClient(model="", api_key="key")


def test_groq_client_requires_api_key() -> None:
    with pytest.raises(ValueError, match="Groq API key is required"):
        GroqModelClient(model="llama-3.3-70b-versatile", api_key="")


def test_groq_client_sends_bearer_auth_and_chat_payload() -> None:
    client = GroqModelClient(
        model="llama-3.3-70b-versatile", api_key="test-key", temperature=0.2
    )
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

    with patch("local_extraction.model_client.requests.post", return_value=mock_response) as mock_post:
        result = client.generate("some prompt")

    assert result == "ok"
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.groq.com/openai/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert kwargs["json"]["model"] == "llama-3.3-70b-versatile"
    assert kwargs["json"]["messages"] == [{"role": "user", "content": "some prompt"}]
    assert kwargs["json"]["temperature"] == 0.2
    assert kwargs["timeout"] == 300.0


def test_groq_client_timeout_override_applies_per_call() -> None:
    client = GroqModelClient(model="llama-3.3-70b-versatile", api_key="test-key")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

    with patch("local_extraction.model_client.requests.post", return_value=mock_response) as mock_post:
        client.generate("prompt", timeout=1800.0)

    _, kwargs = mock_post.call_args
    assert kwargs["timeout"] == 1800.0


def test_groq_client_retries_on_429_then_succeeds() -> None:
    client = GroqModelClient(model="llama-3.3-70b-versatile", api_key="test-key")
    rate_limited = MagicMock(status_code=429, headers={"retry-after": "0"})
    ok = MagicMock(status_code=200)
    ok.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

    with patch("local_extraction.model_client.requests.post", side_effect=[rate_limited, ok]) as mock_post, \
         patch("local_extraction.model_client.time.sleep"):
        result = client.generate("prompt")

    assert result == "ok"
    assert mock_post.call_count == 2


def test_groq_client_raises_after_exhausting_429_retries() -> None:
    client = GroqModelClient(model="llama-3.3-70b-versatile", api_key="test-key")
    rate_limited = MagicMock(status_code=429, headers={"retry-after": "0"})
    rate_limited.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "429 rate limited", response=rate_limited
    )

    with patch("local_extraction.model_client.requests.post", return_value=rate_limited), \
         patch("local_extraction.model_client.time.sleep"):
        with pytest.raises(requests.exceptions.HTTPError):
            client.generate("prompt")


def test_groq_client_raises_payload_too_large_on_413_without_retrying() -> None:
    # Unlike 429, a 413 isn't retryable by this client at all — the same
    # prompt will 413 again every time. It should surface immediately as a
    # distinct, catchable type so a caller that can shrink what it's
    # sending (aggregation.py, extraction.py) gets exactly one shot to react,
    # not 5 identical wasted attempts.
    client = GroqModelClient(model="llama-3.1-8b-instant", api_key="test-key")
    too_large = MagicMock(status_code=413, headers={})

    with patch(
        "local_extraction.model_client.requests.post", return_value=too_large
    ) as mock_post, patch("local_extraction.model_client.time.sleep"):
        with pytest.raises(PayloadTooLargeError):
            client.generate("prompt")

    assert mock_post.call_count == 1


@pytest.mark.parametrize(
    "raw, expected_seconds",
    [
        ("7.66s", pytest.approx(7.66)),
        ("1m0s", pytest.approx(60.0)),
        ("2h3m4s", pytest.approx(2 * 3600 + 3 * 60 + 4)),
        ("500ms", pytest.approx(0.5)),
        ("12", pytest.approx(12.0)),  # bare-number fallback, not a real Groq format
        ("0s", pytest.approx(0.0)),
    ],
)
def test_parse_duration_seconds_handles_go_duration_format(raw, expected_seconds) -> None:
    assert _parse_duration_seconds(raw) == expected_seconds


@pytest.mark.parametrize("raw", [None, "", "not-a-duration", "5x", 5.0, MagicMock()])
def test_parse_duration_seconds_returns_none_for_unparseable_input(raw) -> None:
    assert _parse_duration_seconds(raw) is None


def test_groq_client_records_rate_limit_state_from_response_headers() -> None:
    client = GroqModelClient(model="llama-3.1-8b-instant", api_key="test-key")
    response = MagicMock(
        status_code=200,
        headers={
            "x-ratelimit-remaining-requests": "29",
            "x-ratelimit-reset-requests": "2s",
            "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-reset-tokens": "5s",
        },
    )
    response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

    with patch("local_extraction.model_client.requests.post", return_value=response):
        client.generate("prompt")

    assert client._known_remaining_requests == 29
    assert client._known_remaining_tokens == 0
    assert client._known_tokens_reset_at is not None


def test_groq_client_proactively_waits_when_token_quota_exhausted() -> None:
    # A high RPM floor makes the plain interval-based throttle negligible
    # (0.06s), so any multi-second sleep observed below can only be the
    # proactive token-quota wait, not routine RPM spacing.
    client = GroqModelClient(
        model="llama-3.1-8b-instant", api_key="test-key", requests_per_minute=1000
    )
    exhausted = MagicMock(
        status_code=200,
        headers={"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "5s"},
    )
    exhausted.json.return_value = {"choices": [{"message": {"content": "first"}}]}
    ok = MagicMock(status_code=200, headers={})
    ok.json.return_value = {"choices": [{"message": {"content": "second"}}]}

    with patch(
        "local_extraction.model_client.requests.post", side_effect=[exhausted, ok]
    ) as mock_post, patch("local_extraction.model_client.time.sleep") as mock_sleep:
        client.generate("first prompt")
        client.generate("second prompt")

    assert mock_post.call_count == 2
    assert any(call.args[0] > 4.0 for call in mock_sleep.call_args_list)


def test_groq_client_falls_back_to_backoff_when_retry_after_missing() -> None:
    client = GroqModelClient(model="llama-3.3-70b-versatile", api_key="test-key")
    rate_limited = MagicMock(status_code=429, headers={})  # no retry-after
    ok = MagicMock(status_code=200, headers={})
    ok.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

    with patch(
        "local_extraction.model_client.requests.post", side_effect=[rate_limited, ok]
    ) as mock_post, patch("local_extraction.model_client.time.sleep") as mock_sleep:
        result = client.generate("prompt")

    assert result == "ok"
    assert mock_post.call_count == 2
    # exponential fallback (base 2s, attempt 1) plus up to 1s jitter — well
    # under the 60s cap, but not zero either.
    slept = mock_sleep.call_args_list[0].args[0]
    assert 2.0 <= slept <= 3.0


def test_groq_client_retries_on_connection_error_then_succeeds() -> None:
    client = GroqModelClient(model="llama-3.1-8b-instant", api_key="test-key")
    ok = MagicMock(status_code=200, headers={})
    ok.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

    with patch(
        "local_extraction.model_client.requests.post",
        side_effect=[requests.exceptions.ConnectionError("dns blip"), ok],
    ) as mock_post, patch("local_extraction.model_client.time.sleep") as mock_sleep:
        result = client.generate("prompt")

    assert result == "ok"
    assert mock_post.call_count == 2
    assert mock_sleep.called  # backed off before retrying, didn't hammer immediately


def test_groq_client_raises_after_exhausting_retries_on_connection_error() -> None:
    client = GroqModelClient(model="llama-3.1-8b-instant", api_key="test-key")

    with patch(
        "local_extraction.model_client.requests.post",
        side_effect=requests.exceptions.ConnectionError("dns still down"),
    ) as mock_post, patch("local_extraction.model_client.time.sleep"):
        with pytest.raises(requests.exceptions.ConnectionError):
            client.generate("prompt")

    assert mock_post.call_count == 5  # exhausted the full retry budget, not just one try


def test_groq_client_throttles_between_calls() -> None:
    client = GroqModelClient(
        model="llama-3.3-70b-versatile", api_key="test-key", requests_per_minute=30
    )
    ok = MagicMock(status_code=200)
    ok.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

    with patch("local_extraction.model_client.requests.post", return_value=ok), \
         patch("local_extraction.model_client.time.sleep") as mock_sleep, \
         patch("local_extraction.model_client.time.monotonic", side_effect=[0.0, 0.0, 0.1]):
        client.generate("first")
        client.generate("second")

    # second call is 0.1s after the first but the 30 RPM throttle requires 2s
    # spacing, so a sleep for the remaining ~1.9s must have been requested.
    assert mock_sleep.called


def test_fake_client_returns_canned_responses_in_order() -> None:
    client = FakeModelClient(responses=["first", "second"])

    assert client.generate("prompt a") == "first"
    assert client.generate("prompt b") == "second"
    assert client.prompts_seen == ["prompt a", "prompt b"]


def test_fake_client_responder_is_prompt_dependent() -> None:
    client = FakeModelClient(responder=lambda prompt: f"echo:{prompt}")

    assert client.generate("hi") == "echo:hi"


def test_fake_client_requires_responses_or_responder() -> None:
    with pytest.raises(ValueError):
        FakeModelClient()


def test_fake_client_raises_when_responses_exhausted() -> None:
    client = FakeModelClient(responses=["only one"])
    client.generate("first call")

    with pytest.raises(AssertionError):
        client.generate("second call")
