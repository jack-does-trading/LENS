"""The judge calibration gate, replayed from the recorded Groq verdicts.

Marked `eval`: no API key, no network, no cost, and the same numbers every run.
The verdicts in eval/cassettes/judge_verdicts.json were produced once by real
Groq (scripts/calibrate_judge.py --record) and are keyed by a hash of the full
prompt, so editing the rubric or a calibration case invalidates its recording
and the replay misses loudly instead of quietly scoring a question the judge was
never asked.

What this gate protects: every faithfulness number the project quotes. If the
judge stops agreeing with the human labels, the numbers it produces stop meaning
anything -- and unlike a crash, that failure is silent.
"""

from __future__ import annotations

import pytest

from app.config import settings
from eval import judge_cases
from eval.calibration import calibrate
from eval.cassettes import CassetteJudgeClient, CassetteError, load_judge_verdicts
from eval.harness import load_thresholds
from eval.judge import PROMPT_VERSION, build_prompt, judge_output

pytestmark = pytest.mark.eval


def _replay() -> dict[str, dict]:
    client = CassetteJudgeClient(settings.groq_model)
    return {
        c.case_id: judge_output(client, judge_cases.to_judge_input(c)).as_labels()
        for c in judge_cases.load()
    }


def test_the_cassette_covers_every_calibration_case() -> None:
    """A partial recording would silently shrink n. The replay would still
    produce a kappa -- over fewer cases than the report claims."""
    cases = judge_cases.load()
    client = CassetteJudgeClient(settings.groq_model)
    for case in cases:
        client.generate(build_prompt(judge_cases.to_judge_input(case)))
    assert not client.misses
    assert len(load_judge_verdicts()) >= len(cases)


def test_recorded_verdicts_were_made_with_the_current_rubric() -> None:
    """A verdict recorded against an older JUDGE_PROMPT answers a different
    question. The prompt hash already makes such an entry unreachable; this
    asserts nothing stale is being counted."""
    for key, entry in load_judge_verdicts().items():
        assert entry["prompt_version"] == PROMPT_VERSION, (
            f"{key[:12]}... was recorded under {entry['prompt_version']}, "
            f"current rubric is {PROMPT_VERSION}; re-record"
        )


def test_a_missing_verdict_raises_instead_of_defaulting() -> None:
    """The single worst thing an eval harness can do is degrade quietly. A
    cassette miss that returned a default verdict would hand back a plausible
    kappa for a judge that answered nothing."""
    client = CassetteJudgeClient(settings.groq_model, verdicts={})
    with pytest.raises(CassetteError):
        client.generate("a prompt nobody recorded")


def test_a_verdict_recorded_for_a_different_model_is_not_replayed() -> None:
    """Keying on the model as well as the prompt: gpt-oss-120b's verdicts are
    not evidence about whatever Groq recommends next."""
    client = CassetteJudgeClient("some-other-model")
    with pytest.raises(CassetteError):
        client.generate(build_prompt(judge_cases.to_judge_input(judge_cases.load()[0])))


def test_the_judge_agrees_with_the_human_above_the_kappa_floor() -> None:
    """The gate. Below this floor, no faithfulness score may be quoted anywhere
    -- not in the README, not in a report, not in an interview."""
    floor = load_thresholds()["judge"]["human_kappa_floor"]
    cases = judge_cases.load()
    results = calibrate(judge_cases.human_labels(cases), _replay())
    assert len(results) == 3, "all three rubric fields must be scored"
    for r in results:
        assert r.kappa is not None, f"{r.field}: {r.note}"
        assert r.kappa >= floor, (
            f"{r.field} kappa {r.kappa:.2f} < floor {floor:.2f} ({r.band}); "
            "fix the judge prompt before trusting its scores"
        )


def test_the_calibration_gate_can_actually_fail() -> None:
    """A gate that has never failed is not a gate.

    Substitutes a judge that scores everything 5/no-hallucination -- the exact
    degenerate rater that would sail through a raw-agreement check -- and
    asserts the kappa gate rejects it.
    """
    floor = load_thresholds()["judge"]["human_kappa_floor"]
    cases = judge_cases.load()
    lazy = {
        c.case_id: {"faithfulness": 5, "hallucination": False, "suggestion_groundedness": 5}
        for c in cases
    }
    results = calibrate(judge_cases.human_labels(cases), lazy)
    scored = [r for r in results if r.kappa is not None]
    assert scored, "the degenerate judge should still produce at least one defined kappa"
    assert all(r.kappa < floor for r in scored), [
        (r.field, r.kappa) for r in scored
    ]


def test_the_judge_is_not_merely_reproducing_the_faithfulness_score() -> None:
    """If the judge derived its hallucination flag from its own faithfulness
    number, the flag's kappa would carry no independent information. Two
    calibration cases separate the two axes; this checks the judge actually
    treats them separately on at least one of them."""
    replayed = _replay()
    high_faith_flagged = [
        cid for cid, v in replayed.items() if v["hallucination"] and v["faithfulness"] >= 4
    ]
    low_faith_unflagged = [
        cid for cid, v in replayed.items() if not v["hallucination"] and v["faithfulness"] <= 2
    ]
    assert high_faith_flagged or low_faith_unflagged, (
        "the judge's flag tracks its faithfulness score exactly, so the flag's "
        "kappa is not independent evidence"
    )



def test_every_flagged_case_names_the_phrase_it_flagged() -> None:
    """v2's mechanism, asserted directly.

    Under judge-v1 the flag was a deterministic function of the judge's own
    faithfulness score -- flagged iff faithfulness <= 2, on all 16 cases -- so
    its kappa of 0.73 was re-measuring faithfulness rather than validating the
    flag. v2 requires the judge to quote the invented phrase *before* scoring
    anything. A flag that must cite evidence cannot be derived from a score, and
    a `true` with nothing quoted is an unfalsifiable accusation.
    """
    client = CassetteJudgeClient(settings.groq_model)
    for case in judge_cases.load():
        verdict = judge_output(client, judge_cases.to_judge_input(case))
        assert verdict.flag_has_evidence, (
            f"{case.case_id}: judge flagged a hallucination but quoted nothing"
        )
        if not verdict.hallucination:
            assert verdict.hallucinated_item is None
