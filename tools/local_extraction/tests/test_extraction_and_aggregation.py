from __future__ import annotations

import json
import re

import pytest

from local_extraction.aggregation import (
    AGGREGATION_TIMEOUT_SECONDS,
    _MIN_THESIS_BRIEFS,
    AggregationError,
    aggregate_candidates,
    aggregate_candidates_hierarchical,
    build_thesis_only_prompt,
    generate_core_thesis_from_briefs,
    merge_batch,
    parse_aggregation_response,
    parse_batch_merge_response,
    parse_thesis_only_response,
    select_context_length,
)
from local_extraction.chunking import Chunk
from local_extraction.extraction import (
    ChunkExtractionError,
    extract_candidates_from_chunk,
    parse_chunk_response,
)
from local_extraction.model_client import FakeModelClient, PayloadTooLargeError
from local_extraction.schema import CandidatePrinciple


def test_parse_chunk_response_valid_json() -> None:
    raw = json.dumps(
        {
            "principles": [
                {
                    "name": "Self-acceptance",
                    "summary": "Accepting yourself is foundational.",
                    "applies_to_tags": ["self-esteem"],
                }
            ]
        }
    )

    candidates = parse_chunk_response(raw, source_chapter="Chapter One")

    assert len(candidates) == 1
    assert candidates[0].name == "Self-acceptance"
    assert candidates[0].source_chapter == "Chapter One"
    assert candidates[0].applies_to_tags == ["self-esteem"]


def test_parse_chunk_response_rejects_non_json() -> None:
    with pytest.raises(ChunkExtractionError):
        parse_chunk_response("not json at all", source_chapter=None)


def test_parse_chunk_response_rejects_missing_principles_key() -> None:
    with pytest.raises(ChunkExtractionError):
        parse_chunk_response(json.dumps({"nope": []}), source_chapter=None)


def test_parse_chunk_response_rejects_malformed_entry() -> None:
    with pytest.raises(ChunkExtractionError):
        parse_chunk_response(json.dumps({"principles": [{"summary": "missing name"}]}), source_chapter=None)


def test_extract_candidates_from_chunk_uses_client_and_parses() -> None:
    raw = json.dumps({"principles": [{"name": "X", "summary": "Y."}]})
    client = FakeModelClient(responses=[raw])
    chunk = Chunk(text="chapter text", source_chapter="Ch 1", index=0)

    candidates = extract_candidates_from_chunk(client, "Some Book", "Some Author", chunk)

    assert candidates[0].name == "X"
    assert len(client.prompts_seen) == 1
    assert "Some Book" in client.prompts_seen[0]
    assert "Some Author" in client.prompts_seen[0]


def test_extract_candidates_from_chunk_strips_prose_wrapper() -> None:
    raw = 'Here is the JSON:\n\n' + json.dumps({"principles": [{"name": "X", "summary": "Y."}]})
    client = FakeModelClient(responses=[raw])
    chunk = Chunk(text="chapter text", source_chapter="Ch 1", index=0)

    candidates = extract_candidates_from_chunk(client, "Book", "Author", chunk)

    assert candidates[0].name == "X"


def test_extract_candidates_from_chunk_retries_on_malformed_json_then_succeeds() -> None:
    malformed = '{"principles": [{"name": "X" "summary": "missing comma"}]}'
    valid = json.dumps({"principles": [{"name": "X", "summary": "Fixed on retry."}]})
    client = FakeModelClient(responses=[malformed, valid])
    chunk = Chunk(text="chapter text", source_chapter="Ch 1", index=0)

    candidates = extract_candidates_from_chunk(client, "Book", "Author", chunk, max_attempts=3)

    assert candidates[0].summary == "Fixed on retry."
    assert len(client.prompts_seen) == 2
    # Second attempt's prompt includes the retry reminder.
    assert "REMINDER" in client.prompts_seen[1]


def test_extract_candidates_from_chunk_raises_after_exhausting_retries() -> None:
    malformed = '{"principles": [{"name": "X" "summary": "still broken"}]}'
    client = FakeModelClient(responses=[malformed, malformed, malformed])
    chunk = Chunk(text="chapter text", source_chapter="Ch 1", index=0)

    with pytest.raises(ChunkExtractionError):
        extract_candidates_from_chunk(client, "Book", "Author", chunk, max_attempts=3)
    assert len(client.prompts_seen) == 3


