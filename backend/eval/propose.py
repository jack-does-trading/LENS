"""Candidate generation for assisted labelling.

The labeller's bottleneck was never judgement, it was *search*: finding the
handful of relevant principles among 387 by typing substring queries. This
module does the finding. A human still does the deciding.

Two properties keep this from quietly grading the pipeline against itself:

  1. **It never uses retrieval.** No tag matching, no embeddings, no
     `rank_fusion`. The model reads principle summaries directly, in batches
     covering every human-reviewed principle in the book. If it shared a
     mechanism with the thing under test, agreement between them would be
     guaranteed rather than informative.
  2. **The shortlist is deliberately over-broad** (~10, against a top_k of 3).
     A human choosing from 10 is exercising judgement; a human confirming 3 is
     rubber-stamping. Recall-oriented here, precision comes from the person.

Validated against a hand-labelled control set -- see scripts/compare_labels.py.
Until that comparison has been run, labels produced this way are provisional.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.json_extraction import JSONExtractionError, extract_json_object
from app.llm import LLMClient
from app.models import Principle

# Small enough that the model attends to every principle in the batch rather
# than skimming and favouring the top of the list, large enough that a
# 387-principle book costs 4 calls rather than 7 -- which matters because
# Groq's free tier 429s and each retry adds 10-25s of backoff.
DEFAULT_BATCH = 100
DEFAULT_SHORTLIST = 10
PER_BATCH_PICKS = 5


class ProposalError(RuntimeError):
    """The model's proposal could not be parsed or contained nothing usable."""


@dataclass(frozen=True)
class Candidate:
    principle_id: str
    reason: str


SELECT_PROMPT = """\
You are helping build an evaluation set for a book-grounded advice system.

Below is a situation someone logged, and a numbered list of principles drawn
from one book. Your job is to find which principles a knowledgeable reader of
that book would say are genuinely relevant to this situation -- the ones that
would make for useful, grounded advice.

SITUATION:
{situation}

PRINCIPLES:
{principles_block}

Pick at most {picks}. Fewer is correct when fewer are genuinely relevant --
padding the list with loosely-related principles makes the evaluation set
worse, not better. If none are relevant, return an empty list.

For each pick give a reason of at most 15 words, phrased so a human reviewer
can judge your choice quickly.

Return valid JSON only, matching this shape:
{{"candidates": [{{"principle_id": "<exact id from the list>", "reason": "..."}}]}}
No prose outside the JSON. Use principle_id values exactly as written above.
"""


def format_situation(entries: list[dict], mood: int | None) -> str:
    lines = [f"- {e.get('time', '--:--')}: {e.get('action', '')} [{e.get('category', '')}]" for e in entries]
    if mood is not None:
        lines.append(f"- mood: {mood}/5")
    return "\n".join(lines)


def _block(principles: list[Principle]) -> str:
    return "\n".join(
        f"- id: {p.principle_id}\n  name: {p.name}\n  summary: {p.summary}" for p in principles
    )


def _ask(client: LLMClient, situation: str, principles: list[Principle], picks: int) -> list[Candidate]:
    prompt = SELECT_PROMPT.format(
        situation=situation, principles_block=_block(principles), picks=picks
    )
    raw = client.generate(prompt)
    try:
        payload = json.loads(extract_json_object(raw))
    except (JSONExtractionError, json.JSONDecodeError) as exc:
        raise ProposalError(f"unparseable proposal: {exc}") from exc

    valid = {p.principle_id for p in principles}
    out: list[Candidate] = []
    for item in payload.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("principle_id", "")).strip()
        # Models invent plausible-looking slugs. An unknown id is dropped
        # rather than surfaced -- a reviewer should never be offered a
        # principle that doesn't exist.
        if pid in valid and pid not in {c.principle_id for c in out}:
            out.append(Candidate(principle_id=pid, reason=str(item.get("reason", "")).strip()))
    return out


def propose_candidates(
    client: LLMClient,
    situation: str,
    principles: list[Principle],
    *,
    batch_size: int = DEFAULT_BATCH,
    shortlist: int = DEFAULT_SHORTLIST,
) -> list[Candidate]:
    """Map-reduce a shortlist out of every principle in the book.

    Map: each batch nominates its best few. Reduce: one pass over the union
    picks the final shortlist. Chunking is what lets this cover a 387-principle
    book without relying on the model to attend evenly across 10k words.
    """
    if not principles:
        return []

    batches = [principles[i : i + batch_size] for i in range(0, len(principles), batch_size)]
    nominated: list[Candidate] = []
    for batch in batches:
        try:
            nominated.extend(_ask(client, situation, batch, PER_BATCH_PICKS))
        except ProposalError:
            # One bad batch must not lose the other batches' work; the reviewer
            # can always fall back to /s search for anything missed.
            continue

    if not nominated:
        raise ProposalError("no usable candidates from any batch")
    if len(nominated) <= shortlist:
        return nominated

    by_id = {p.principle_id: p for p in principles}
    finalists = [by_id[c.principle_id] for c in nominated if c.principle_id in by_id]
    try:
        return _ask(client, situation, finalists, shortlist)[:shortlist]
    except ProposalError:
        return nominated[:shortlist]
