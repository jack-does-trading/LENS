"""Regenerating `applies_to_tags` for principles that never got real ones.

The local extraction tool copied the literal placeholders from its own prompt's
schema example -- `tag-one`, `tag-two`, `tag-three` -- into 854 of 2347 tag
assignments, covering 93-100% of four of the seven published books. Those
principles then passed the human review gate, which in practice was a
`mark_reviewed.py` flip rather than a read.

Two things break as a result:

  1. Tag matching, the half of retrieval weighted 2x *because* it is precise,
     cannot fire for those books at all.
  2. The tags are embedded too -- `embeddings.py` builds its input as
     "{name}. {summary} Tags: {tags}" -- so every principle in an affected book
     carries the same meaningless suffix in its vector.

The fix is not just "generate some tags". Measured against the 16 real logged
situations, the tag arm fired on 1 of 16 entries, and the misses were things
users actually typed: work, family, relationships, money, health, life. Free-form
model-generated tags would be no more likely to collide with that vocabulary
than the ones they replace. So tagging is constrained to a fixed set of life
domains that matches how people describe their own days, plus a couple of
book-specific terms for precision.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.json_extraction import JSONExtractionError, extract_json_object
from app.llm import LLMClient
from app.models import Principle

# Literal leftovers from the extraction prompt's example schema.
PLACEHOLDER = re.compile(r"^tag-(one|two|three|four|five|six|seven|eight|nine|ten|\d+)$")

# A closed vocabulary, chosen to overlap the categories real users type into
# this app rather than to describe the books tidily. Retrieval lowercases and
# strips both sides, so these match a user's "Work" or "Family " as written.
LIFE_DOMAINS = [
    "work", "career", "money", "health", "fitness", "relationships", "family",
    "parenting", "friendship", "love", "self-esteem", "confidence", "fear",
    "anxiety", "anger", "motivation", "discipline", "habits", "procrastination",
    "purpose", "meaning", "failure", "success", "conflict", "negotiation",
    "leadership", "power", "communication", "boundaries", "responsibility",
    "change", "growth", "focus", "decision-making", "loneliness", "grief",
    "existence", "life",
]

BATCH = 12

RETAG_PROMPT = """\
You are assigning retrieval tags to principles extracted from a book.

These tags are matched against the category a person types when logging their
day -- words like "work", "family", "money", "low self confidence". A tag only
earns its place if someone in that situation would plausibly have used it.

For each principle below, choose:
  - 2 to 4 tags from this fixed list (use the exact spelling):
    {domains}
  - 0 to 2 extra tags specific to this principle's idea, lowercase, hyphenated,
    at most three words (e.g. "sunk-cost", "loss-aversion").

Fewer, accurate tags beat more. Do not tag a principle with a domain it only
loosely touches -- a tag that matches everything retrieves nothing useful.

PRINCIPLES:
{principles_block}

Return valid JSON only, matching this shape:
{{"tagged": [{{"principle_id": "<exact id>", "tags": ["...", "..."]}}]}}
No prose outside the JSON. Include every principle_id listed above exactly once.
"""


class TaggingError(RuntimeError):
    pass


@dataclass(frozen=True)
class Retag:
    principle_id: str
    old_tags: list[str]
    new_tags: list[str]


def needs_retagging(principle: Principle) -> bool:
    """True when a principle has no usable tag at all.

    Deliberately not "has any placeholder": a principle carrying one real tag
    plus one placeholder still works for retrieval, and rewriting it would
    discard a human's actual choice to fix cosmetics.
    """
    tags = [t.strip().lower() for t in principle.applies_to_tags if t.strip()]
    return not tags or all(PLACEHOLDER.match(t) for t in tags)


def _clean(tag: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", tag.strip().lower()).strip("-")


def propose_tags(client: LLMClient, principles: list[Principle]) -> dict[str, list[str]]:
    block = "\n".join(
        f"- id: {p.principle_id}\n  name: {p.name}\n  summary: {p.summary}" for p in principles
    )
    raw = client.generate(
        RETAG_PROMPT.format(domains=", ".join(LIFE_DOMAINS), principles_block=block)
    )
    try:
        payload = json.loads(extract_json_object(raw))
    except (JSONExtractionError, json.JSONDecodeError) as exc:
        raise TaggingError(f"unparseable tagging response: {exc}") from exc

    valid_ids = {p.principle_id for p in principles}
    out: dict[str, list[str]] = {}
    for item in payload.get("tagged") or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("principle_id", "")).strip()
        if pid not in valid_ids:
            continue
        tags, seen = [], set()
        for t in item.get("tags") or []:
            cleaned = _clean(str(t))
            # A regenerated placeholder would be the exact bug returning.
            if not cleaned or PLACEHOLDER.match(cleaned) or cleaned in seen:
                continue
            seen.add(cleaned)
            tags.append(cleaned)
        if tags:
            out[pid] = tags[:6]
    return out
