from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_extraction.chunking import Chunk
from local_extraction.model_client import FakeModelClient
from local_extraction.pipeline import (
    OutputExistsError,
    PipelineConfig,
    ValidationFailed,
    _chunk_content_hash,
    _load_cached_candidates,
    _write_chunk_cache,
    run_pipeline,
)
from local_extraction.schema import CandidatePrinciple


def _chunk_response(name: str, summary: str) -> str:
    return json.dumps({"principles": [{"name": name, "summary": summary, "applies_to_tags": ["tag-a"]}]})


def _make_config(tmp_path: Path, pdf_path: Path) -> PipelineConfig:
    return PipelineConfig(
        pdf_path=pdf_path,
        model="fake-model",
        book_id="six-pillars-of-self-esteem",
        title="The Six Pillars of Self-Esteem",
        author="Nathaniel Branden",
        tone="scientific",
        output_path=tmp_path / "output" / "six-pillars-of-self-esteem.json",
        scratch_dir=tmp_path / "scratch",
    )


def test_pipeline_happy_path_writes_validated_output(tmp_path: Path, pdf_with_toc: Path) -> None:
    config = _make_config(tmp_path, pdf_with_toc)
    client = FakeModelClient(
        responses=[
            _chunk_response("Self-Acceptance", "Accept yourself as you are, without conditions."),
            _chunk_response("Living Consciously", "Pay attention to reality as it actually is."),
            json.dumps(
                {
                    "core_thesis": "Self-esteem rests on internal practices, not external validation.",
                    "principles": [
                        {
                            "name": "Self-Acceptance",
                            "summary": "Accept yourself as you are, without conditions.",
                            "source_chapter": "Chapter One",
                            "applies_to_tags": ["self-esteem"],
                        },
                        {
                            "name": "Living Consciously",
                            "summary": "Pay attention to reality as it actually is.",
                            "source_chapter": "Chapter Two",
                            "applies_to_tags": ["awareness"],
                        },
                    ],
                }
            ),
        ]
    )

    result = run_pipeline(config, client)

    assert config.output_path.exists()
    on_disk = json.loads(config.output_path.read_text())
    assert on_disk == result

    assert result["book"]["book_id"] == "six-pillars-of-self-esteem"
    assert result["book"]["extraction_method"] == "local_model"
    assert result["book"]["review_status"] == "pending_review"
    assert result["book"]["tone"] == "scientific"
    assert len(result["principles"]) == 2
    assert {p["principle_id"] for p in result["principles"]} == {
        "self-acceptance",
        "living-consciously",
    }
    assert result["extraction_metadata"]["chunking_strategy"] == "toc"
    assert result["extraction_metadata"]["model"] == "fake-model"

    # Scratch artifacts exist for local debugging (raw text + raw per-chunk output).
    assert (config.scratch_dir / "raw_pdf_text.txt").exists()
    assert (config.scratch_dir / "chunk_000_candidates.json").exists()
    assert (config.scratch_dir / "chunk_001_candidates.json").exists()
    assert (config.scratch_dir / "aggregated_draft_raw.json").exists()


def test_pipeline_dedupes_principle_ids_on_name_collision(tmp_path: Path, pdf_with_toc: Path) -> None:
    config = _make_config(tmp_path, pdf_with_toc)
    same_name_principles = json.dumps(
        {
            "core_thesis": "Thesis text.",
            "principles": [
                {"name": "Self-Respect", "summary": "First take on the idea.", "source_chapter": "ch1"},
                {"name": "Self-Respect", "summary": "Second, distinct take on the idea.", "source_chapter": "ch2"},
            ],
        }
    )
    client = FakeModelClient(
        responses=[
            _chunk_response("A", "Summary A."),
            _chunk_response("B", "Summary B."),
            same_name_principles,
        ]
    )

    result = run_pipeline(config, client)

    ids = [p["principle_id"] for p in result["principles"]]
    assert ids == ["self-respect", "self-respect-2"]


