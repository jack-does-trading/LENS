#!/usr/bin/env python3
"""Score the faithfulness judge against the human labels, and record its answers.

    # once, locally, with GROQ_API_KEY set -- costs 16 LLM calls
    .venv/bin/python scripts/calibrate_judge.py --record

    # thereafter, and in CI -- no key, no network, no cost
    .venv/bin/python scripts/calibrate_judge.py

The order matters and is the whole point of the module it exercises: nothing
downstream is allowed to quote a faithfulness score until this prints a kappa
above the floor in eval/thresholds.json. A judge is an instrument, and an
uncalibrated instrument produces readings, not measurements.

Exits 1 on a kappa below the floor, so it works as a gate as well as a report.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.llm import GroqLLMClient, LLMClient, OllamaLLMClient  # noqa: E402
from eval import judge_cases  # noqa: E402
from eval.calibration import calibrate, confusion_matrix, format_confusion, report  # noqa: E402
from eval.cassettes import (  # noqa: E402
    CassetteJudgeClient,
    judge_key,
    load_judge_verdicts,
    save_judge_verdicts,
)
from eval.harness import load_thresholds  # noqa: E402
from eval.judge import PROMPT_VERSION, JudgeError, build_prompt, judge_output, parse_verdict  # noqa: E402


class _RecordingClient:
    """Delegates to a real provider and keeps every completion, keyed by prompt.

    Flush-on-produce, like the labeller and the query-vector cache: each verdict
    is written the moment it arrives rather than at the end of the loop. Paid API
    calls that only persist if the whole run succeeds are a bug this project has
    already paid for twice.
    """

    def __init__(self, inner: LLMClient, model: str) -> None:
        self._inner = inner
        self._model = model
        self.verdicts = load_judge_verdicts()

    def generate(self, prompt: str, *, timeout: float | None = None) -> str:
        raw = self._inner.generate(prompt, timeout=timeout)
        self.verdicts[judge_key(prompt, self._model)] = {
            "raw": raw,
            "prompt_version": PROMPT_VERSION,
            "model": self._model,
        }
        save_judge_verdicts(self.verdicts)
        return raw


def _live_client(provider: str | None) -> tuple[LLMClient, str]:
    provider = provider or ("groq" if settings.groq_api_key else "ollama")
    if provider == "groq":
        if not settings.groq_api_key:
            raise SystemExit("--record with --provider groq needs GROQ_API_KEY in backend/.env")
        return GroqLLMClient(model=settings.groq_model, api_key=settings.groq_api_key), settings.groq_model
    return OllamaLLMClient(host=settings.ollama_host, model=settings.ollama_model), settings.ollama_model


def run(client: LLMClient, cases: list[judge_cases.JudgeCase], *, verbose: bool) -> dict[str, dict]:
    labels: dict[str, dict] = {}
    for case in cases:
        verdict = judge_output(client, judge_cases.to_judge_input(case))
        labels[case.case_id] = verdict.as_labels()
        if verbose:
            human = case.human
            flags = "".join(
                " " if human[f] == verdict.as_labels()[f] else "*"
                for f in ("faithfulness", "hallucination", "suggestion_groundedness")
            )
            print(
                f"  {case.case_id:44} human f{human['faithfulness']} "
                f"h{int(human['hallucination'])} g{human['suggestion_groundedness']}  "
                f"judge f{verdict.faithfulness} h{int(verdict.hallucination)} "
                f"g{verdict.suggestion_groundedness}  [{flags}]  {verdict.reason[:60]}"
            )
    return labels


def replay(cases: list[judge_cases.JudgeCase], model: str, *, verbose: bool) -> dict[str, dict]:
    return run(CassetteJudgeClient(model), cases, verbose=verbose)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--record",
        action="store_true",
        help="call the real provider and write eval/cassettes/judge_verdicts.json",
    )
    parser.add_argument("--provider", choices=["groq", "ollama"], default=None)
    parser.add_argument("--model", default=None, help="override the cassette's model key on replay")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    cases = judge_cases.load()
    thresholds = load_thresholds()["judge"]
    floor = thresholds["human_kappa_floor"]

    if args.record:
        inner, model = _live_client(args.provider)
        print(f"recording {len(cases)} judge verdicts from {model} ({PROMPT_VERSION})")
        judge_labels = run(_RecordingClient(inner, model), cases, verbose=not args.quiet)
    else:
        model = args.model or settings.groq_model
        print(f"replaying recorded verdicts for {model} ({PROMPT_VERSION})")
        judge_labels = replay(cases, model, verbose=not args.quiet)

    human = judge_cases.human_labels(cases)
    results = calibrate(human, judge_labels)
    print()
    print(report(results, kappa_floor=floor))

    for field in ("faithfulness", "hallucination", "suggestion_groundedness"):
        ids = sorted(set(human) & set(judge_labels))
        labels, matrix = confusion_matrix(
            [human[c][field] for c in ids], [judge_labels[c][field] for c in ids]
        )
        print(f"\n{field}:")
        print(format_confusion(labels, matrix))

    print("\ndisagreements:")
    for cid in sorted(set(human) & set(judge_labels)):
        diffs = [f for f in ("faithfulness", "hallucination", "suggestion_groundedness")
                 if human[cid][f] != judge_labels[cid][f]]
        if diffs:
            print(f"  {cid:44} {', '.join(f'{f}: {human[cid][f]} vs {judge_labels[cid][f]}' for f in diffs)}")

    breaches = [r.field for r in results if r.kappa is not None and r.kappa < floor]
    if breaches:
        print(f"\nKAPPA BELOW FLOOR ({floor:.2f}): {', '.join(breaches)}")
        print("Fix the judge prompt before quoting any score it produces.")
        return 1
    print(f"\nall rubric fields at or above the kappa floor of {floor:.2f}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except JudgeError as exc:
        raise SystemExit(f"judge error: {exc}")
