"""Does the verifier actually stop bad output?

Every other test in this suite asserts that good input produces good output.
These assert the opposite direction -- that seeded bad output is *refused* --
which is the direction that protects a user, and the one Architecture section 6
insists on:

    a verification step that never fails anything is not a verification step

The rule half runs on every push: no LLM, no secrets, no cost. The entailment
half is marked `eval_live` and excluded by default, because it needs a real
provider.
"""

from __future__ import annotations

import pytest

from eval.adversarial.cases import (
    ENTAILMENT_PATH,
    RULES_PATH,
    catch_rate,
    false_positive_rate,
    load,
    run_entailment_cases,
    run_rule_cases,
    summarise,
)

# Architecture section 6's own ship gate.
MIN_CATCH_RATE = 0.90


def test_rule_checks_catch_every_seeded_violation() -> None:
    outcomes = run_rule_cases(load(RULES_PATH))
    caught, seeded = catch_rate(outcomes)
    missed = [o.case.case_id for o in outcomes if o.case.expect == "reject" and not o.correct]
    assert seeded >= 8, "the seeded set has shrunk below a meaningful size"
    assert caught / seeded >= MIN_CATCH_RATE, f"catch rate {caught}/{seeded}; missed {missed}"


def test_rule_checks_do_not_reject_valid_output() -> None:
    """A false positive is not harmless: it burns a synthesis retry, and five
    of them hand the user the fallback template instead of a real answer.
    """
    outcomes = run_rule_cases(load(RULES_PATH))
    wrong, total = false_positive_rate(outcomes)
    culprits = [o.case.case_id for o in outcomes if o.case.expect == "accept" and o.rejected]
    assert total >= 3
    assert wrong == 0, f"valid output wrongly rejected: {culprits}"


def test_boundary_cases_land_on_the_right_side() -> None:
    """The caps are 15 words and 3 suggestions. Off-by-one in either direction
    is either a copyright exposure or a needless fallback.
    """
    by_id = {o.case.case_id: o for o in run_rule_cases(load(RULES_PATH))}
    assert not by_id["accept-fifteen-word-quote"].rejected
    assert by_id["reject-sixteen-word-quote"].rejected
    assert not by_id["accept-exactly-three-suggestions"].rejected
    assert by_id["reject-four-suggestions"].rejected


def test_smart_quotes_cannot_bypass_the_length_cap() -> None:
    """An LLM emits curly quotes routinely; a cap that only sees straight ones
    would be trivially and invisibly evaded.
    """
    by_id = {o.case.case_id: o for o in run_rule_cases(load(RULES_PATH))}
    assert by_id["reject-long-quote-curly-marks"].rejected


def test_every_known_gap_is_declared_rather_than_silently_failing() -> None:
    """`known_gap` exists so an unfixed hole stays visible. If a case marked as
    a gap now passes, the marker is stale and must be removed -- otherwise the
    set slowly fills with cases that look tracked but assert nothing.
    """
    for outcome in run_rule_cases(load(RULES_PATH)):
        if outcome.case.known_gap:
            assert not outcome.correct, (
                f"{outcome.case.case_id} is marked known_gap but now passes; drop the marker"
            )


def test_entailment_cases_are_rule_clean() -> None:
    """The load-bearing precondition for the entailment half.

    `_rule_based_issues` short-circuits, so a semantically-ungrounded case that
    also trips a regex never reaches the LLM and proves nothing about the
    judge. This test is what keeps that half honest, and it needs no LLM.
    """
    for outcome in run_rule_cases(load(ENTAILMENT_PATH)):
        assert not outcome.rejected, (
            f"{outcome.case.case_id} trips a rule check ({outcome.issues}), so it would never "
            "reach the entailment judge -- rewrite it to be rule-clean"
        )


@pytest.mark.eval_live
def test_entailment_judge_catches_semantic_hallucination() -> None:
    """Hits a real provider. Run with: pytest -m eval_live"""
    from scripts.label_eval_cases import _build_client  # local import: needs no key otherwise

    outcomes = run_entailment_cases(_build_client(), load(ENTAILMENT_PATH))
    stats = summarise(outcomes)
    assert not stats["short_circuited"], f"rule checks pre-empted {stats['short_circuited']}"
    assert stats["catch_rate"] >= MIN_CATCH_RATE, stats
    assert stats["false_positives"] == 0, [
        o.case.case_id for o in outcomes if o.case.expect == "accept" and not o.correct
    ]