def test_extract_candidates_from_chunk_splits_and_retries_on_413() -> None:
    # Real behavior observed on a fixed-size-chunked book: a single ~3000-word
    # chunk request was rejected outright with 413, well within the model's
    # context window. Recovery must never drop book content — split the
    # chunk and extract each half independently instead of sampling/dropping.
    calls = {"count": 0}
    valid = json.dumps({"principles": [{"name": "P", "summary": "Short summary."}]})

    def responder(prompt: str) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            raise PayloadTooLargeError("413 Payload Too Large")
        return valid

    client = FakeModelClient(responder=responder)
    chunk = Chunk(text=" ".join(f"word{i}" for i in range(1000)), source_chapter="Ch 1", index=0)

    candidates = extract_candidates_from_chunk(client, "Book", "Author", chunk)

    # First call 413s on the full chunk; split into two ~500-word halves,
    # each extracted independently — one principle per half, both halves'
    # words fully covered, nothing dropped.
    assert len(candidates) == 2
    assert calls["count"] == 3


def test_extract_candidates_from_chunk_raises_413_when_too_small_to_split_further() -> None:
    # Below _MIN_CHUNK_SPLIT_WORDS, splitting stops — the error propagates
    # instead of recursing toward degenerate single-word chunks.
    client = FakeModelClient(
        responder=lambda prompt: (_ for _ in ()).throw(PayloadTooLargeError("413 Payload Too Large"))
    )
    chunk = Chunk(text=" ".join(f"w{i}" for i in range(50)), source_chapter="Ch 1", index=0)

    with pytest.raises(PayloadTooLargeError):
        extract_candidates_from_chunk(client, "Book", "Author", chunk)


def _overlong_quote_response(name: str = "Bad") -> str:
    long_quote = " ".join(f"w{i}" for i in range(20))  # 20 words, over the 15-word limit
    return json.dumps({"principles": [{"name": name, "summary": f'Paraphrase. "{long_quote}" end.'}]})


def test_extract_candidates_from_chunk_retries_on_content_rule_violation_then_succeeds() -> None:
    valid = json.dumps({"principles": [{"name": "Good", "summary": "A clean, short summary."}]})
    client = FakeModelClient(responses=[_overlong_quote_response(), valid])
    chunk = Chunk(text="chapter text", source_chapter="Ch 1", index=0)

    candidates = extract_candidates_from_chunk(client, "Book", "Author", chunk, max_attempts=3)

    assert candidates[0].name == "Good"
    assert len(client.prompts_seen) == 2
    assert "15 words" in client.prompts_seen[1]  # rule-specific reminder, not the generic JSON one


def test_extract_candidates_from_chunk_drops_only_violating_candidate_after_retries_exhausted() -> None:
    # Two candidates in one response: one always violates the quote rule,
    # the other is always fine. After exhausting retries, only the violator
    # should be dropped — the good candidate must survive.
    long_quote = " ".join(f"w{i}" for i in range(20))
    mixed_response = json.dumps(
        {
            "principles": [
                {"name": "Bad", "summary": f'Paraphrase. "{long_quote}" end.'},
                {"name": "Good", "summary": "A clean, short summary."},
            ]
        }
    )
    client = FakeModelClient(responses=[mixed_response, mixed_response])
    chunk = Chunk(text="chapter text", source_chapter="Ch 1", index=0)

    candidates = extract_candidates_from_chunk(client, "Book", "Author", chunk, max_attempts=2)

    assert [c.name for c in candidates] == ["Good"]
    assert len(client.prompts_seen) == 2


def test_parse_aggregation_response_valid_json() -> None:
    raw = json.dumps(
        {
            "core_thesis": "The book argues X.",
            "principles": [
                {
                    "name": "P1",
                    "summary": "Summary one.",
                    "source_chapter": "ch1",
                    "applies_to_tags": ["tag"],
                }
            ],
        }
    )

    draft = parse_aggregation_response(raw)

    assert draft.core_thesis == "The book argues X."
    assert len(draft.principles) == 1
    assert draft.principles[0].name == "P1"


def test_parse_aggregation_response_rejects_missing_keys() -> None:
    with pytest.raises(AggregationError):
        parse_aggregation_response(json.dumps({"core_thesis": "only this"}))


def test_select_context_length_picks_smallest_sufficient_tier() -> None:
    assert select_context_length("short prompt") == 8192
    assert select_context_length(" ".join("w" for _ in range(6000))) == 16384
    assert select_context_length(" ".join("w" for _ in range(20000))) == 32768


