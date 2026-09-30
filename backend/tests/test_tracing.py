"""Langfuse export: the shape of a trace, the masking switch, and fail-open.

The third of those is the one that matters most. Every other component in this
pipeline fails closed, and that is correct; this one must not. A tracing bug that
can cost a user their analysis has traded a real feature for a diagnostic, so the
fail-open behaviour is asserted directly rather than assumed from a decorator.
"""

from __future__ import annotations

import json
import urllib.error
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.embeddings import FakeEmbeddingClient
from app.llm import FakeLLMClient
from app.main import app
from app.models import Analysis, Book, DailyLog, Principle, ReviewStatus, User
from app.routers.analyses import get_embedding_client, get_llm_client
from app.telemetry import ENTAILMENT, SYNTHESIS, LLMCall
from app.tracing import (
    MASK_PREFIX,
    AnalysisTrace,
    LangfuseExporter,
    TraceConfig,
    config_from_settings,
    export_analysis,
    mask,
)

SECRET = "I told my manager about the layoffs"

CFG = TraceConfig(public_key="pk", secret_key="sk", host="https://lf.test", mask_inputs=False)
MASKED = TraceConfig(public_key="pk", secret_key="sk", host="https://lf.test", mask_inputs=True)


def _trace(**overrides) -> AnalysisTrace:
    defaults = dict(
        log_id=str(uuid.uuid4()),
        book_id="atomic-habits",
        provider="groq",
        model="openai/gpt-oss-120b",
        prompt_version="synthesis-v3+verification-v2",
        retrieved_principle_ids=["identity-habits", "start-small"],
        retrieval_ms=41,
        entries_text=[SECRET],
        calls=[
            LLMCall(kind=SYNTHESIS, duration_ms=900, prompt_chars=1200, response_chars=400,
                    prompt=f"prompt with {SECRET}", response="a reflection"),
            LLMCall(kind=ENTAILMENT, duration_ms=300, prompt_chars=800, response_chars=20,
                    prompt="You are a fact-checker", response='{"verdict": "PASS"}'),
        ],
        attempts=1,
        verification_status="passed",
        latency_ms=1400,
    )
    defaults.update(overrides)
    return AnalysisTrace(**defaults)


