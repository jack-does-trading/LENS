#!/usr/bin/env python3
"""Render the whole quality report as Markdown, for CI to publish.

    python scripts/eval_report.py --database-url postgresql://lens:lens@localhost:5432/lens \
        --out eval-report.md

Three sections, in this order and deliberately not a different one:

  1. **Judge calibration** -- Cohen's kappa against the human labels. First,
     because it is what licenses the numbers below it. A faithfulness score
     quoted above an uncalibrated judge is not a measurement.
  2. **Retrieval** -- recall@k, precision@k, MRR and hit rate on the golden set.
  3. **Verifier** -- catch rate and false-positive rate on the seeded bad
     outputs. Architecture section 6: "a verification step that never fails
     anything is not a verification step."

Needs an EMPTY, DISPOSABLE database: it migrates the schema and seeds the
recorded corpus into it. Refuses a non-local host for the same reason
tests/conftest.py does -- this writes 1213 principles into whatever it is
pointed at. Override with LENS_ALLOW_REMOTE_TEST_DB=1 if you genuinely mean it.

Reads only committed cassettes, so it needs no API key and costs nothing.
Exits 1 on any threshold breach, so it is a gate as well as a report.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, func, make_url, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.models import Book  # noqa: E402
from eval import judge_cases  # noqa: E402
from eval.adversarial import cases as adversarial  # noqa: E402
from eval.calibration import calibrate  # noqa: E402
from eval.calibration import report as calibration_report  # noqa: E402
from eval.cassettes import CassetteEmbeddingClient, CassetteJudgeClient, load_corpus  # noqa: E402
from eval.harness import load_thresholds, report, run_golden_set, seed_corpus  # noqa: E402
from eval.judge import PROMPT_VERSION as JUDGE_PROMPT_VERSION  # noqa: E402
from eval.judge import judge_output  # noqa: E402
from eval.metrics import aggregate  # noqa: E402

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "db"}


def calibration_section(thresholds: dict) -> tuple[str, list[str]]:
    """Replay the recorded judge verdicts and score them against the human labels.

    No database and no network: the verdicts came from real Groq once (see
    scripts/calibrate_judge.py --record) and are keyed by a hash of the whole
    prompt, so a rubric edit invalidates them and this raises rather than
    reporting a kappa for a question the judge was never asked.
    """
    floor = thresholds["judge"]["human_kappa_floor"]
    cases = judge_cases.load()
    client = CassetteJudgeClient(settings.groq_model)
    judged = {
        c.case_id: judge_output(client, judge_cases.to_judge_input(c)).as_labels() for c in cases
    }
    results = calibrate(judge_cases.human_labels(cases), judged)
    breaches = [f"judge_kappa_{r.field}" for r in results if r.kappa is not None and r.kappa < floor]
    body = calibration_report(results, kappa_floor=floor)
    body += (
        f"\n\nRubric `{JUDGE_PROMPT_VERSION}`, judge `{settings.groq_model}`, replayed from "
        "`eval/cassettes/judge_verdicts.json`. Raw agreement is shown next to κ on purpose: "
        "quadratic weighting forgives an off-by-one grade almost entirely, so a high ordinal κ "
        "next to a middling raw agreement means a judge that is consistently one grade strict "
        "rather than one that agrees case by case."
    )
    return body, breaches


def verifier_section(thresholds: dict) -> tuple[str, list[str]]:
    """The rule half of the adversarial set. Deterministic, no LLM."""
    t = thresholds["verifier"]
    outcomes = adversarial.run_rule_cases(adversarial.load(adversarial.RULES_PATH))
    caught, seeded = adversarial.catch_rate(outcomes)
    wrong, clean = adversarial.false_positive_rate(outcomes)
    catch = caught / seeded if seeded else 0.0
    fpr = wrong / clean if clean else 0.0
    gaps = [o.case.case_id for o in outcomes if o.case.known_gap]
    rows = [
        ("catch rate", f"{caught}/{seeded}", catch, t["catch_rate_floor"], catch >= t["catch_rate_floor"]),
        (
            "false-positive rate",
            f"{wrong}/{clean}",
            fpr,
            t["false_positive_rate_max"],
            fpr <= t["false_positive_rate_max"],
        ),
    ]
    lines = [
        "### Verifier — seeded bad outputs (rule half)",
        "",
        "| metric | count | value | threshold | |",
        "|---|---|---|---|---|",
    ]
    for name, count, value, threshold, ok in rows:
        lines.append(f"| {name} | {count} | {value:.2f} | {threshold:.2f} | {'✅' if ok else '❌'} |")
    if gaps:
        lines += ["", f"Declared known gaps (asserted to still fail, not silently dropped): {', '.join(gaps)}."]
    lines += [
        "",
        "The entailment half needs a live provider and runs in the nightly job "
        "(`pytest -m eval_live`), not here.",
    ]
    breaches = [name.replace(" ", "_").replace("-", "_") for name, _, _, _, ok in rows if not ok]
    return "\n".join(lines), breaches


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database-url", required=True)
    ap.add_argument("--out", default="eval-report.md")
    args = ap.parse_args()

    host = make_url(args.database_url).host or ""
    if host not in LOCAL_HOSTS and os.environ.get("LENS_ALLOW_REMOTE_TEST_DB") != "1":
        print(f"refusing to seed a non-local database host {host!r}", file=sys.stderr)
        return 2

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.cmd_opts = SimpleNamespace(x=[f"db_url={args.database_url}"])
    command.upgrade(cfg, "head")

    top_k = load_thresholds()["retrieval"]["top_k"]
    engine = create_engine(args.database_url)
    try:
        with Session(engine) as db:
            # Refuse a database that already holds a corpus, rather than letting
            # seed_corpus die on a books_pkey violation. Same run twice is the
            # obvious thing to try by hand, and the raw IntegrityError buries
            # "this database is not empty" under a hundred lines of SQLAlchemy.
            existing = db.scalar(select(func.count()).select_from(Book))
            if existing:
                print(
                    f'{args.database_url.rsplit("/", 1)[-1]} already contains {existing} books. '
                    'This script seeds a corpus and needs an empty database: drop and recreate it, '
                    'or point --database-url somewhere disposable.',
                    file=sys.stderr,
                )
                return 2
            seed_corpus(db, load_corpus())
            db.commit()
            scores = run_golden_set(db, top_k=top_k)
    finally:
        engine.dispose()

    thresholds = load_thresholds()
    calibration_md, calibration_breaches = calibration_section(thresholds)
    verifier_md, verifier_breaches = verifier_section(thresholds)

    markdown = "\n\n---\n\n".join([calibration_md, report(scores, top_k), verifier_md])
    Path(args.out).write_text(markdown + "\n")
    print(markdown)

    agg = aggregate(scores)
    t = thresholds["retrieval"]
    breaches = calibration_breaches + verifier_breaches + [
        name
        for name, value, floor in (
            ("recall", agg["recall_at_k"], t["recall_at_k_floor"]),
            ("precision", agg["precision_at_k"], t["precision_at_k_floor"]),
            ("mrr", agg["mrr"], t["mrr_floor"]),
            ("hit_rate", agg["hit_rate"], t["hit_rate_floor"]),
        )
        if value < floor
    ]
    if breaches:
        print(f"\nTHRESHOLD BREACH: {', '.join(breaches)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