def test_aggregate_candidates_uses_client_and_parses() -> None:
    raw = json.dumps({"core_thesis": "Thesis.", "principles": []})
    client = FakeModelClient(responses=[raw])
    candidates = [CandidatePrinciple(name="A", summary="B", source_chapter="ch1")]

    draft = aggregate_candidates(client, "Book", "Author", candidates)

    assert draft.core_thesis == "Thesis."
    assert len(client.prompts_seen) == 1
    assert client.context_lengths_seen == [8192]
    assert client.timeouts_seen == [AGGREGATION_TIMEOUT_SECONDS]


def test_aggregate_candidates_scales_context_length_with_candidate_volume() -> None:
    raw = json.dumps({"core_thesis": "Thesis.", "principles": []})
    client = FakeModelClient(responses=[raw])
    # Enough bulky candidates to push the serialized prompt past the 8192 tier.
    candidates = [
        CandidatePrinciple(name=f"P{i}", summary=" ".join(f"w{j}" for j in range(150)), source_chapter="ch1")
        for i in range(60)
    ]

    aggregate_candidates(client, "Book", "Author", candidates)

    assert client.context_lengths_seen[0] > 8192


def test_aggregate_candidates_retries_on_malformed_json_then_succeeds() -> None:
    malformed = '{"core_thesis": "T" "principles": []}'
    valid = json.dumps({"core_thesis": "Fixed.", "principles": []})
    client = FakeModelClient(responses=[malformed, valid])
    candidates = [CandidatePrinciple(name="A", summary="B", source_chapter="ch1")]

    draft = aggregate_candidates(client, "Book", "Author", candidates, max_attempts=3)

    assert draft.core_thesis == "Fixed."
    assert len(client.prompts_seen) == 2


def test_aggregate_candidates_raises_after_exhausting_retries() -> None:
    malformed = '{"core_thesis": "T" "principles": []}'
    client = FakeModelClient(responses=[malformed, malformed])
    candidates = [CandidatePrinciple(name="A", summary="B", source_chapter="ch1")]

    with pytest.raises(AggregationError):
        aggregate_candidates(client, "Book", "Author", candidates, max_attempts=2)


def _batch_merge_response(count: int, prefix: str = "P") -> str:
    return json.dumps(
        {
            "principles": [
                {"name": f"{prefix}{i}", "summary": f"Summary {prefix}{i}."}
                for i in range(count)
            ]
        }
    )


def _final_aggregation_response(thesis: str = "Final thesis.") -> str:
    return json.dumps({"core_thesis": thesis, "principles": []})


def _make_candidates(n: int) -> list[CandidatePrinciple]:
    return [CandidatePrinciple(name=f"C{i}", summary=f"Summary {i}.", source_chapter="ch1") for i in range(n)]


def test_parse_batch_merge_response_valid_json() -> None:
    principles = parse_batch_merge_response(_batch_merge_response(3))
    assert len(principles) == 3
    assert principles[0].name == "P0"


def test_parse_batch_merge_response_rejects_missing_principles_key() -> None:
    with pytest.raises(AggregationError):
        parse_batch_merge_response(json.dumps({"nope": []}))


def test_merge_batch_uses_client_and_parses() -> None:
    client = FakeModelClient(responses=[_batch_merge_response(2)])
    batch = _make_candidates(5)

    result = merge_batch(client, "Book", "Author", batch)

    assert len(result) == 2
    assert "ONE BATCH" in client.prompts_seen[0]


def test_aggregate_candidates_hierarchical_skips_batching_when_already_small() -> None:
    client = FakeModelClient(responses=[_final_aggregation_response("Small set thesis.")])
    candidates = _make_candidates(10)  # under the default threshold of 30

    draft = aggregate_candidates_hierarchical(client, "Book", "Author", candidates)

    assert draft.core_thesis == "Small set thesis."
    assert len(client.prompts_seen) == 1  # straight to the final pass, no batching


def test_aggregate_candidates_hierarchical_batches_then_runs_final_pass() -> None:
    # 50 candidates, batch_size=25 -> 2 batches; each reduces to 15 -> 30 total,
    # which is not > the threshold of 30, so one round of batching then the final pass.
    client = FakeModelClient(
        responses=[
            _batch_merge_response(15, prefix="A"),
            _batch_merge_response(15, prefix="B"),
            _final_aggregation_response("Merged thesis."),
        ]
    )
    candidates = _make_candidates(50)

    draft = aggregate_candidates_hierarchical(
        client, "Book", "Author", candidates, batch_size=25, single_pass_threshold=30
    )

    assert draft.core_thesis == "Merged thesis."
    assert len(client.prompts_seen) == 3
    assert "ONE BATCH" in client.prompts_seen[0]
    assert "ONE BATCH" in client.prompts_seen[1]
    assert "ONE BATCH" not in client.prompts_seen[2]  # final pass uses the other prompt


