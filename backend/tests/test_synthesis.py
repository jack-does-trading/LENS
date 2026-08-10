import pytest

from app.llm import FakeLLMClient
from app.models import Book, BookTone, Principle
from app.synthesis import (
    SynthesisError,
    fallback_analysis,
    parse_synthesis_response,
    synthesize_analysis,
)


def _book() -> Book:
    return Book(
        book_id="six-pillars-of-self-esteem",
        title="The Six Pillars of Self-Esteem",
        author="Nathaniel Branden",
        core_thesis="Self-esteem grows from conscious, deliberate practices.",
        tone=BookTone.pragmatic,
    )


def _principles(n: int = 1) -> list[Principle]:
    all_principles = [
        Principle(
            principle_id="living-consciously",
            book_id="six-pillars-of-self-esteem",
            name="Living Consciously",
            summary="Pay attention to facts and goals rather than operating on autopilot.",
        ),
        Principle(
            principle_id="self-acceptance",
            book_id="six-pillars-of-self-esteem",
            name="Self-Acceptance",
            summary="Accept all facts of your reality, including thoughts you dislike.",
        ),
        Principle(
            principle_id="self-responsibility",
            book_id="six-pillars-of-self-esteem",
            name="Self-Responsibility",
            summary="Take responsibility for your own choices and actions.",
        ),
        Principle(
            principle_id="self-assertiveness",
            book_id="six-pillars-of-self-esteem",
            name="Self-Assertiveness",
            summary="Honor your own wants, needs, and values in action.",
        ),
    ]
    return all_principles[:n]


def test_parse_synthesis_response_extracts_valid_json() -> None:
    raw = (
        '```json\n{"reflection": "Because you mentioned redoing the report, this reflects the principle.", '
        '"suggestions": [{"text": "Try this.", "principle_id": "living-consciously", "explanation": "Because it builds awareness."}]}\n```'
    )
    result = parse_synthesis_response(raw)
    assert result["reflection"] == "Because you mentioned redoing the report, this reflects the principle."
    assert result["suggestions"][0]["principle_id"] == "living-consciously"
    assert result["suggestions"][0]["explanation"] == "Because it builds awareness."


def test_parse_synthesis_response_rejects_missing_reflection() -> None:
    with pytest.raises(SynthesisError):
        parse_synthesis_response('{"suggestions": [{"text": "x", "principle_id": "p1", "explanation": "y"}]}')


def test_parse_synthesis_response_rejects_empty_reflection() -> None:
    with pytest.raises(SynthesisError):
        parse_synthesis_response(
            '{"reflection": "", "suggestions": [{"text": "x", "principle_id": "p1", "explanation": "y"}]}'
        )
    with pytest.raises(SynthesisError):
        parse_synthesis_response(
            '{"reflection": "   ", "suggestions": [{"text": "x", "principle_id": "p1", "explanation": "y"}]}'
        )


def test_parse_synthesis_response_rejects_non_string_reflection() -> None:
    with pytest.raises(SynthesisError):
        parse_synthesis_response(
            '{"reflection": ["a", "b"], "suggestions": [{"text": "x", "principle_id": "p1", "explanation": "y"}]}'
        )


def test_parse_synthesis_response_rejects_empty_suggestions() -> None:
    with pytest.raises(SynthesisError):
        parse_synthesis_response('{"reflection": "x", "suggestions": []}')


def test_parse_synthesis_response_rejects_blank_suggestion_text() -> None:
    with pytest.raises(SynthesisError):
        parse_synthesis_response(
            '{"reflection": "x", "suggestions": [{"text": "", "principle_id": "p1", "explanation": "y"}]}'
        )
    with pytest.raises(SynthesisError):
        parse_synthesis_response(
            '{"reflection": "x", "suggestions": [{"text": "   ", "principle_id": "p1", "explanation": "y"}]}'
        )


def test_parse_synthesis_response_rejects_missing_suggestion_explanation() -> None:
    with pytest.raises(SynthesisError):
        parse_synthesis_response(
            '{"reflection": "x", "suggestions": [{"text": "do this", "principle_id": "p1"}]}'
        )


def test_parse_synthesis_response_rejects_blank_suggestion_explanation() -> None:
    with pytest.raises(SynthesisError):
        parse_synthesis_response(
            '{"reflection": "x", "suggestions": [{"text": "do this", "principle_id": "p1", "explanation": "  "}]}'
        )


def test_parse_synthesis_response_rejects_malformed_json() -> None:
    with pytest.raises(SynthesisError):
        parse_synthesis_response("not json at all")


def test_synthesize_analysis_calls_client_and_parses_result() -> None:
    client = FakeLLMClient(
        responses=[
            '{"reflection": "Stay aware of your patterns today.", '
            '"suggestions": [{"text": "Reflect tonight.", "principle_id": "living-consciously", "explanation": "Builds awareness."}]}'
        ]
    )
    result = synthesize_analysis(client, _book(), _principles(), [{"time": "09:00", "action": "worked", "category": "focus"}])
    assert result["reflection"] == "Stay aware of your patterns today."
    assert len(client.prompts_seen) == 1
    assert "Living Consciously" in client.prompts_seen[0]


def test_synthesize_analysis_retry_appends_reminder_with_specific_issues() -> None:
    client = FakeLLMClient(
        responses=['{"reflection": "a", "suggestions": [{"text": "b", "principle_id": "living-consciously", "explanation": "c"}]}']
    )
    synthesize_analysis(client, _book(), _principles(), [], retry_issues=["suggestion cites unknown principle_id 'x'"])
    assert "REMINDER" in client.prompts_seen[0]
    assert "suggestion cites unknown principle_id 'x'" in client.prompts_seen[0]


def test_fallback_analysis_uses_principle_text_directly_no_llm() -> None:
    result = fallback_analysis(_principles())
    assert result["reflection"] == (
        "Based on what you logged today, these ideas from the book connect "
        "to your situation: Living Consciously. See the suggestions below "
        "for what the book says about each."
    )
    assert result["suggestions"][0]["text"] == _principles()[0].summary
    assert result["suggestions"][0]["principle_id"] == "living-consciously"
    assert result["suggestions"][0]["explanation"]


def test_fallback_analysis_joins_multiple_principle_names_grammatically() -> None:
    result = fallback_analysis(_principles(3))
    assert "Living Consciously, Self-Acceptance, and Self-Responsibility" in result["reflection"]


def test_fallback_analysis_caps_suggestions_at_three() -> None:
    result = fallback_analysis(_principles(4))
    assert len(result["suggestions"]) == 3


def test_fallback_analysis_reflection_does_not_duplicate_suggestion_text() -> None:
    # The full principle summary (the actual grounded content) should only
    # appear once -- in the suggestion -- not also copy-pasted into the
    # reflection paragraph, or the two sections read as redundant
    # restatements of each other.
    result = fallback_analysis(_principles())
    summary = _principles()[0].summary
    assert summary not in result["reflection"]
    assert summary == result["suggestions"][0]["text"]
