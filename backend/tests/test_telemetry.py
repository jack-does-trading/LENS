"""Phase 0 of the eval harness: proving the pipeline can be measured at all.

The property under test throughout is the one the README names -- that falling
back to the template stops being indistinguishable from success.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.constraints import MAX_QUOTE_WORDS
from app.llm import FakeLLMClient, LLMError
from app.models import Principle
from app.synthesis import build_synthesis_prompt
from app.telemetry import (
    ENTAILMENT,
    SYNTHESIS,
    InstrumentedLLMClient,
    JsonFormatter,
    classify_issue,
    classify_prompt,
    log_event,
    safe,
)
from app.verification import _build_entailment_prompt, _rule_based_issues


def _principle(pid: str = "p1", **kw) -> Principle:
    """An unsaved Principle -- verification and prompt building never touch
    the DB, so no session is needed here.
    """
    defaults = dict(
        principle_id=pid,
        book_id="b1",
        name="Living Consciously",
        summary="Pay attention to facts and goals rather than operating on autopilot.",
        applies_to_tags=["awareness"],
    )
    defaults.update(kw)
    return Principle(**defaults)


def _suggestion(pid: str = "p1", text: str = "Write down one fact.", explanation: str = "Because awareness.") -> dict:
    return {"principle_id": pid, "text": text, "explanation": explanation}


# --- prompt classification -------------------------------------------------


def test_classify_prompt_recognises_the_real_entailment_prompt() -> None:
    prompt = _build_entailment_prompt("You did fine.", [_suggestion()], [_principle()])
    assert classify_prompt(prompt) == ENTAILMENT


def test_classify_prompt_recognises_the_real_synthesis_prompt(seed_book) -> None:
    prompt = build_synthesis_prompt(
        seed_book, [_principle()], [{"time": "09:00", "action": "ran", "category": "health"}], 3
    )
    assert classify_prompt(prompt) == SYNTHESIS


def test_classify_prompt_defaults_to_synthesis_for_anything_unrecognised() -> None:
    # Misattributing an unknown prompt to the cheap, frequent stage is the
    # safer default: it can't make the entailment count look healthier than it
    # is, which is the direction that would hide a broken verifier.
    assert classify_prompt("who knows what this is") == SYNTHESIS


# --- InstrumentedLLMClient -------------------------------------------------


def test_instrumented_client_passes_through_and_records_both_stages() -> None:
    inner = FakeLLMClient(responses=["synth-out", '{"verdict": "PASS"}'])
    traced = InstrumentedLLMClient(inner)

    assert traced.generate("SYSTEM:\nYou are an advisor") == "synth-out"
    traced.generate(_build_entailment_prompt("r", [_suggestion()], [_principle()]))

    assert [c.kind for c in traced.calls] == [SYNTHESIS, ENTAILMENT]
    assert traced.count(SYNTHESIS) == 1
    assert traced.count(ENTAILMENT) == 1
    assert inner.prompts_seen  # the wrapper really delegated
    assert all(c.duration_ms >= 0 for c in traced.calls)


def test_instrumented_client_records_then_reraises() -> None:
    traced = InstrumentedLLMClient(FakeLLMClient(responses=[]))

    with pytest.raises(LLMError):
        traced.generate("SYSTEM: anything")

    # Re-raising unchanged is what keeps the router's retry loop working; the
    # error is captured as data rather than swallowed.
    assert len(traced.calls) == 1
    assert traced.calls[0].error is not None
    assert "LLMError" in traced.calls[0].error
    assert traced.errors == [traced.calls[0].error]


def test_instrumented_client_does_not_retain_prompt_text_by_default() -> None:
    """Prompts embed the user's journal entries verbatim. Sizes and timings are
    enough for every metric in Phase 0, so the text is not retained unless a
    caller explicitly opts in.
    """
    traced = InstrumentedLLMClient(FakeLLMClient(responses=["ok"]))
    traced.generate("SYSTEM: i had a terrible day and here is why")

    call = traced.calls[0]
    assert call.prompt is None
    assert call.response is None
    assert call.prompt_chars == len("SYSTEM: i had a terrible day and here is why")
    assert call.response_chars == 2


def test_instrumented_client_captures_text_when_asked() -> None:
    traced = InstrumentedLLMClient(FakeLLMClient(responses=["ok"]), capture_text=True)
    traced.generate("SYSTEM: hello")
    assert traced.calls[0].prompt == "SYSTEM: hello"
    assert traced.calls[0].response == "ok"


def test_total_duration_sums_every_call() -> None:
    traced = InstrumentedLLMClient(FakeLLMClient(responses=["a", "b"]))
    traced.generate("SYSTEM: one")
    traced.generate("SYSTEM: two")
    assert traced.total_duration_ms == sum(c.duration_ms for c in traced.calls)


# --- issue classification --------------------------------------------------


def test_every_rule_the_verifier_can_emit_has_a_classification() -> None:
    """Drift guard. The issue strings interpolate ids and counts, so the
    classifier matches on substrings -- which silently rots the moment someone
    rewords a message in verification.py. Rather than assert against hardcoded
    copies of those strings, this drives the real `_rule_based_issues` into
    every failure branch and demands each result classify to something.
    """
    long_quote = '"' + " ".join(["word"] * (MAX_QUOTE_WORDS + 1)) + '"'
    cases = [
        # too many suggestions
        ("ok", [_suggestion() for _ in range(4)], {"p1"}),
        # unknown principle_id
        ("ok", [_suggestion(pid="nope")], {"p1"}),
        # empty text / empty explanation
        ("ok", [_suggestion(text="   ")], {"p1"}),
        ("ok", [_suggestion(explanation="   ")], {"p1"}),
        # first person
        ("I had a good day and my mood was fine.", [_suggestion()], {"p1"}),
        # too many quotes
        ('He said "one thing" and also "another thing".', [_suggestion()], {"p1"}),
        # quote too long
        (f"The book says {long_quote}.", [_suggestion()], {"p1"}),
    ]

    produced: set[str] = set()
    for reflection, suggestions, valid in cases:
        issues = _rule_based_issues(reflection, suggestions, valid)
        assert issues, f"expected a rule to fire for {reflection!r}"
        for issue in issues:
            kind = classify_issue(issue)
            assert kind != "other", f"unclassified verification issue: {issue!r}"
            produced.add(kind)

    assert produced == {
        "too_many_suggestions",
        "unknown_principle_id",
        "empty_suggestion_text",
        "empty_explanation",
        "first_person_voice",
        "too_many_quotes",
        "quote_too_long",
    }


def test_classify_issue_covers_the_two_non_rule_failure_paths() -> None:
    # Emitted by verify_analysis and by the router's exception branch
    # respectively -- neither comes from _rule_based_issues.
    assert (
        classify_issue("entailment check: reflection claims something not supported by the principles")
        == "entailment_failed"
    )
    assert classify_issue("the previous response was rejected: boom") == "synthesis_error"


def test_classify_issue_falls_back_rather_than_raising() -> None:
    assert classify_issue("a brand new message nobody anticipated") == "other"


# --- structured logging ----------------------------------------------------


def test_log_event_emits_parseable_json_with_promoted_fields(caplog) -> None:
    logger = logging.getLogger("test.telemetry")
    formatter = JsonFormatter()

    with caplog.at_level(logging.WARNING, logger="test.telemetry"):
        log_event(logger, logging.WARNING, "analysis.fallback_used", log_id="abc", attempts=5)

    payload = json.loads(formatter.format(caplog.records[-1]))
    assert payload["event"] == "analysis.fallback_used"
    assert payload["level"] == "WARNING"
    # Promoted to the top level so a log aggregator can filter on them without
    # regexing a formatted sentence.
    assert payload["log_id"] == "abc"
    assert payload["attempts"] == 5


def test_json_formatter_handles_a_plain_record() -> None:
    record = logging.LogRecord("x", logging.INFO, "f", 1, "plain message", None, None)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "plain message"


# --- fail-open guarantee ---------------------------------------------------


def test_safe_swallows_telemetry_failures() -> None:
    """Verification fails closed to protect the user from an ungrounded answer.
    Telemetry fails *open*: a logging bug must never cost a user their
    analysis.
    """

    @safe("exploding exporter")
    def boom() -> None:
        raise RuntimeError("langfuse is down")

    assert boom() is None


def test_running_migrations_does_not_disable_the_app_loggers() -> None:
    """Regression guard for a silent, whole-suite failure mode.

    alembic/env.py calls logging.config.fileConfig, whose `disable_existing_loggers`
    default is True -- so migrating the schema used to disable every app.* logger
    for the rest of the process. That is invisible on Render, where migrations run
    as their own command, and load-bearing under pytest, where tests/conftest.py
    migrates in-process before the session starts. The effect was that any test
    asserting on an application log line quietly stopped asserting anything, and
    passed or failed depending on test ordering.

    This project's entire Phase 0 argument is that the log line emitted at the
    moment of fallback is what turns a silent outage into a visible one. A
    logging config that can switch those lines off deserves a test.
    """
    for name in ("app", "app.telemetry", "app.tracing", "app.routers.analyses"):
        assert not logging.getLogger(name).disabled, (
            f"logger {name!r} is disabled; something called fileConfig() with "
            "disable_existing_loggers left at its default"
        )