def test_aggregate_candidates_hierarchical_passes_through_batch_that_fails_to_merge() -> None:
    # 50 candidates, batch_size=25 -> 2 batches. Batch 1 fails all 2 attempts
    # (malformed JSON both times) and should be passed through unmerged
    # (25 candidates unchanged) rather than aborting the whole run. Batch 2
    # succeeds normally (15 candidates). Total after round 1: 25 + 15 = 40,
    # within the default safe_final_limit (60) and max_rounds defaults to 1,
    # so a single final pass runs directly on those 40.
    malformed = '{"principles": [{"name": "X" "broken": true}]}'
    client = FakeModelClient(
        responses=[
            malformed,
            malformed,  # batch 1: exhausts 2 attempts, passed through unmerged (25)
            _batch_merge_response(15, prefix="B"),  # batch 2: merges to 15
            _final_aggregation_response("Resilient thesis."),
        ]
    )
    candidates = _make_candidates(50)

    draft = aggregate_candidates_hierarchical(
        client, "Book", "Author", candidates, batch_size=25, single_pass_threshold=30, max_attempts=2
    )

    assert draft.core_thesis == "Resilient thesis."
    assert len(client.prompts_seen) == 4  # did not abort after the first batch's failure


def test_aggregate_candidates_hierarchical_stops_looping_when_no_progress() -> None:
    # 40 candidates, batch_size=25 -> batches of 25 and 15; both batches come
    # back unreduced (40 total, no shrinkage) -> must not loop forever, should
    # proceed straight to the final pass afterward instead.
    client = FakeModelClient(
        responses=[
            _batch_merge_response(25, prefix="A"),
            _batch_merge_response(15, prefix="B"),
            _final_aggregation_response("Fallback thesis."),
        ]
    )
    candidates = _make_candidates(40)

    draft = aggregate_candidates_hierarchical(
        client, "Book", "Author", candidates, batch_size=25, single_pass_threshold=30
    )

    assert draft.core_thesis == "Fallback thesis."
    assert len(client.prompts_seen) == 3  # exactly one round, not an infinite loop


def test_aggregate_candidates_hierarchical_stops_after_max_rounds_even_if_still_above_threshold() -> None:
    # 100 candidates, batch_size=50 -> 2 batches/round, each round reducing by
    # 20%. With single_pass_threshold=10 alone this would keep batching for
    # many rounds; max_rounds=2 must cut it off regardless.
    client = FakeModelClient(
        responses=[
            _batch_merge_response(40),  # round 1 batch 1: 50 -> 40
            _batch_merge_response(40),  # round 1 batch 2: 50 -> 40 (current: 80)
            _batch_merge_response(40),  # round 2 batch 1: 50 -> 40
            _batch_merge_response(24),  # round 2 batch 2: 30 -> 24 (current: 64)
            _final_aggregation_response("Bounded thesis."),
        ]
    )
    candidates = _make_candidates(100)

    draft = aggregate_candidates_hierarchical(
        client,
        "Book",
        "Author",
        candidates,
        batch_size=50,
        single_pass_threshold=10,
        max_rounds=2,
        min_reduction_ratio=0.0,
        safe_final_limit=100,  # large enough that 64 still gets the full final pass
    )

    assert draft.core_thesis == "Bounded thesis."
    assert len(client.prompts_seen) == 5  # 2 rounds x 2 batches + 1 final pass, not more


def test_aggregate_candidates_hierarchical_stops_early_on_low_reduction_ratio() -> None:
    # 40 candidates, batch_size=20 -> 2 batches, each barely reducing (10%),
    # well below a 50% minimum reduction ratio -> must stop after round 1
    # even though max_rounds=3 would otherwise allow more rounds.
    client = FakeModelClient(
        responses=[
            _batch_merge_response(18),
            _batch_merge_response(18),
            _final_aggregation_response("Early stop thesis."),
        ]
    )
    candidates = _make_candidates(40)

    draft = aggregate_candidates_hierarchical(
        client,
        "Book",
        "Author",
        candidates,
        batch_size=20,
        single_pass_threshold=5,
        max_rounds=3,
        min_reduction_ratio=0.5,
    )

    assert draft.core_thesis == "Early stop thesis."
    assert len(client.prompts_seen) == 3  # one round, not the three max_rounds would allow


