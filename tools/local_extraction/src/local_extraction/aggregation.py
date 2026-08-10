from __future__ import annotations

import json
import logging

from tqdm import tqdm

from local_extraction.json_utils import extract_json_object
from local_extraction.model_client import LocalModelClient, PayloadTooLargeError
from local_extraction.schema import AggregatedDraft, CandidatePrinciple

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 3

# The aggregation prompt concatenates candidates from every chunk, so its size
# scales with how many chunks the book was split into — unlike per-chunk
# extraction calls, it can't be sized correctly at client-construction time.
# Pick the smallest context tier that comfortably fits the actual prompt
# rather than guessing one fixed number, so a small book doesn't pay for RAM
# it doesn't need and a large one doesn't silently truncate.
_CONTEXT_LENGTH_TIERS = (8192, 16384, 32768, 65536, 131072)
_OUTPUT_MARGIN_TOKENS = 1500
_WORDS_TO_TOKENS_FACTOR = 1.35

# One real run (211 candidates, context_length=16384) hit the per-chunk
# default 300s timeout on this pass specifically — merging/deduping a large
# candidate set is a single big-context call with a lot to read and write,
# unlike the many small, fast per-chunk calls. Give this one call a much more
# generous, fixed timeout rather than guessing a tier-scaled number from one
# data point.
AGGREGATION_TIMEOUT_SECONDS = 1800.0


def select_context_length(prompt: str) -> int:
    estimated_tokens = int(len(prompt.split()) * _WORDS_TO_TOKENS_FACTOR) + _OUTPUT_MARGIN_TOKENS
    for tier in _CONTEXT_LENGTH_TIERS:
        if tier >= estimated_tokens:
            return tier
    return _CONTEXT_LENGTH_TIERS[-1]

_RETRY_REMINDER = (
    "\n\nREMINDER: your previous response could not be parsed as JSON. "
    "Return ONLY a single valid JSON object matching the shape above. "
    "No prose before or after it. No markdown code fences."
)

# Batching thresholds for hierarchical aggregation (see
# aggregate_candidates_hierarchical). Chosen from one real observation: a
# single aggregation call over 211 candidates degraded badly — the model
# echoed the prompt's own placeholder text ("at most 150 words") as the
# core_thesis and silently dropped candidates from 16 of 21 chapters, even
# though the estimated token count fit the chosen context window. A batch of
# 25 candidates is roughly the same order of magnitude as a single per-chunk
# extraction call's input (which was reliable across all 48 real chunks), so
# it's a reasonable size for a small local model to merge coherently in one shot.
DEFAULT_BATCH_SIZE = 25
DEFAULT_SINGLE_PASS_THRESHOLD = 30

# A real run on a 348-page, 21-chapter book showed each round's reduction
# shrinking fast: 211->171 (-19%), 171->146 (-15%), 146->136 (-7%) candidates,
# at ~25-30 minutes per round. That's not stalled merging — it's most of a
# full book's candidates genuinely being distinct ideas, not duplicates.
# Chasing single_pass_threshold=30 down from there could run for hours
# without ever getting there. These bounds stop batching once it stops paying
# off, rather than forcing convergence to an arbitrary small number.
DEFAULT_MAX_ROUNDS = 1
DEFAULT_MIN_REDUCTION_RATIO = 0.10
# If batching stops with more candidates than this, a single full-detail final
# call (every candidate's full name+summary+tags) risks the same overwhelm
# that produced the placeholder-thesis failure. Past this size, core_thesis is
# generated from short one-line briefs instead (bounded prompt size regardless
# of how many principles remain) and the merged list is used as the final
# principles as-is — see generate_core_thesis_from_briefs.
DEFAULT_SAFE_FINAL_LIMIT = 60

# Groq's API gateway enforces a raw HTTP request-body size cap that's
# independent of the model's token/context-window limits — a prompt sized
# correctly for the chosen context_length tier (see select_context_length)
# can still be rejected outright with 413 Payload Too Large. Observed on a
# real run: 409 principle briefs (short one-liners, well inside the 16384
# context tier) still 413'd. generate_core_thesis_from_briefs below shrinks
# and retries when this happens rather than crashing the whole aggregation
# pass right before it would have written output. Doesn't go below this
# floor — past this point the summary would be too unrepresentative to be
# worth generating at all, and the caller should see the real error instead.
_MIN_THESIS_BRIEFS = 20

