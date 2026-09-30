"""Evaluation harness for the grounded-advice pipeline.

Deliberately a sibling of `app/`, not a subpackage: nothing here is imported by
the running service, and nothing here may be installed into production (see
`requirements-eval.txt`). `docs/Architecture.md` section 6 is the spec.
"""
