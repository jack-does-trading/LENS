"""Paraphrase golden-set entry text so the set can be committed.

The labelled golden set is only useful if it travels with the repo, but every
case is a real site visitor's journal entry in their own words -- including two
disclosures of suicidal ideation. Those must not reach a public remote.

The paraphrase has to thread a needle. It must keep everything retrieval
depends on -- the situation, the domain, the emotional register -- because the
expected_principle_ids were labelled against the *meaning*. If a paraphrase
drifts semantically, the labels silently stop applying and every number
computed from them becomes fiction. It must simultaneously drop the author's
phrasing, and any name, place or detail that could identify them.

So this is deliberately a rewrite, not a redaction to structure: replacing the
text with "[redacted] [Work]" would preserve privacy perfectly and destroy the
eval set. scripts/redact_eval_cases.py re-scores retrieval before and after and
refuses to accept a drift it cannot justify.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.json_extraction import extract_json_object
from app.llm import LLMClient

PROMPT_VERSION = "redact-v1"

REDACT_PROMPT = """You rewrite short personal journal entries so they can be published as test data.

Rewrite the entry below so that:
- the situation, the domain of life, and the emotional intensity are UNCHANGED
- a reader would classify it the same way and think of the same advice
- NO phrase of four or more consecutive words survives from the original
- no names, workplaces, places, ages, or uniquely identifying details remain
- it stays first-person and roughly the same length
- you do NOT soften, censor, or add advice. If the entry describes self-harm or
  suicidal thoughts, the rewrite must describe them just as plainly. This is
  test data for a safety-relevant system; sanitising it would hide the very
  cases that matter most.

Original entry (category: {category}):
{action}

Return ONLY this JSON:
{{"rewrite": "..."}}"""


class RedactionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Rewrite:
    original: str
    rewritten: str
    category: str

    @property
    def longest_shared_phrase(self) -> int:
        """Longest run of consecutive words the rewrite shares with the original.

        The cheapest objective check that a rewrite actually happened -- an LLM
        told to paraphrase will sometimes echo the input nearly verbatim, and
        that failure is silent unless something measures it.
        """
        a = self.original.lower().split()
        b = set()
        words = self.rewritten.lower().split()
        for n in range(1, len(words) + 1):
            for i in range(len(words) - n + 1):
                b.add(" ".join(words[i : i + n]))
        best = 0
        for n in range(1, len(a) + 1):
            for i in range(len(a) - n + 1):
                if " ".join(a[i : i + n]) in b:
                    best = max(best, n)
        return best


def rewrite_entry(client: LLMClient, action: str, category: str) -> Rewrite:
    prompt = REDACT_PROMPT.format(category=category or "(none)", action=action)
    raw = client.generate(prompt)
    try:
        payload = json.loads(extract_json_object(raw))
    except (ValueError, json.JSONDecodeError) as exc:
        raise RedactionError(f"unparseable rewrite response: {raw[:200]!r}") from exc
    text = (payload.get("rewrite") or "").strip()
    if not text:
        raise RedactionError(f"empty rewrite for {action[:60]!r}")
    return Rewrite(original=action, rewritten=text, category=category)
