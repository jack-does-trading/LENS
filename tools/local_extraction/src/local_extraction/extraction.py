from __future__ import annotations

import json
import logging

from local_extraction.chunking import Chunk
from local_extraction.json_utils import extract_json_object
from local_extraction.model_client import LocalModelClient, PayloadTooLargeError
from local_extraction.schema import CandidatePrinciple
from local_extraction.validation import validate_principle_summary

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 3

# Groq's raw request-body size cap (see PayloadTooLargeError) can reject a
# chunk's extraction request even at the standard --max-chunk-words size —
# observed in practice on a chunk built by the fixed-size fallback strategy.
# Below this many words, splitting further isn't worth it: let the error
# propagate rather than recursing toward single-word chunks.
_MIN_CHUNK_SPLIT_WORDS = 400

_RETRY_REMINDER = (
    "\n\nREMINDER: your previous response could not be parsed as JSON. "
    "Return ONLY a single valid JSON object matching the shape above. "
    "No prose before or after it. No markdown code fences."
)

_QUOTE_RULE_REMINDER = (
    "\n\nREMINDER: at least one candidate above violated a hard rule (a quote "
    "over 15 words, or more than one quote in a summary). Rewrite so every "
    "summary has at most one quote, and that quote is 15 words or fewer — "
    "paraphrase instead where a quote isn't necessary."
)

# Mirrors the constraints in the runtime synthesis prompt (architecture §3):
# paraphrase only, no verbatim reproduction, quotes <=15 words, <=1 quote per
# candidate principle. Stated in this tool's own prompt per §2 Path B.
CHUNK_PROMPT_TEMPLATE = """\
You are drafting candidate principles from ONE CHAPTER of a book, for a human \
editor to review later. You are extracting ideas from this chapter text only \
— you have not seen and must not reference any other part of the book.

RULES (must follow exactly):
- Paraphrase the author's ideas in your own words. Do not reproduce the \
author's sentences verbatim.
- You may include at most ONE short quotation per candidate principle, and \
that quotation must be 15 words or fewer. If a quote isn't necessary, \
paraphrase instead.
- Only draft principles that are actually present in the chapter text below. \
Do not invent ideas the text doesn't support.

BOOK: {book_title} by {book_author}
CHAPTER/SECTION LABEL: {source_chapter}

CHAPTER TEXT:
{chunk_text}

TASK:
Return valid JSON only, matching this shape, with 1-6 candidate principles \
for this chapter (fewer is fine if the chapter doesn't support that many):
{{
  "principles": [
    {{
      "name": "short principle name",
      "summary": "paraphrased summary, at most 200 words, at most one quote \
of at most 15 words",
      "applies_to_tags": ["tag-one", "tag-two"]
    }}
  ]
}}
No prose outside the JSON.
"""


def build_chunk_prompt(book_title: str, book_author: str, chunk: Chunk) -> str:
    return CHUNK_PROMPT_TEMPLATE.format(
        book_title=book_title,
        book_author=book_author,
        source_chapter=chunk.source_chapter or f"chunk {chunk.index}",
        chunk_text=chunk.text,
    )


class ChunkExtractionError(RuntimeError):
    """Raised when a chunk's model response isn't parseable JSON in the expected shape."""


def parse_chunk_response(raw_response: str, source_chapter: str | None) -> list[CandidatePrinciple]:
    try:
        payload = json.loads(extract_json_object(raw_response))
    except json.JSONDecodeError as exc:
        raise ChunkExtractionError(f"model response was not valid JSON: {exc}") from exc

    principles = payload.get("principles")
    if not isinstance(principles, list):
        raise ChunkExtractionError("model response JSON missing a 'principles' list")

    candidates: list[CandidatePrinciple] = []
    for entry in principles:
        if not isinstance(entry, dict) or "name" not in entry or "summary" not in entry:
            raise ChunkExtractionError(f"malformed candidate principle entry: {entry!r}")
        candidates.append(
            CandidatePrinciple(
                name=entry["name"],
                summary=entry["summary"],
                source_chapter=source_chapter,
                applies_to_tags=list(entry.get("applies_to_tags", [])),
            )
        )
    return candidates