def _by_type(batch: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for event in batch:
        out.setdefault(event["type"], []).append(event["body"])
    return out


# --- config ---------------------------------------------------------------


def test_tracing_is_off_unless_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment with no Langfuse keys must behave exactly as it did before
    this module existed."""
    monkeypatch.setattr(settings, "langfuse_enabled", False)
    assert config_from_settings(settings) is None


def test_enabled_but_incomplete_keys_does_not_half_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Half-configured is a configuration mistake, and returning a TraceConfig
    with an empty secret would turn it into a 401 on every single analysis."""
    monkeypatch.setattr(settings, "langfuse_enabled", True)
    monkeypatch.setattr(settings, "langfuse_public_key", "pk")
    monkeypatch.setattr(settings, "langfuse_secret_key", None)
    assert config_from_settings(settings) is None


def test_masking_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prompts carry journal entries verbatim, so the safe default has to be the
    private one. A deployment that wants raw text in the UI says so explicitly
    rather than discovering after the fact that it has been shipping it."""
    monkeypatch.setattr(settings, "langfuse_enabled", True)
    monkeypatch.setattr(settings, "langfuse_public_key", "pk")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk")
    cfg = config_from_settings(settings)
    assert cfg is not None and cfg.mask_inputs is True


# --- trace shape ----------------------------------------------------------


def test_the_batch_has_one_trace_a_retrieval_span_and_a_generation_per_call() -> None:
    batch = _trace().to_batch(CFG)
    groups = _by_type(batch)
    assert len(groups["trace-create"]) == 1
    assert len(groups["span-create"]) == 1
    assert len(groups["generation-create"]) == 2
    assert {g["name"] for g in groups["generation-create"]} == {"synthesis #1", "entailment #1"}


def test_every_event_hangs_off_the_same_trace_id() -> None:
    """An event with the wrong traceId is silently orphaned by Langfuse -- it is
    accepted, stored, and never shown under the trace."""
    trace = _trace()
    for event in trace.to_batch(CFG):
        if event["type"] != "trace-create":
            assert event["body"]["traceId"] == trace.trace_id


def test_retrieval_is_a_span_not_a_generation() -> None:
    """Step A is deliberately LLM-free (architecture §3). A trace that recorded
    it as a generation would misrepresent the design and invent token costs."""
    span = _by_type(_trace().to_batch(CFG))["span-create"][0]
    assert span["name"] == "retrieval"
    assert span["metadata"]["llm_used"] is False
    assert span["output"]["principle_ids"] == ["identity-habits", "start-small"]


def test_retries_are_numbered_so_a_trace_shows_the_self_correction() -> None:
    calls = [
        LLMCall(kind=SYNTHESIS, duration_ms=1, prompt_chars=1, response_chars=1),
        LLMCall(kind=ENTAILMENT, duration_ms=1, prompt_chars=1, response_chars=1),
        LLMCall(kind=SYNTHESIS, duration_ms=1, prompt_chars=1, response_chars=1),
        LLMCall(kind=ENTAILMENT, duration_ms=1, prompt_chars=1, response_chars=1),
    ]
    names = [g["name"] for g in _by_type(_trace(calls=calls).to_batch(CFG))["generation-create"]]
    assert names == ["synthesis #1", "entailment #1", "synthesis #2", "entailment #2"]


def test_a_failed_call_is_exported_at_error_level() -> None:
    calls = [LLMCall(kind=SYNTHESIS, duration_ms=5, prompt_chars=1, response_chars=0,
                     error="LLMError: connection refused")]
    generation = _by_type(_trace(calls=calls).to_batch(CFG))["generation-create"][0]
    assert generation["level"] == "ERROR"
    assert "connection refused" in generation["statusMessage"]


def test_fallback_is_exported_as_a_score_not_buried_in_metadata() -> None:
    """'What fraction of analyses fell back this week' is the exact question that
    went unanswered through the Groq outage. A score is what Langfuse can chart
    and alert on; metadata is not."""
    scores = {s["name"]: s for s in _by_type(
        _trace(fallback_used=True, verification_status="fallback_used", attempts=5).to_batch(CFG)
    )["score-create"]}
    assert scores["verification_passed"]["value"] == 0.0
    assert scores["verification_passed"]["comment"] == "fallback_used"
    assert scores["synthesis_attempts"]["value"] == 5.0
    assert scores["latency_ms"]["value"] == 1400.0


def test_verification_issues_are_exported_as_rule_names_not_raw_strings() -> None:
    """The raw strings interpolate ids, counts and excerpts of generated text.
    classify_issue folds them into the small set of rules that actually exist,
    which is both countable and free of content."""
    trace = _trace(
        verification_issues=[
            "suggestion cites unknown principle_id 'made-up-id'",
            'reflection has a 19-word quote, exceeds 15-word limit',
        ]
    )
    issues = next(
        s for s in _by_type(trace.to_batch(CFG))["score-create"] if s["name"] == "verification_issues"
    )
    assert issues["value"] == 2.0
    assert issues["comment"] == "quote_too_long,unknown_principle_id"
    assert "made-up-id" not in json.dumps(trace.to_batch(CFG))


def test_environment_tags_every_event_so_eval_runs_do_not_pollute_production() -> None:
    cfg = TraceConfig(public_key="pk", secret_key="sk", mask_inputs=True, environment="eval")
    for event in _trace().to_batch(cfg):
        assert event["body"]["environment"] == "eval"


# --- masking --------------------------------------------------------------


def test_masking_removes_every_trace_of_the_entry_text() -> None:
    """The assertion that matters: not "the input field is masked", but "the
    secret does not appear anywhere in the payload". A single unmasked field
    somewhere in the tree defeats the whole switch."""
    payload = json.dumps(_trace().to_batch(MASKED))
    assert SECRET not in payload
    assert mask(SECRET) in payload


def test_unmasked_mode_really_does_send_the_text() -> None:
    """The inverse, asserted too -- a masking switch that masked unconditionally
    would be indistinguishable from a working one until someone needed to read a
    prompt in the UI."""
    payload = json.dumps(_trace().to_batch(CFG))
    assert SECRET in payload


def test_masking_keeps_every_operational_metric() -> None:
    """The stated tradeoff: you lose the ability to read the prompt, and nothing
    else. If masking also dropped latencies or ids it would not be a mitigation,
    it would be a downgrade."""
    groups = _by_type(_trace().to_batch(MASKED))
    trace_body = groups["trace-create"][0]
    assert trace_body["metadata"]["book_id"] == "atomic-habits"
    assert trace_body["metadata"]["masked"] is True
    assert groups["span-create"][0]["metadata"]["latency_ms"] == 41
    assert groups["generation-create"][0]["metadata"]["latency_ms"] == 900
    assert groups["generation-create"][0]["metadata"]["prompt_chars"] == 1200


def test_the_mask_is_stable_so_repeat_entries_still_group() -> None:
    assert mask(SECRET) == mask(SECRET)
    assert mask(SECRET) != mask(SECRET + ".")
    assert mask(SECRET).startswith(MASK_PREFIX)


# --- fail open ------------------------------------------------------------


def test_a_dead_langfuse_returns_none_rather_than_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("app.tracing.urllib.request.urlopen", boom)
    assert LangfuseExporter(CFG).send(_trace()) is None


def test_an_http_error_is_swallowed_with_the_body_in_the_log(caplog) -> None:
    class FakeHTTPError(urllib.error.HTTPError):
        def __init__(self) -> None:
            super().__init__("https://lf.test", 400, "Bad Request", {}, None)

        def read(self) -> bytes:
            return b'{"errors": ["unknown field"]}'

    exporter = LangfuseExporter(CFG)
    exporter._post = lambda batch: (_ for _ in ()).throw(FakeHTTPError())  # type: ignore[assignment]
    with caplog.at_level("WARNING"):
        assert exporter.send(_trace()) is None
    assert "unknown field" in caplog.text


def test_a_bug_in_trace_building_is_also_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`safe` wraps export_analysis itself, not just the POST. A TypeError while
    serialising a trace must cost the trace, never the analysis."""
    monkeypatch.setattr(
        AnalysisTrace, "to_batch", lambda self, cfg: (_ for _ in ()).throw(TypeError("nope"))
    )
    assert export_analysis(CFG, _trace()) is None


def test_export_is_a_no_op_when_unconfigured() -> None:
    assert export_analysis(None, _trace()) is None


def test_the_request_still_succeeds_when_the_exporter_explodes(
    client: TestClient,
    db_session: Session,
    seed_user: User,
    seed_book: Book,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end, through the real HTTP path, with tracing switched on and
    Langfuse unreachable. This is the whole contract in one test."""
    db_session.add(
        Principle(
            principle_id="living-consciously",
            book_id=seed_book.book_id,
            name="Living Consciously",
            summary="Pay attention to facts and goals rather than operating on autopilot.",
            applies_to_tags=["awareness"],
            review_status=ReviewStatus.human_reviewed,
        )
    )
    db_session.commit()
    from app.embeddings import generate_embeddings_for_book

    generate_embeddings_for_book(db_session, seed_book.book_id, FakeEmbeddingClient(dimension=1024))

    log = DailyLog(
        user_id=seed_user.user_id,
        chosen_book_id=seed_book.book_id,
        date="2026-09-30",
        entries=[{"time": "09:00", "action": SECRET, "category": "awareness"}],
        mood=3,
    )
    db_session.add(log)
    db_session.commit()
    db_session.refresh(log)

    monkeypatch.setattr(settings, "langfuse_enabled", True)
    monkeypatch.setattr(settings, "langfuse_public_key", "pk")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk")

    def boom(*_args, **_kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("app.tracing.urllib.request.urlopen", boom)

    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient(
        responses=[
            '{"reflection": "You were unsure of a decision today.", "suggestions": '
            '[{"text": "Notice the self-talk tonight.", "principle_id": "living-consciously", '
            '"explanation": "Awareness is the first step."}]}',
            '{"verdict": "PASS"}',
        ]
    )
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient(dimension=1024)
    try:
        response = client.post("/api/analyses", json={"log_id": str(log.log_id)})
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
        app.dependency_overrides.pop(get_embedding_client, None)

    assert response.status_code == 201, response.text
    assert response.json()["verification_status"] == "passed"
    assert db_session.query(Analysis).filter(Analysis.log_id == log.log_id).count() == 1


def test_prompts_are_not_retained_in_memory_when_masking_is_on(
    client: TestClient,
    db_session: Session,
    seed_user: User,
    seed_book: Book,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """capture_text is tied to the trace config and nothing else. With masking on
    -- the default -- the request must never hold the user's journal text in an
    LLMCall, because the exporter is only ever going to send a digest of it."""
    db_session.add(
        Principle(
            principle_id="living-consciously",
            book_id=seed_book.book_id,
            name="Living Consciously",
            summary="Pay attention to facts and goals rather than operating on autopilot.",
            applies_to_tags=["awareness"],
            review_status=ReviewStatus.human_reviewed,
        )
    )
    db_session.commit()
    from app.embeddings import generate_embeddings_for_book

    generate_embeddings_for_book(db_session, seed_book.book_id, FakeEmbeddingClient(dimension=1024))
    log = DailyLog(
        user_id=seed_user.user_id,
        chosen_book_id=seed_book.book_id,
        date="2026-09-30",
        entries=[{"time": "09:00", "action": SECRET, "category": "awareness"}],
        mood=3,
    )
    db_session.add(log)
    db_session.commit()
    db_session.refresh(log)

    monkeypatch.setattr(settings, "langfuse_enabled", True)
    monkeypatch.setattr(settings, "langfuse_public_key", "pk")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk")
    monkeypatch.setattr(settings, "langfuse_mask_inputs", True)

    sent: list[AnalysisTrace] = []
    monkeypatch.setattr(
        "app.routers.analyses.export_analysis", lambda cfg, trace: sent.append(trace)
    )

    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient(
        responses=[
            '{"reflection": "You were unsure of a decision today.", "suggestions": '
            '[{"text": "Notice the self-talk tonight.", "principle_id": "living-consciously", '
            '"explanation": "Awareness is the first step."}]}',
            '{"verdict": "PASS"}',
        ]
    )
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient(dimension=1024)
    try:
        assert client.post("/api/analyses", json={"log_id": str(log.log_id)}).status_code == 201
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
        app.dependency_overrides.pop(get_embedding_client, None)

    assert sent, "the exporter was never queued"
    trace = sent[0]
    assert trace.calls, "no LLM calls recorded"
    assert all(call.prompt is None and call.response is None for call in trace.calls)
    # The entry text is still on the trace object -- it is masked at serialisation
    # time, which is the one place that can guarantee it for every field at once.
    assert SECRET not in json.dumps(trace.to_batch(MASKED))