def test_aggregate_candidates_hierarchical_uses_thesis_only_path_above_safe_final_limit() -> None:
    # No batching at all (max_rounds=0); the candidate count alone (80)
    # exceeds safe_final_limit (60), so the lightweight brief-based thesis
    # path must be used instead of a full-detail final merge, and the
    # candidates must be returned as the final principles list unchanged.
    client = FakeModelClient(responses=[json.dumps({"core_thesis": "Brief-based thesis."})])
    candidates = _make_candidates(80)

    draft = aggregate_candidates_hierarchical(
        client, "Book", "Author", candidates, max_rounds=0, safe_final_limit=60
    )

    assert draft.core_thesis == "Brief-based thesis."
    assert draft.principles == candidates  # unchanged, no further merge attempted
    assert len(client.prompts_seen) == 1
    assert "PRINCIPLE BRIEFS" in client.prompts_seen[0]


def test_build_thesis_only_prompt_uses_short_briefs_not_full_summaries() -> None:
    long_summary = " ".join(f"word{i}" for i in range(100))
    principles = [CandidatePrinciple(name="Idea", summary=long_summary, source_chapter="ch1")]

    prompt = build_thesis_only_prompt("Book", "Author", principles)

    assert "word99" not in prompt  # truncated to a short brief, not the full 100-word summary
    assert "Idea:" in prompt


def test_parse_thesis_only_response_valid() -> None:
    assert parse_thesis_only_response(json.dumps({"core_thesis": "X."})) == "X."


def test_parse_thesis_only_response_rejects_missing_key() -> None:
    with pytest.raises(AggregationError):
        parse_thesis_only_response(json.dumps({"nope": "X"}))


def test_aggregate_candidates_hierarchical_show_progress_prints_batch_progress(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeModelClient(
        responses=[
            _batch_merge_response(15, prefix="A"),
            _batch_merge_response(15, prefix="B"),
            _final_aggregation_response("Progress thesis."),
        ]
    )
    candidates = _make_candidates(50)

    aggregate_candidates_hierarchical(
        client, "Book", "Author", candidates, batch_size=25, single_pass_threshold=30, show_progress=True
    )

    captured = capsys.readouterr()
    assert "Aggregation round 1" in captured.err
    assert "Final pass" in captured.out


def test_generate_core_thesis_from_briefs_retries_on_malformed_json() -> None:
    malformed = "not json"
    valid = json.dumps({"core_thesis": "Recovered thesis."})
    client = FakeModelClient(responses=[malformed, valid])
    candidates = _make_candidates(5)

    thesis = generate_core_thesis_from_briefs(client, "Book", "Author", candidates, max_attempts=2)

    assert thesis == "Recovered thesis."
    assert len(client.prompts_seen) == 2


def test_generate_core_thesis_from_briefs_shrinks_and_retries_on_413() -> None:
    # Real Groq behavior: a request can be rejected outright with 413 even
    # though it fits the chosen context-length tier — a raw HTTP body-size
    # cap, unrelated to token/context limits. 50 briefs 413s, halving
    # (50 -> 25 -> 20) until it lands at the _MIN_THESIS_BRIEFS floor, which
    # succeeds.
    candidates = _make_candidates(50)

    def responder(prompt: str) -> str:
        # The prompt embeds its own brief count ("PRINCIPLE BRIEFS (N
        # total):") — read that back rather than counting "- " lines, which
        # would also match the template's own unrelated bullet points.
        brief_count = int(re.search(r"PRINCIPLE BRIEFS \((\d+) total\)", prompt).group(1))
        if brief_count > _MIN_THESIS_BRIEFS:
            raise PayloadTooLargeError("413 Payload Too Large")
        return json.dumps({"core_thesis": f"Thesis from {brief_count} briefs."})

    client = FakeModelClient(responder=responder)

    thesis = generate_core_thesis_from_briefs(client, "Book", "Author", candidates)

    assert thesis == f"Thesis from {_MIN_THESIS_BRIEFS} briefs."


def test_generate_core_thesis_from_briefs_raises_413_once_at_min_briefs() -> None:
    # Below _MIN_THESIS_BRIEFS, shrinking stops — PayloadTooLargeError
    # propagates instead of shrinking forever or silently giving up.
    candidates = _make_candidates(_MIN_THESIS_BRIEFS + 5)
    client = FakeModelClient(
        responder=lambda prompt: (_ for _ in ()).throw(PayloadTooLargeError("413 Payload Too Large"))
    )

    with pytest.raises(PayloadTooLargeError):
        generate_core_thesis_from_briefs(client, "Book", "Author", candidates)