def test_pipeline_rejects_and_writes_nothing_on_overlong_quote(tmp_path: Path, pdf_with_toc: Path) -> None:
    config = _make_config(tmp_path, pdf_with_toc)
    long_quote = " ".join(f"w{i}" for i in range(40))  # deliberately 40-word quote
    bad_aggregation = json.dumps(
        {
            "core_thesis": "Thesis text.",
            "principles": [
                {
                    "name": "Bad Principle",
                    "summary": f'A paraphrase with a bad quote: "{long_quote}" attributed to the author.',
                    "source_chapter": "ch1",
                }
            ],
        }
    )
    client = FakeModelClient(
        responses=[
            _chunk_response("A", "Summary A."),
            _chunk_response("B", "Summary B."),
            bad_aggregation,
        ]
    )

    with pytest.raises(ValidationFailed) as exc_info:
        run_pipeline(config, client)

    assert any("quote" in str(issue).lower() for issue in exc_info.value.issues)
    assert not config.output_path.exists()

    # Raw (invalid) model output still lands in scratch for debugging, per design —
    # only the *validated* file is withheld.
    assert (config.scratch_dir / "aggregated_draft_raw.json").exists()


def test_pipeline_rejects_overlong_core_thesis(tmp_path: Path, pdf_with_toc: Path) -> None:
    config = _make_config(tmp_path, pdf_with_toc)
    overlong_thesis = " ".join(f"word{i}" for i in range(200))  # > 150 words
    bad_aggregation = json.dumps({"core_thesis": overlong_thesis, "principles": []})
    client = FakeModelClient(
        responses=[
            _chunk_response("A", "Summary A."),
            _chunk_response("B", "Summary B."),
            bad_aggregation,
        ]
    )

    with pytest.raises(ValidationFailed) as exc_info:
        run_pipeline(config, client)

    assert any("core_thesis" in issue.field for issue in exc_info.value.issues)
    assert not config.output_path.exists()


