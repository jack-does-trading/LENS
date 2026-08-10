from __future__ import annotations

import json

from local_extraction.json_utils import extract_json_object


def test_extract_json_object_passthrough_for_clean_json() -> None:
    raw = '{"a": 1, "b": [1, 2]}'
    assert extract_json_object(raw) == raw


def test_extract_json_object_strips_leading_prose() -> None:
    raw = 'Here is a valid JSON response with 2 items:\n\n{"a": 1}'
    assert json.loads(extract_json_object(raw)) == {"a": 1}


def test_extract_json_object_strips_trailing_prose() -> None:
    raw = '{"a": 1}\n\nI hope this helps!'
    assert json.loads(extract_json_object(raw)) == {"a": 1}


def test_extract_json_object_strips_markdown_fence_with_json_tag() -> None:
    raw = '```json\n{"a": 1}\n```'
    assert json.loads(extract_json_object(raw)) == {"a": 1}


def test_extract_json_object_strips_plain_markdown_fence() -> None:
    raw = '```\n{"a": 1}\n```'
    assert json.loads(extract_json_object(raw)) == {"a": 1}


def test_extract_json_object_handles_nested_braces() -> None:
    raw = 'preamble text {"a": {"nested": 1}, "b": 2} trailing text'
    assert json.loads(extract_json_object(raw)) == {"a": {"nested": 1}, "b": 2}


def test_extract_json_object_ignores_braces_inside_strings() -> None:
    raw = '{"a": "text with a } brace inside"}'
    assert json.loads(extract_json_object(raw)) == {"a": "text with a } brace inside"}


def test_extract_json_object_does_not_repair_malformed_json() -> None:
    # Missing comma between keys — a genuine model error, left for the caller
    # to detect via json.loads raising, not silently patched here.
    raw = '{"a": 1 "b": 2}'
    result = extract_json_object(raw)
    assert result == raw


def test_extract_json_object_no_braces_returns_original_text() -> None:
    assert extract_json_object("no json here at all") == "no json here at all"