# Second local-model pass, intermediate round (no core_thesis at this level —
# only used when a batch still needs merging with siblings before the final pass).
BATCH_MERGE_PROMPT_TEMPLATE = """\
You are merging ONE BATCH of candidate principles drafted from a run of \
consecutive chapters in a book, deduping overlaps, for a human editor to \
review later. This is an intermediate step — you are NOT writing the book's \
final thesis here, only consolidating this batch into a shorter list.

RULES (must follow exactly):
- Merge near-duplicate candidates into a single principle; keep genuinely \
distinct ideas separate. Do not drop a genuinely distinct idea just to \
shorten the list.
- Do not invent principles that aren't represented in the candidates below.
- Paraphrase; do not fabricate new verbatim quotes. Respect: at most one \
quote per principle, at most 15 words per quote.

BOOK: {book_title} by {book_author}

CANDIDATE PRINCIPLES (this batch only, may contain duplicates/overlap):
{candidates_json}

TASK:
Return valid JSON only, matching this shape:
{{
  "principles": [
    {{
      "name": "short principle name",
      "summary": "paraphrased summary, at most 200 words, at most one quote \
of at most 15 words",
      "source_chapter": "chapter label this principle mainly comes from, or \
null if merged from several",
      "applies_to_tags": ["tag-one", "tag-two"]
    }}
  ]
}}
No prose outside the JSON.
"""

# Lightweight fallback when there are too many merged principles left for a
# safe full-detail final call (see DEFAULT_SAFE_FINAL_LIMIT). Uses short
# one-line briefs instead of full name+summary+tags per principle, so the
# prompt stays small regardless of how many principles remain.
THESIS_ONLY_PROMPT_TEMPLATE = """\
You are writing ONLY a book's core thesis paragraph, based on a consolidated \
list of its principles (shown below as short one-line briefs, not full \
text), for a human editor to review later.

RULES (must follow exactly):
- Write ONE core_thesis paragraph, at most 150 words, that states the \
book's overall argument, based only on the principle briefs below.
- Do not invent claims the briefs don't support.

BOOK: {book_title} by {book_author}

PRINCIPLE BRIEFS ({count} total):
{briefs}

TASK:
Return valid JSON only, matching this shape:
{{
  "core_thesis": "at most 150 words"
}}
No prose outside the JSON.
"""

# Second local-model pass (architecture §2 Path B, "Aggregation pass"): merges
# and dedupes candidate principles across all chunks into one draft core_thesis
# and a consolidated principles list.
AGGREGATION_PROMPT_TEMPLATE = """\
You are merging candidate principles drafted separately from every chapter of \
a book into one consolidated draft, for a human editor to review later.

RULES (must follow exactly):
- Merge near-duplicate candidates from different chapters into a single \
principle; keep genuinely distinct ideas separate.
- Do not invent principles that aren't represented in the candidates below.
- Paraphrase; do not fabricate new verbatim quotes. If a candidate's summary \
already contains a quotation, you may keep it, but still respect: at most one \
quote per principle, at most 15 words per quote.
- Write ONE core_thesis paragraph (at most 150 words) that states the book's \
overall argument, based only on the candidates below.

BOOK: {book_title} by {book_author}

CANDIDATE PRINCIPLES (drafted per-chapter, may contain duplicates/overlap):
{candidates_json}

TASK:
Return valid JSON only, matching this shape:
{{
  "core_thesis": "at most 150 words",
  "principles": [
    {{
      "name": "short principle name",
      "summary": "paraphrased summary, at most 200 words, at most one quote \
of at most 15 words",
      "source_chapter": "chapter label this principle mainly comes from, or \
null if merged from several",
      "applies_to_tags": ["tag-one", "tag-two"]
    }}
  ]
}}
No prose outside the JSON.
"""


def _candidates_to_json(candidates: list[CandidatePrinciple]) -> str:
    return json.dumps(
        [
            {
                "name": c.name,
                "summary": c.summary,
                "source_chapter": c.source_chapter,
                "applies_to_tags": c.applies_to_tags,
            }
            for c in candidates
        ],
        indent=2,
    )


def _brief(principle: CandidatePrinciple, max_words: int = 12) -> str:
    """A short one-line summary of a principle, for prompts whose size must
    stay bounded regardless of how many principles are being described."""
    words = principle.summary.split()
    snippet = " ".join(words[:max_words])
    if len(words) > max_words:
        snippet += "..."
    return f"{principle.name}: {snippet}"


