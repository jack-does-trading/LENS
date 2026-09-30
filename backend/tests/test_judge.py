"""The judge, and the calibration that licenses trusting it.

Three tiers, on purpose:

  * unmarked   -- parsing, the kappa arithmetic, and the shape of the
                  calibration set. No network, no database, runs on every push.
  * `eval`     -- replays the recorded Groq verdicts and asserts the kappa floor.
                  No key, no cost; this is the gate.
  * `eval_live`-- asks the real provider again, so a model swap or a Groq
                  decommission surfaces in the nightly run rather than in a
                  number nobody rechecked.
"""

from __future__ import annotations

import json

import pytest

from app.verification import _rule_based_issues
from eval import judge_cases
from eval.calibration import (
    CalibrationError,
    calibrate,
    cohens_kappa,
    confusion_matrix,
    interpret,
    weighted_kappa,
)
from eval.judge import (
    JudgeError,
    aggregate_verdicts,
    build_prompt,
    parse_verdict,
)

# --- parsing ---------------------------------------------------------------


def test_parses_a_clean_verdict() -> None:
    verdict = parse_verdict(
        '{"faithfulness": 4, "hallucination": false, '
        '"suggestion_groundedness": 5, "reason": "fine"}'
    )
    assert (verdict.faithfulness, verdict.hallucination, verdict.suggestion_groundedness) == (4, False, 5)
    assert verdict.passes_ship_gate


def test_parses_a_fenced_verdict() -> None:
    """Groq is under no obligation to omit a markdown fence, and a bare
    json.loads here is the exact bug that once forced every analysis in
    production to the fallback template."""
    raw = '```json\n{"faithfulness": 5, "hallucination": false, "suggestion_groundedness": 5}\n```'
    assert parse_verdict(raw).faithfulness == 5


def test_accepts_stringly_typed_scores_and_flags() -> None:
    verdict = parse_verdict(
        '{"faithfulness": "3", "hallucination": "yes", "suggestion_groundedness": "4"}'
    )
    assert verdict.faithfulness == 3
    assert verdict.hallucination is True


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        '{"faithfulness": 7, "hallucination": false, "suggestion_groundedness": 5}',
        '{"faithfulness": 0, "hallucination": false, "suggestion_groundedness": 5}',
        '{"faithfulness": true, "hallucination": false, "suggestion_groundedness": 5}',
        '{"faithfulness": 4, "hallucination": "maybe", "suggestion_groundedness": 5}',
        '{"faithfulness": 4, "hallucination": false}',
        "[1, 2, 3]",
    ],
)
def test_a_malformed_verdict_raises_rather_than_defaulting(raw: str) -> None:
    """The single most important assertion about the judge.

    A judge whose unparseable answers become 5s reports a perfect system; one
    whose unparseable answers become 1s reports a broken pipeline. Both are worse
    than a crash, because both look like data.
    """
    with pytest.raises(JudgeError):
        parse_verdict(raw)


def test_the_prompt_shows_the_judge_the_principle_ids() -> None:
    """The rubric asks whether each suggestion applies the principle it cites.
    That question is unanswerable unless both sides carry ids -- the same hole
    that made the entailment prompt's id check unfalsifiable until v2."""
    case = judge_cases.load()[0]
    prompt = build_prompt(judge_cases.to_judge_input(case))
    for pid in {s["principle_id"] for s in case.suggestions}:
        assert f"id: {pid}" in prompt
        assert f"cites {pid!r}" in prompt


def test_the_prompt_does_not_leak_the_answer() -> None:
    """A rater who can see the expected labels is not a rater. Nothing from the
    human label or the case's stated intent may reach the prompt."""
    case = next(c for c in judge_cases.load() if c.human["hallucination"])
    prompt = build_prompt(judge_cases.to_judge_input(case))
    assert case.why not in prompt
    assert "hallucination\": true" not in prompt.lower()
    assert "faithfulness\": 1" not in prompt


def test_ship_gate_matches_section_6() -> None:
    """§6: 'no case with a human-flagged hallucination, average faithfulness
    >= 4/5'. A 4 with no hallucination passes; a 5 with one does not."""
    ok = parse_verdict('{"faithfulness": 4, "hallucination": false, "suggestion_groundedness": 3}')
    flagged = parse_verdict('{"faithfulness": 5, "hallucination": true, "suggestion_groundedness": 5}')
    low = parse_verdict('{"faithfulness": 3, "hallucination": false, "suggestion_groundedness": 5}')
    assert ok.passes_ship_gate
    assert not flagged.passes_ship_gate
    assert not low.passes_ship_gate


def test_aggregate_reports_hallucinations_as_a_rate_not_a_mean() -> None:
    """§6's gate is 'no case with a flagged hallucination', so one bad case has
    to stay visible instead of being averaged into invisibility."""
    verdicts = {
        "a": parse_verdict('{"faithfulness": 5, "hallucination": false, "suggestion_groundedness": 5}'),
        "b": parse_verdict('{"faithfulness": 5, "hallucination": true, "suggestion_groundedness": 5}'),
    }
    agg = aggregate_verdicts(verdicts)
    assert agg["hallucination_free_rate"] == 0.5
    assert agg["hallucinated_cases"] == ["b"]
    assert agg["ship_gate_failures"] == ["b"]


# --- the kappa arithmetic --------------------------------------------------


def test_cohens_kappa_matches_the_hand_computed_value() -> None:
    """observed 3/4 = 0.75; expected (0.5*0.25)+(0.5*0.75) = 0.5;
    (0.75-0.5)/(1-0.5) = 0.5. Same value scikit-learn's
    cohen_kappa_score returns for this input."""
    assert cohens_kappa([True, False, True, False], [True, False, False, False]) == pytest.approx(0.5)


