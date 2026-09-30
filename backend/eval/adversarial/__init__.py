"""Seeded bad outputs for Step C.

`docs/Architecture.md` section 6 is blunt about why this exists:

    a verification step that never fails anything is not a verification step

Everything else in the harness measures whether good output is produced. This
measures whether bad output is *stopped*, which is the direction that actually
protects a user -- and the direction nothing in the 149-test suite currently
covers, because every existing verification test asserts on one rule at a time
rather than on the verifier's overall catch rate.

Split in two on purpose. `_rule_based_issues` short-circuits: if any rule
fires, `verify_analysis` returns before the entailment LLM call happens. So a
semantically-ungrounded case that also trips a rule proves nothing about the
entailment check -- it would have been caught by a regex. The cases in
`entailment.jsonl` are therefore required to be rule-clean, and a test asserts
that they are.
"""