def test_pipeline_skips_chunk_that_exhausts_extraction_retries_and_continues(
    tmp_path: Path, pdf_with_toc: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Same principle as a batch-merge failure during aggregation: one chunk's
    # model output being unrecoverably malformed shouldn't abort the whole
    # book. Chunk 0 exhausts all 3 parse attempts; chunk 1 and aggregation
    # still succeed, and the book still gets a validated draft.
    config = _make_config(tmp_path, pdf_with_toc)
    malformed = "not json at all"
    client = FakeModelClient(
        responses=[
            malformed,
            malformed,
            malformed,  # chunk 0: exhausts all 3 attempts, gets skipped
            _chunk_response("B", "Summary B."),  # chunk 1: succeeds normally
            _valid_aggregation_response(),
        ]
    )

    with caplog.at_level("WARNING"):
        result = run_pipeline(config, client)

    assert config.output_path.exists()
    assert result["principles"]
    assert any("chunk 0" in message and "skipping" in message for message in caplog.messages)
    # Failed chunk gets no cache entry (so a re-run retries it fresh); the
    # chunk that succeeded does.
    assert not (config.scratch_dir / "chunk_000_candidates.json").exists()
    assert (config.scratch_dir / "chunk_001_candidates.json").exists()


def test_pipeline_show_progress_prints_skipped_chunk_summary(
    tmp_path: Path, pdf_with_toc: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _make_config(tmp_path, pdf_with_toc)
    malformed = "not json at all"
    client = FakeModelClient(
        responses=[
            malformed,
            malformed,
            malformed,
            _chunk_response("B", "Summary B."),
            _valid_aggregation_response(),
        ]
    )

    run_pipeline(config, client, show_progress=True)

    captured = capsys.readouterr()
    assert "1/2 chunks skipped" in captured.err or "1/2 chunks skipped" in captured.out


def _valid_aggregation_response() -> str:
    return json.dumps(
        {
            "core_thesis": "Self-esteem rests on internal practices, not external validation.",
            "principles": [
                {
                    "name": "Self-Acceptance",
                    "summary": "Accept yourself as you are, without conditions.",
                    "source_chapter": "Chapter One",
                    "applies_to_tags": ["self-esteem"],
                },
            ],
        }
    )


def test_pipeline_show_progress_false_by_default_prints_nothing(
    tmp_path: Path, pdf_with_toc: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _make_config(tmp_path, pdf_with_toc)
    client = FakeModelClient(
        responses=[
            _chunk_response("A", "Summary A."),
            _chunk_response("B", "Summary B."),
            _valid_aggregation_response(),
        ]
    )

    run_pipeline(config, client)  # show_progress defaults to False

    captured = capsys.readouterr()
    assert "Extracting principles" not in captured.err
    assert "Aggregating" not in captured.out


def test_pipeline_show_progress_true_prints_progress(
    tmp_path: Path, pdf_with_toc: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _make_config(tmp_path, pdf_with_toc)
    client = FakeModelClient(
        responses=[
            _chunk_response("A", "Summary A."),
            _chunk_response("B", "Summary B."),
            _valid_aggregation_response(),
        ]
    )

    run_pipeline(config, client, show_progress=True)

    captured = capsys.readouterr()
    assert "Extracting principles" in captured.err
    assert "Aggregating" in captured.out


def test_pipeline_config_rejects_invalid_tone(tmp_path: Path, pdf_with_toc: Path) -> None:
    with pytest.raises(ValueError, match="tone must be one of"):
        PipelineConfig(
            pdf_path=pdf_with_toc,
            model="fake-model",
            book_id="x",
            title="X",
            author="Y",
            tone="not-a-real-tone",
            output_path=tmp_path / "out.json",
            scratch_dir=tmp_path / "scratch",
        )


# --- chunk-cache helpers -----------------------------------------------------


def test_chunk_content_hash_stable_for_identical_content() -> None:
    a = Chunk(text="some chapter text", source_chapter="Ch 1", index=0)
    b = Chunk(text="some chapter text", source_chapter="Ch 1", index=5)  # index shouldn't matter
    assert _chunk_content_hash(a) == _chunk_content_hash(b)


def test_chunk_content_hash_differs_for_different_text() -> None:
    a = Chunk(text="some chapter text", source_chapter="Ch 1", index=0)
    b = Chunk(text="different chapter text", source_chapter="Ch 1", index=0)
    assert _chunk_content_hash(a) != _chunk_content_hash(b)


def test_load_cached_candidates_missing_file_is_a_miss(tmp_path: Path) -> None:
    assert _load_cached_candidates(tmp_path / "nope.json", "somehash") is None


def test_write_then_load_cached_candidates_round_trips(tmp_path: Path) -> None:
    cache_path = tmp_path / "chunk_000_candidates.json"
    candidates = [CandidatePrinciple(name="X", summary="Y.", source_chapter="ch1", applies_to_tags=["t"])]

    _write_chunk_cache(cache_path, "hash-abc", candidates)
    loaded = _load_cached_candidates(cache_path, "hash-abc")

    assert loaded is not None
    assert loaded[0].name == "X"
    assert loaded[0].applies_to_tags == ["t"]


def test_load_cached_candidates_hash_mismatch_is_a_miss(tmp_path: Path) -> None:
    cache_path = tmp_path / "chunk_000_candidates.json"
    _write_chunk_cache(cache_path, "hash-abc", [CandidatePrinciple(name="X", summary="Y.", source_chapter=None)])

    assert _load_cached_candidates(cache_path, "different-hash") is None


def test_load_cached_candidates_old_bare_list_format_is_a_miss(tmp_path: Path) -> None:
    # Format before hash-based caching existed — must be treated as a miss,
    # not crash.
    cache_path = tmp_path / "chunk_000_candidates.json"
    cache_path.write_text(json.dumps([{"name": "X", "summary": "Y."}]), encoding="utf-8")

    assert _load_cached_candidates(cache_path, "any-hash") is None


def test_load_cached_candidates_corrupt_json_is_a_miss(tmp_path: Path) -> None:
    cache_path = tmp_path / "chunk_000_candidates.json"
    cache_path.write_text("not json at all", encoding="utf-8")

    assert _load_cached_candidates(cache_path, "any-hash") is None


# --- run_pipeline caching + overwrite behavior -------------------------------


def test_pipeline_reuses_cached_chunk_extraction_on_rerun(tmp_path: Path, pdf_with_toc: Path) -> None:
    config = _make_config(tmp_path, pdf_with_toc)
    client1 = FakeModelClient(
        responses=[
            _chunk_response("A", "Summary A."),
            _chunk_response("B", "Summary B."),
            _valid_aggregation_response(),
        ]
    )
    run_pipeline(config, client1)

    # Same PDF, same scratch dir -> both chunks' content is unchanged, so a
    # rerun should skip re-extracting them entirely. Only give the client one
    # response (for aggregation) — if a chunk were re-extracted, this queue
    # would be exhausted and raise.
    rerun_config = PipelineConfig(
        pdf_path=config.pdf_path,
        model=config.model,
        book_id=config.book_id,
        title=config.title,
        author=config.author,
        tone=config.tone,
        output_path=config.output_path,
        scratch_dir=config.scratch_dir,
        overwrite_output=True,
    )
    client2 = FakeModelClient(responses=[_valid_aggregation_response()])

    result = run_pipeline(rerun_config, client2)

    assert len(client2.prompts_seen) == 1  # aggregation only, both chunks reused from cache
    assert result["principles"]


def test_pipeline_recomputes_when_chunk_content_changes(
    tmp_path: Path, pdf_with_toc: Path, pdf_with_headings_no_toc: Path
) -> None:
    shared_scratch = tmp_path / "scratch"
    config1 = PipelineConfig(
        pdf_path=pdf_with_toc,
        model="fake-model",
        book_id="b",
        title="T",
        author="A",
        tone="scientific",
        output_path=tmp_path / "out1.json",
        scratch_dir=shared_scratch,
    )
    client1 = FakeModelClient(
        responses=[
            _chunk_response("A", "Summary A."),
            _chunk_response("B", "Summary B."),
            _valid_aggregation_response(),
        ]
    )
    run_pipeline(config1, client1)

    # Different PDF sharing the same scratch dir -> the chunk text at indices
    # 0/1 has changed, so the cache must be treated as stale and recomputed,
    # not silently reused.
    config2 = PipelineConfig(
        pdf_path=pdf_with_headings_no_toc,
        model="fake-model",
        book_id="b",
        title="T",
        author="A",
        tone="scientific",
        output_path=tmp_path / "out2.json",
        scratch_dir=shared_scratch,
    )
    client2 = FakeModelClient(
        responses=[
            _chunk_response("C", "Summary C."),
            _chunk_response("D", "Summary D."),
            _valid_aggregation_response(),
        ]
    )

    run_pipeline(config2, client2)

    assert len(client2.prompts_seen) == 3  # both chunks recomputed + aggregation, nothing reused


def test_pipeline_refuses_to_overwrite_existing_output_by_default(
    tmp_path: Path, pdf_with_toc: Path
) -> None:
    config = _make_config(tmp_path, pdf_with_toc)
    client1 = FakeModelClient(
        responses=[
            _chunk_response("A", "Summary A."),
            _chunk_response("B", "Summary B."),
            _valid_aggregation_response(),
        ]
    )
    run_pipeline(config, client1)

    client2 = FakeModelClient(responses=["should never be used"])
    with pytest.raises(OutputExistsError):
        run_pipeline(config, client2)

    assert client2.prompts_seen == []  # bails out before any chunking/extraction/model work


def test_pipeline_overwrite_flag_allows_replacing_existing_output(
    tmp_path: Path, pdf_with_toc: Path
) -> None:
    config = _make_config(tmp_path, pdf_with_toc)
    client1 = FakeModelClient(
        responses=[
            _chunk_response("A", "Summary A."),
            _chunk_response("B", "Summary B."),
            _valid_aggregation_response(),
        ]
    )
    run_pipeline(config, client1)

    overwrite_config = PipelineConfig(
        pdf_path=config.pdf_path,
        model=config.model,
        book_id=config.book_id,
        title=config.title,
        author=config.author,
        tone=config.tone,
        output_path=config.output_path,
        scratch_dir=config.scratch_dir,
        overwrite_output=True,
    )
    client2 = FakeModelClient(responses=[_valid_aggregation_response()])

    result = run_pipeline(overwrite_config, client2)

    assert result["principles"]