def _split_chunk_in_half(chunk: Chunk) -> tuple[Chunk, Chunk]:
    """Split a chunk's text in half by word count for PayloadTooLargeError
    recovery. Both halves keep the original source_chapter and index —
    reviewers see chapter-level provenance, and the caller (pipeline.py)
    caches per the original chunk's index regardless of how many model
    calls it actually took internally. Unlike aggregation's brief-sampling
    413 recovery, this never drops content: every word of the chunk still
    gets sent, just across two smaller calls."""
    words = chunk.text.split()
    midpoint = len(words) // 2
    return (
        Chunk(text=" ".join(words[:midpoint]), source_chapter=chunk.source_chapter, index=chunk.index),
        Chunk(text=" ".join(words[midpoint:]), source_chapter=chunk.source_chapter, index=chunk.index),
    )


def extract_candidates_from_chunk(
    client: LocalModelClient,
    book_title: str,
    book_author: str,
    chunk: Chunk,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[CandidatePrinciple]:
    """Call the model for one chunk, parse its response, and check every
    candidate against the hard content rules (quote length/count, via
    validate_principle_summary) before accepting it.

    Why check here, not just at the end: this pipeline's final validate_draft
    check would eventually catch a rule violation too — but only *after* the
    (much more expensive) aggregation pass has already run on it. A real run
    burned ~100 minutes of aggregation before discovering two over-length
    quotes that were already present in the raw per-chunk extraction and
    could have been caught here in seconds.

    Small local models also routinely fail to produce parseable JSON on the
    first try (stray prose, a dropped comma). Both failure modes retry the
    same request, with an added reminder, up to `max_attempts` times. If
    retries are exhausted on a *content-rule* violation (not a parse
    failure), only the violating candidate(s) are dropped — their text is
    never rewritten or truncated — and the rest of the chunk's candidates are
    kept, with a clear warning naming what was dropped and why.
    """
    base_prompt = build_chunk_prompt(book_title, book_author, chunk)
    logger.info(
        "chunk %d (%s): requested candidate extraction", chunk.index, chunk.source_chapter or "unlabeled"
    )

    last_error: ChunkExtractionError | None = None
    last_candidates: list[CandidatePrinciple] | None = None
    last_issues_by_candidate: list[list] = []
    had_rule_violation = False

    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            prompt = base_prompt
        elif had_rule_violation:
            prompt = base_prompt + _QUOTE_RULE_REMINDER
        else:
            prompt = base_prompt + _RETRY_REMINDER
        try:
            raw_response = client.generate(prompt)
        except PayloadTooLargeError:
            if len(chunk.text.split()) <= _MIN_CHUNK_SPLIT_WORDS:
                raise
            first_half, second_half = _split_chunk_in_half(chunk)
            logger.warning(
                "chunk %d: rejected as too large by the backend (413) at %d words; "
                "splitting into two %d/%d-word halves and extracting each separately",
                chunk.index,
                len(chunk.text.split()),
                len(first_half.text.split()),
                len(second_half.text.split()),
            )
            return extract_candidates_from_chunk(
                client, book_title, book_author, first_half, max_attempts
            ) + extract_candidates_from_chunk(
                client, book_title, book_author, second_half, max_attempts
            )
        try:
            candidates = parse_chunk_response(raw_response, chunk.source_chapter)
        except ChunkExtractionError as exc:
            last_error = exc
            had_rule_violation = False
            logger.warning(
                "chunk %d attempt %d/%d failed to parse model response: %s",
                chunk.index,
                attempt,
                max_attempts,
                exc,
            )
            continue

        issues_by_candidate = [
            validate_principle_summary(c.name, c.summary) for c in candidates
        ]
        if not any(issues_by_candidate):
            return candidates

        had_rule_violation = True
        last_candidates = candidates
        last_issues_by_candidate = issues_by_candidate
        flat_issues = [i for issues in issues_by_candidate for i in issues]
        last_error = ChunkExtractionError(
            f"candidate(s) violated content rules: {'; '.join(str(i) for i in flat_issues)}"
        )
        logger.warning(
            "chunk %d attempt %d/%d: candidate(s) violated content rules: %s",
            chunk.index,
            attempt,
            max_attempts,
            "; ".join(str(i) for i in flat_issues),
        )

    if last_candidates is not None:
        survivors = [
            c for c, issues in zip(last_candidates, last_issues_by_candidate) if not issues
        ]
        for candidate, issues in zip(last_candidates, last_issues_by_candidate):
            if issues:
                logger.warning(
                    "chunk %d: dropping candidate %r after exhausting retries: %s",
                    chunk.index,
                    candidate.name,
                    "; ".join(str(i) for i in issues),
                )
        return survivors

    assert last_error is not None
    raise last_error