def test_a_constant_judge_scores_zero_not_high() -> None:
    """The whole reason for kappa rather than raw agreement.

    A judge that answers 'no hallucination' unconditionally agrees with the
    human on 9 of 10 cases here -- 90%, which reads as excellent. It detects
    nothing. Kappa says 0.
    """
    human = [False] * 9 + [True]
    lazy = [False] * 10
    raw_agreement = sum(1 for h, j in zip(human, lazy) if h == j) / len(human)
    assert raw_agreement == 0.9
    assert cohens_kappa(human, lazy) == pytest.approx(0.0)


def test_weighted_kappa_forgives_adjacent_grades_and_punishes_distant_ones() -> None:
    """Quadratic weighting is the right choice for a 1-5 rubric: off-by-one is a
    near miss, 1-vs-5 is a total disagreement, and unweighted kappa cannot tell
    them apart."""
    truth = [5, 4, 3, 2, 1, 5, 4, 3]
    off_by_one = [4, 3, 2, 1, 2, 4, 3, 2]
    inverted = [1, 2, 3, 4, 5, 1, 2, 3]
    assert weighted_kappa(truth, truth) == pytest.approx(1.0)
    assert weighted_kappa(truth, off_by_one) > 0.65
    assert weighted_kappa(truth, inverted) < 0.0
    # The sharp version of the same claim: `off_by_one` never agrees exactly
    # with `truth` on a single case, so nominal kappa scores it at or below
    # chance and would have us throw away a rater that is systematically one
    # grade strict. That rater is usable; the inverted one is not.
    assert cohens_kappa(truth, off_by_one) <= 0.0
    assert weighted_kappa(truth, off_by_one) > weighted_kappa(truth, inverted) + 0.9


def test_undefined_kappa_is_reported_not_faked() -> None:
    """Both raters used one identical label: agreement is total and chance
    agreement is also total, so kappa is 0/0. Returning 1.0 would claim a
    calibrated judge on evidence containing no information."""
    with pytest.raises(CalibrationError):
        cohens_kappa([True] * 5, [True] * 5)
    with pytest.raises(CalibrationError):
        weighted_kappa([5] * 5, [5] * 5)


def test_calibrate_refuses_a_rating_outside_the_rubric() -> None:
    with pytest.raises(CalibrationError):
        weighted_kappa([5, 4], [5, 9])


def test_calibrate_uses_only_cases_present_in_both_sets() -> None:
    """A partially-labelled set must produce a smaller honest n, never a silent
    substitution of defaults for the missing half."""
    human = {
        "a": {"faithfulness": 5, "hallucination": False, "suggestion_groundedness": 5},
        "b": {"faithfulness": 1, "hallucination": True, "suggestion_groundedness": 2},
        "c": {"faithfulness": 3, "hallucination": False, "suggestion_groundedness": 3},
    }
    judge = {k: v for k, v in human.items() if k != "c"}
    assert all(r.n == 2 for r in calibrate(human, judge))
    with pytest.raises(CalibrationError):
        calibrate(human, {"z": human["a"]})


def test_confusion_matrix_rows_are_human_and_columns_are_judge() -> None:
    """Which way round this goes decides whether you read 'the judge never
    flags anything' or 'the judge flags everything' -- opposite fixes."""
    labels, matrix = confusion_matrix([True, True, False], [False, True, False])
    i_true, i_false = labels.index(True), labels.index(False)
    assert matrix[i_true][i_false] == 1  # human said yes, judge said no
    assert matrix[i_false][i_true] == 0


def test_interpret_names_the_landis_koch_band() -> None:
    assert interpret(0.85) == "almost perfect"
    assert interpret(0.65) == "substantial"
    assert interpret(-0.1).startswith("none")


# --- the calibration set itself -------------------------------------------


def test_every_calibration_case_validates() -> None:
    assert len(judge_cases.load()) >= 12


def test_calibration_cases_are_rule_clean() -> None:
    """Same load-bearing precondition the entailment set has. A case that trips
    a regex would be rejected by Step C before the judge ever ran, so its label
    would be measuring the rule checks, not the judge."""
    for case in judge_cases.load():
        issues = _rule_based_issues(case.reflection, case.suggestions, judge_cases.VALID_IDS)
        assert not issues, f"{case.case_id} trips a rule check: {issues}"


def test_the_calibration_set_spans_the_middle_of_both_scales() -> None:
    """The reason this set exists separately from eval/adversarial/.

    A set made only of 5s and 1s turns ordinal kappa into a near-binary
    agreement score, and a judge that can only tell perfect from catastrophic
    would pass it while being useless on the 3s and 4s real output produces.
    """
    labels = judge_cases.human_labels(judge_cases.load())
    for field in ("faithfulness", "suggestion_groundedness"):
        seen = {v[field] for v in labels.values()}
        assert {1, 2, 3, 4, 5} <= seen, f"{field} only covers {sorted(seen)}"


def test_the_hallucination_flag_is_not_a_proxy_for_low_faithfulness() -> None:
    """If every hallucinated case were also the lowest-faithfulness case, the
    flag's kappa would just be re-measuring faithfulness. Two cases break the
    correlation deliberately, in both directions."""
    cases = judge_cases.load()
    assert any(c.human["hallucination"] and c.human["faithfulness"] >= 4 for c in cases)
    assert any(not c.human["hallucination"] and c.human["faithfulness"] <= 2 for c in cases)


def test_labels_are_balanced_enough_for_kappa_to_be_defined() -> None:
    flags = [c.human["hallucination"] for c in judge_cases.load()]
    assert 0 < sum(flags) < len(flags)