def _evenly_sample(principles: list[CandidatePrinciple], count: int) -> list[CandidatePrinciple]:
    """Evenly-spaced sample of `principles` down to `count` items, preserving
    original order — used to shrink a too-large thesis-briefs prompt while
    keeping representation from across the whole book rather than just
    truncating to its first N (which would skew toward early chapters)."""
    if count >= len(principles):
        return principles
    step = len(principles) / count
    return [principles[int(i * step)] for i in range(count)]


def build_thesis_only_prompt(
    book_title: str, book_author: str, principles: list[CandidatePrinciple]
) -> str:
    briefs = "\n".join(f"- {_brief(p)}" for p in principles)
    return THESIS_ONLY_PROMPT_TEMPLATE.format(
        book_title=book_title,
        book_author=book_author,
        count=len(principles),
        briefs=briefs,
    )


def parse_thesis_only_response(raw_response: str) -> str:
    try:
        payload = json.loads(extract_json_object(raw_response))
    except json.JSONDecodeError as exc:
        raise AggregationError(f"model response was not valid JSON: {exc}") from exc
    core_thesis = payload.get("core_thesis")
    if not isinstance(core_thesis, str):
        raise AggregationError("model response JSON missing a 'core_thesis' string")
    return core_thesis


def generate_core_thesis_from_briefs(
    client: LocalModelClient,
    book_title: str,
    book_author: str,
    principles: list[CandidatePrinciple],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> str:
    """Generate core_thesis from principle briefs, shrinking (evenly-sampled,
    never truncated to just the head) and retrying if Groq rejects the
    prompt as too large (see _MIN_THESIS_BRIEFS). Safe to sample here
    specifically because this call only produces the prose core_thesis
    paragraph — the actual saved principles list (`principles`, unmodified)
    is unaffected either way; see aggregate_candidates_hierarchical.
    """
    briefs_to_use = principles
    last_error: AggregationError | None = None

    while True:
        base_prompt = build_thesis_only_prompt(book_title, book_author, briefs_to_use)
        context_length = select_context_length(base_prompt)
        logger.info(
            "generating core_thesis from %d principle briefs (context_length=%d)",
            len(briefs_to_use),
            context_length,
        )

        too_large = False
        for attempt in range(1, max_attempts + 1):
            prompt = base_prompt if attempt == 1 else base_prompt + _RETRY_REMINDER
            try:
                raw_response = client.generate(
                    prompt, context_length=context_length, timeout=AGGREGATION_TIMEOUT_SECONDS
                )
            except PayloadTooLargeError:
                if len(briefs_to_use) <= _MIN_THESIS_BRIEFS:
                    raise
                too_large = True
                break
            try:
                return parse_thesis_only_response(raw_response)
            except AggregationError as exc:
                last_error = exc
                logger.warning(
                    "thesis-only attempt %d/%d failed to parse model response: %s",
                    attempt,
                    max_attempts,
                    exc,
                )

        if too_large:
            new_count = max(_MIN_THESIS_BRIEFS, len(briefs_to_use) // 2)
            logger.warning(
                "core_thesis prompt rejected as too large by Groq (413) with %d briefs; "
                "retrying with an evenly-sampled %d",
                len(briefs_to_use),
                new_count,
            )
            briefs_to_use = _evenly_sample(briefs_to_use, new_count)
            continue

        assert last_error is not None
        raise last_error


def build_aggregation_prompt(
    book_title: str, book_author: str, candidates: list[CandidatePrinciple]
) -> str:
    return AGGREGATION_PROMPT_TEMPLATE.format(
        book_title=book_title,
        book_author=book_author,
        candidates_json=_candidates_to_json(candidates),
    )


def build_batch_merge_prompt(
    book_title: str, book_author: str, candidates: list[CandidatePrinciple]
) -> str:
    return BATCH_MERGE_PROMPT_TEMPLATE.format(
        book_title=book_title,
        book_author=book_author,
        candidates_json=_candidates_to_json(candidates),
    )


class AggregationError(RuntimeError):
    """Raised when an aggregation-pass model response isn't parseable in the expected shape."""


def parse_aggregation_response(raw_response: str) -> AggregatedDraft:
    try:
        payload = json.loads(extract_json_object(raw_response))
    except json.JSONDecodeError as exc:
        raise AggregationError(f"model response was not valid JSON: {exc}") from exc

    if "core_thesis" not in payload or "principles" not in payload:
        raise AggregationError("model response JSON missing 'core_thesis' or 'principles'")

    principles_payload = payload["principles"]
    if not isinstance(principles_payload, list):
        raise AggregationError("'principles' must be a list")

    principles: list[CandidatePrinciple] = []
    for entry in principles_payload:
        if not isinstance(entry, dict) or "name" not in entry or "summary" not in entry:
            raise AggregationError(f"malformed principle entry: {entry!r}")
        principles.append(
            CandidatePrinciple(
                name=entry["name"],
                summary=entry["summary"],
                source_chapter=entry.get("source_chapter"),
                applies_to_tags=list(entry.get("applies_to_tags", [])),
            )
        )

    return AggregatedDraft(core_thesis=payload["core_thesis"], principles=principles)


def parse_batch_merge_response(raw_response: str) -> list[CandidatePrinciple]:
    try:
        payload = json.loads(extract_json_object(raw_response))
    except json.JSONDecodeError as exc:
        raise AggregationError(f"model response was not valid JSON: {exc}") from exc

    principles_payload = payload.get("principles")
    if not isinstance(principles_payload, list):
        raise AggregationError("model response JSON missing a 'principles' list")

    principles: list[CandidatePrinciple] = []
    for entry in principles_payload:
        if not isinstance(entry, dict) or "name" not in entry or "summary" not in entry:
            raise AggregationError(f"malformed principle entry: {entry!r}")
        principles.append(
            CandidatePrinciple(
                name=entry["name"],
                summary=entry["summary"],
                source_chapter=entry.get("source_chapter"),
                applies_to_tags=list(entry.get("applies_to_tags", [])),
            )
        )
    return principles


def merge_batch(
    client: LocalModelClient,
    book_title: str,
    book_author: str,
    batch: list[CandidatePrinciple],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[CandidatePrinciple]:
    """Merge/dedupe one batch of candidates — an intermediate round in
    hierarchical aggregation, producing no core_thesis (see
    aggregate_candidates_hierarchical for why this exists)."""
    base_prompt = build_batch_merge_prompt(book_title, book_author, batch)
    context_length = select_context_length(base_prompt)

    last_error: AggregationError | None = None
    for attempt in range(1, max_attempts + 1):
        prompt = base_prompt if attempt == 1 else base_prompt + _RETRY_REMINDER
        raw_response = client.generate(
            prompt, context_length=context_length, timeout=AGGREGATION_TIMEOUT_SECONDS
        )
        try:
            return parse_batch_merge_response(raw_response)
        except AggregationError as exc:
            last_error = exc
            logger.warning(
                "batch merge attempt %d/%d failed to parse model response: %s",
                attempt,
                max_attempts,
                exc,
            )

    assert last_error is not None
    raise last_error


def aggregate_candidates_hierarchical(
    client: LocalModelClient,
    book_title: str,
    book_author: str,
    candidates: list[CandidatePrinciple],
    batch_size: int = DEFAULT_BATCH_SIZE,
    single_pass_threshold: int = DEFAULT_SINGLE_PASS_THRESHOLD,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    min_reduction_ratio: float = DEFAULT_MIN_REDUCTION_RATIO,
    safe_final_limit: int = DEFAULT_SAFE_FINAL_LIMIT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    show_progress: bool = False,
) -> AggregatedDraft:
    """Reduce a (possibly large) candidate list via batch-merge passes, then
    produce the final core_thesis + consolidated principles list.

    Why batching exists at all: feeding hundreds of candidates into a single
    aggregation call can overwhelm a small local model's effective attention.
    Observed in practice — 211 candidates in one call produced a core_thesis
    that was just the prompt's own placeholder text echoed back, and silently
    dropped candidates from 16 of the book's 21 chapters, despite the prompt
    fitting the chosen context window token-for-token.

    Why batching is *bounded* (max_rounds, min_reduction_ratio) rather than
    looping until len(current) <= single_pass_threshold: a real run showed
    each round's reduction shrinking fast (-19%, -15%, -7%) — most of a full
    book's candidates are genuinely distinct ideas, not duplicates. Chasing a
    small threshold from there could run for hours without ever reaching it.
    Stopping once a round stops paying off is the honest reflection of that,
    not a shortcut.

    Why there's a safe_final_limit: if batching stops with more candidates
    than this, a full-detail final call (every candidate's complete
    name+summary+tags) risks the exact overwhelm failure above. Past that
    size, core_thesis is generated from short one-line briefs instead (a
    prompt whose size stays bounded no matter how many principles remain),
    and the merged list is used as the final principles as-is — no further
    full-content merge is attempted at that volume.
    """
    current = candidates
    round_number = 0
    while round_number < max_rounds and len(current) > single_pass_threshold:
        round_number += 1
        batches = [current[i : i + batch_size] for i in range(0, len(current), batch_size)]
        logger.info(
            "hierarchical aggregation round %d/%d: merging %d candidates across %d batch(es) of up to %d",
            round_number,
            max_rounds,
            len(current),
            len(batches),
            batch_size,
        )
        merged: list[CandidatePrinciple] = []
        batch_progress = tqdm(
            enumerate(batches),
            total=len(batches),
            desc=f"Aggregation round {round_number}/{max_rounds}",
            unit="batch",
            disable=not show_progress,
        )
        for batch_index, batch in batch_progress:
            batch_progress.set_postfix_str(f"batch {batch_index + 1}/{len(batches)}")
            try:
                batch_result = merge_batch(client, book_title, book_author, batch, max_attempts)
            except AggregationError as exc:
                # A batch merge is a dedup/cleanup step, not a correctness-
                # critical one — losing this batch's candidates entirely (or
                # aborting the whole run after other batches already
                # succeeded) would be worse than just skipping the merge for
                # this one batch. Pass its candidates through unmerged; later
                # rounds or the final pass get another chance to dedupe them.
                logger.warning(
                    "  round %d batch %d/%d: merge failed after retries (%s); "
                    "passing its %d candidates through unmerged",
                    round_number,
                    batch_index + 1,
                    len(batches),
                    exc,
                    len(batch),
                )
                batch_result = batch
            logger.info(
                "  round %d batch %d/%d: %d -> %d candidates",
                round_number,
                batch_index + 1,
                len(batches),
                len(batch),
                len(batch_result),
            )
            merged.extend(batch_result)
        batch_progress.close()

        previous_count = len(current)
        if len(merged) >= previous_count:
            logger.warning(
                "hierarchical aggregation made no progress in round %d (%d -> %d candidates); "
                "stopping batching",
                round_number,
                previous_count,
                len(merged),
            )
            current = merged
            break

        reduction_ratio = 1 - (len(merged) / previous_count)
        current = merged
        logger.info(
            "round %d complete: %d -> %d candidates (%.0f%% reduction)",
            round_number,
            previous_count,
            len(current),
            reduction_ratio * 100,
        )
        if reduction_ratio < min_reduction_ratio:
            logger.info(
                "reduction ratio %.0f%% below minimum %.0f%%; stopping batching early",
                reduction_ratio * 100,
                min_reduction_ratio * 100,
            )
            break

    if len(current) <= safe_final_limit:
        logger.info("final aggregation pass (full detail): %d candidates", len(current))
        if show_progress:
            tqdm.write(f"Final pass: writing core_thesis + {len(current)} consolidated principles...")
        return aggregate_candidates(client, book_title, book_author, current, max_attempts)

    logger.info(
        "skipping full-detail final merge (%d candidates > safe limit %d); "
        "generating core_thesis from brief summaries instead",
        len(current),
        safe_final_limit,
    )
    if show_progress:
        tqdm.write(
            f"{len(current)} principles remain (above the {safe_final_limit}-candidate safe "
            "limit) — generating core_thesis from short briefs instead of a full merge..."
        )
    core_thesis = generate_core_thesis_from_briefs(client, book_title, book_author, current, max_attempts)
    return AggregatedDraft(core_thesis=core_thesis, principles=current)


def aggregate_candidates(
    client: LocalModelClient,
    book_title: str,
    book_author: str,
    candidates: list[CandidatePrinciple],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> AggregatedDraft:
    """Call the model for the aggregation pass and parse its response.

    Retries the same request, with an added reminder, up to `max_attempts`
    times on a JSON parse failure before giving up — never hand-patches the
    model's actual JSON syntax, only retries the call.
    """
    base_prompt = build_aggregation_prompt(book_title, book_author, candidates)
    context_length = select_context_length(base_prompt)
    logger.info(
        "aggregation pass: merging %d candidate principles (context_length=%d)",
        len(candidates),
        context_length,
    )

    last_error: AggregationError | None = None
    for attempt in range(1, max_attempts + 1):
        prompt = base_prompt if attempt == 1 else base_prompt + _RETRY_REMINDER
        raw_response = client.generate(
            prompt, context_length=context_length, timeout=AGGREGATION_TIMEOUT_SECONDS
        )
        try:
            return parse_aggregation_response(raw_response)
        except AggregationError as exc:
            last_error = exc
            logger.warning(
                "aggregation attempt %d/%d failed to parse model response: %s",
                attempt,
                max_attempts,
                exc,
            )

    assert last_error is not None
    raise last_error
