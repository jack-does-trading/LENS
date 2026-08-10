from app.llm import FakeLLMClient
from app.models import Principle
from app.verification import verify_analysis


def _principle(principle_id: str = "living-consciously") -> Principle:
    return Principle(
        principle_id=principle_id,
        book_id="six-pillars-of-self-esteem",
        name="Living Consciously",
        summary="Pay attention to facts and goals rather than operating on autopilot.",
    )


def _suggestion(text: str = "Notice your patterns tonight.", principle_id: str = "living-consciously", explanation: str = "It builds awareness.") -> dict:
    return {"text": text, "principle_id": principle_id, "explanation": explanation}


def test_verify_analysis_passes_clean_output() -> None:
    client = FakeLLMClient(responses=['{"verdict": "PASS"}'])
    result = verify_analysis(
        client,
        "Reflect on your day with awareness.",
        [_suggestion()],
        [_principle()],
    )
    assert result.passed
    assert result.issues == []


def test_verify_analysis_rejects_blank_suggestion_text() -> None:
    client = FakeLLMClient(responses=[])  # would raise if called
    result = verify_analysis(
        client, "Some reflection.", [_suggestion(text="  ")], [_principle()]
    )
    assert not result.passed
    assert "empty text" in result.issues[0]


def test_verify_analysis_rejects_blank_suggestion_explanation() -> None:
    client = FakeLLMClient(responses=[])  # would raise if called
    result = verify_analysis(
        client, "Some reflection.", [_suggestion(explanation="   ")], [_principle()]
    )
    assert not result.passed
    assert "empty explanation" in result.issues[0]


def test_verify_analysis_rejects_more_than_three_suggestions() -> None:
    client = FakeLLMClient(responses=[])  # would raise if called
    suggestions = [_suggestion(text=f"tip {i}") for i in range(4)]
    result = verify_analysis(client, "Some reflection.", suggestions, [_principle()])
    assert not result.passed
    assert any("top-3" in issue for issue in result.issues)


def test_verify_analysis_rejects_unknown_principle_id() -> None:
    client = FakeLLMClient(responses=['{"verdict": "PASS"}'])
    result = verify_analysis(
        client,
        "Some reflection.",
        [_suggestion(principle_id="nonexistent-principle")],
        [_principle()],
    )
    assert not result.passed
    assert "unknown principle_id" in result.issues[0]


def test_verify_analysis_rejects_overlength_quote() -> None:
    client = FakeLLMClient(responses=['{"verdict": "PASS"}'])
    long_quote = " ".join(["word"] * 16)
    result = verify_analysis(
        client,
        f'The author says "{long_quote}" about this.',
        [_suggestion()],
        [_principle()],
    )
    assert not result.passed
    assert any("quote" in issue for issue in result.issues)


def test_verify_analysis_passes_entailment_wrapped_in_markdown_fences() -> None:
    # Regression test: unlike Ollama (forced format="json"), Groq routinely
    # wraps its JSON answer in ```json fences or a sentence of prose. A bare
    # json.loads(raw) used to fail-closed on every single Groq response,
    # forcing every analysis to the non-LLM fallback regardless of whether
    # the model's verdict was actually PASS. See app/json_extraction.py.
    client = FakeLLMClient(responses=['Here is my verdict:\n```json\n{"verdict": "PASS"}\n```'])
    result = verify_analysis(
        client, "Some reflection.", [_suggestion()], [_principle()]
    )
    assert result.passed


def test_verify_analysis_fails_closed_on_unparseable_entailment_response() -> None:
    client = FakeLLMClient(responses=["I'm not sure, maybe?"])
    result = verify_analysis(
        client, "Some reflection.", [_suggestion()], [_principle()]
    )
    assert not result.passed


def test_verify_analysis_fails_closed_on_unexpected_verdict_value() -> None:
    client = FakeLLMClient(responses=['{"verdict": "MAYBE"}'])
    result = verify_analysis(
        client, "Some reflection.", [_suggestion()], [_principle()]
    )
    assert not result.passed


def test_verify_analysis_fails_when_entailment_says_fail() -> None:
    client = FakeLLMClient(responses=['{"verdict": "FAIL"}'])
    result = verify_analysis(
        client, "Some reflection.", [_suggestion()], [_principle()]
    )
    assert not result.passed
    assert "entailment" in result.issues[0]


def test_verify_analysis_rejects_first_person_reflection() -> None:
    client = FakeLLMClient(responses=[])  # would raise if called
    result = verify_analysis(
        client,
        "Today's mood of 3 suggests that I'm feeling somewhat uncertain about social interactions.",
        [_suggestion()],
        [_principle()],
    )
    assert not result.passed
    assert any("first person" in issue for issue in result.issues)


def test_verify_analysis_rejects_first_person_suggestion_text() -> None:
    client = FakeLLMClient(responses=[])  # would raise if called
    result = verify_analysis(
        client,
        "Some reflection.",
        [_suggestion(text="Tomorrow I will notice my patterns.")],
        [_principle()],
    )
    assert not result.passed
    assert any("first person" in issue for issue in result.issues)


def test_verify_analysis_allows_second_person_reflection() -> None:
    client = FakeLLMClient(responses=['{"verdict": "PASS"}'])
    result = verify_analysis(
        client,
        "Today's mood of 3 suggests you're feeling somewhat uncertain about social interactions.",
        [_suggestion()],
        [_principle()],
    )
    assert result.passed


def test_verify_analysis_skips_llm_call_when_rule_check_already_fails() -> None:
    client = FakeLLMClient(responses=[])  # would raise if called
    result = verify_analysis(
        client, "Some reflection.", [_suggestion(principle_id="unknown")], [_principle()]
    )
    assert not result.passed
    assert client.prompts_seen == []
