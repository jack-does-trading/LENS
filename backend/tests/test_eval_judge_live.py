"""The same judge, asked again for real. Needs GROQ_API_KEY.

Split into its own module rather than marked inline, because tests/test_eval_judge.py
carries a module-level `pytest.mark.eval` and a marker cannot be removed from a
single test in a marked module. Left in place, this test was selected by
`pytest -m eval` -- the no-secrets, no-cost PR gate -- where it would have made
sixteen live Groq calls per run, or silently skipped for want of a key. A gate
that sometimes skips is not a gate, and one that sometimes bills is not free.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.llm import GroqLLMClient
from eval import judge_cases
from eval.calibration import calibrate
from eval.harness import load_thresholds
from eval.judge import judge_output

pytestmark = pytest.mark.eval_live


def test_the_live_judge_still_agrees_with_the_recording() -> None:
    """Hits real Groq. Run with: pytest -m eval_live

    This is what catches a model decommission or a silent provider-side change
    -- the class of failure that has already bitten this repo once. A drop here
    with no code change means the cassette is stale, not that the pipeline broke.
    """
    if not settings.groq_api_key:
        pytest.skip("GROQ_API_KEY not set")
    floor = load_thresholds()["judge"]["human_kappa_floor"]
    client = GroqLLMClient(model=settings.groq_model, api_key=settings.groq_api_key)
    cases = judge_cases.load()
    live = {
        c.case_id: judge_output(client, judge_cases.to_judge_input(c)).as_labels()
        for c in cases
    }
    results = calibrate(judge_cases.human_labels(cases), live)
    for r in results:
        assert r.kappa is not None and r.kappa >= floor, (r.field, r.kappa, r.note)
