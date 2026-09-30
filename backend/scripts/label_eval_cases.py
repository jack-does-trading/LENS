#!/usr/bin/env python3
"""Label staged eval cases by hand: which principles *should* have surfaced?

    python scripts/label_eval_cases.py --database-url postgresql://...

This is the step that makes every downstream number mean something, and it is
deliberately not automatable. A golden set labelled by the same class of system
being measured grades that system against itself.

Two design choices worth knowing about:

  * What the system actually retrieved is hidden behind `/sys`. Showing it up
    front would anchor you to confirming the current behaviour, which is the
    fastest way to build a golden set that can never fail.
  * Nothing reaches the committed file without an explicit `/keep`. Staged
    cases contain real journal text; `/drop` and `/redact` exist so a case
    written by someone other than you never has to be committed verbatim.

Assisted mode (`--assist`) proposes a shortlist for each case with one LLM
pass over every principle in the book, and you accept or reject each one. It
never uses tag matching or embeddings, so it does not share a mechanism with
the retriever it is helping evaluate. Hand-label a few cases without it first
and run scripts/compare_labels.py -- until that agreement check exists, an
assisted golden set is provisional.

Commands:
  /propose     ask for a shortlist and review it one by one (needs --assist)
  /s <query>   search this book's principles by name, summary or tag
  /a <id>      add a principle to the expected set   /r <id>  remove one
  /list        show the expected set so far
  /sys         reveal what the pipeline actually returned (anchoring warning)
  /note <txt>  record why you chose these
  /redact      replace the entry text with your own paraphrase
  /keep        save to eval/cases/golden.jsonl and move on
  /drop        discard this case entirely (privacy)
  /skip        leave it staged, decide later
  /q           save progress and quit
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session, defer  # noqa: E402

from app.config import settings  # noqa: E402
from app.llm import GroqLLMClient, LLMClient, OllamaLLMClient  # noqa: E402
from app.models import Book, Principle, ReviewStatus  # noqa: E402
from eval.propose import ProposalError, format_situation, propose_candidates  # noqa: E402
from scripts.retag_principles import _engine  # noqa: E402
from eval.cases import (  # noqa: E402
    GOLDEN_PATH,
    STAGING_PATH,
    EvalCase,
    load_cases,
    save_cases,
)

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def _print_case(case: EvalCase, book: Book | None) -> None:
    title = book.title if book else case.book_id
    print(f"\n{BOLD}{case.case_id}{RESET}  ({title})")
    print(f"  mood: {case.mood if case.mood is not None else '-'}/5")
    for e in case.entries:
        print(f"  - {e.get('time', '--:--')}  {e.get('action', '')}  {DIM}[{e.get('category','')}]{RESET}")


def _search(principles: list[Principle], query: str) -> list[Principle]:
    """Filter in memory rather than issuing a query.

    The interactive loop must not touch the database -- see _load_case_context.
    """
    q = query.strip().lower()
    return [
        p
        for p in principles
        if q in p.name.lower()
        or q in p.summary.lower()
        or any(q in t.lower() for t in p.applies_to_tags)
    ]


def _show(principles: list[Principle], limit: int = 12) -> None:
    if not principles:
        print("  (no matches)")
        return
    for p in principles[:limit]:
        tags = ", ".join(p.applies_to_tags)
        print(f"  {BOLD}{p.principle_id}{RESET}  {p.name}")
        print(f"      {p.summary[:110]}{'...' if len(p.summary) > 110 else ''}")
        print(f"      {DIM}tags: {tags}{RESET}")
    if len(principles) > limit:
        print(f"  {DIM}... {len(principles) - limit} more; narrow the search{RESET}")


def _build_client(provider: str | None = None) -> LLMClient:
    """Pick a provider for the proposer.

    Unlike app.routers.analyses.get_llm_client, this prefers Groq whenever a
    key exists rather than obeying LLM_PROVIDER. That setting exists so a
    hosted deployment can't silently route a user's journal entries to a
    different provider than intended; this is an offline authoring tool run by
    hand, and defaulting to an Ollama instance that probably isn't running just
    produces a confusing connection error.
    """
    provider = provider or ("groq" if settings.groq_api_key else "ollama")
    if provider == "groq":
        if not settings.groq_api_key:
            raise SystemExit("--assist with --provider groq needs GROQ_API_KEY in backend/.env")
        print(f"{DIM}proposer: groq {settings.groq_model}{RESET}")
        return GroqLLMClient(model=settings.groq_model, api_key=settings.groq_api_key)
    print(f"{DIM}proposer: ollama {settings.ollama_model}{RESET}")
    return OllamaLLMClient(host=settings.ollama_host, model=settings.ollama_model)


def _review_candidates(
    client: LLMClient,
    case: EvalCase,
    principles: list[Principle],
    expected: list[str],
) -> None:
    """Propose a shortlist and walk it. Mutates `expected` in place."""
    by_id = {p.principle_id: p for p in principles}
    print(f"  {DIM}reading all {len(principles)} principles in this book...{RESET}")
    try:
        candidates = propose_candidates(
            client, format_situation(case.entries, case.mood), principles
        )
    except ProposalError as exc:
        print(f"  proposal failed ({exc}) -- fall back to /s search")
        return

    print(f"  {len(candidates)} candidates. y = relevant, n = not, q = stop reviewing\n")
    for i, cand in enumerate(candidates, start=1):
        p = by_id[cand.principle_id]
        if cand.principle_id in expected:
            continue
        print(f"  [{i}/{len(candidates)}] {BOLD}{p.name}{RESET}")
        print(f"      {p.summary[:140]}{'...' if len(p.summary) > 140 else ''}")
        print(f"      {DIM}proposed because: {cand.reason}{RESET}")
        answer = input("      relevant? [y/N/q] ").strip().lower()
        if answer == "q":
            break
        if answer == "y":
            expected.append(cand.principle_id)
            print(f"      + added ({len(expected)} expected)")
    print(f"\n  expected so far: {', '.join(expected) or '(none)'}")


def _load_case_context(engine, book_id: str) -> tuple[Book | None, list[Principle]]:
    """Read everything one case needs, then hand back the connection.

    The labelling loop blocks on input() for minutes per case. Holding a
    Session open across that leaves an idle TCP connection to Supabase, which
    the pooler eventually drops; the failure then surfaces on the *next* case
    as "SSL SYSCALL error: Can't assign requested address", far from its cause.
    So: open, read eagerly, expunge, close, and do the interactive part against
    plain detached objects.

    defer(embedding) because nothing here reads the vector, and without it this
    drags ~20KB per principle (a 1024-dim vector serialized as text) over the
    wire for every case.
    """
    with Session(engine) as db:
        book = db.get(Book, book_id)
        principles = list(
            db.scalars(
                select(Principle)
                .options(defer(Principle.embedding))
                .where(
                    Principle.book_id == book_id,
                    Principle.review_status == ReviewStatus.human_reviewed,
                )
            ).all()
        )
        # Detach while the session is still open: the instances keep the
        # attributes already loaded, and nothing can lazily re-query later.
        db.expunge_all()
    return book, principles


def label(
    database_url: str, labeller: str, assist: bool = False, provider: str | None = None
) -> None:
    staging = load_cases(STAGING_PATH)
    golden = load_cases(GOLDEN_PATH)
    if not staging:
        print(f"nothing staged in {STAGING_PATH}. Run scripts/export_eval_cases.py first.")
        return

    llm = _build_client(provider) if assist else None
    engine = _engine(database_url)
    skipped: list[EvalCase] = []
    idx = 0

    def persist(pending: list[EvalCase]) -> None:
        """Write both files after every decision.

        /keep used to only append to an in-memory list, with the real write
        deferred to _finish(). Any crash between the first /keep and the last
        case silently threw away every label made in that run, while still
        printing "kept -> golden.jsonl". Labelling is expensive human work; it
        gets flushed to disk the moment it is produced.
        """
        save_cases(GOLDEN_PATH, golden)
        save_cases(STAGING_PATH, skipped + pending)

    try:
        for idx, case in enumerate(staging, start=1):
            book, book_principles = _load_case_context(engine, case.book_id)
            print(f"\n{'=' * 72}\n[{idx}/{len(staging)}]")
            _print_case(case, book)
            expected = list(case.expected_principle_ids)
            notes = case.label_notes
            revealed = False
            if llm is not None:
                _review_candidates(llm, case, list(book_principles), expected)

            while True:
                try:
                    raw = input(f"\n{BOLD}>{RESET} ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\ninterrupted -- saving progress")
                    raise SystemExit(_finish(skipped + staging[idx - 1 :], golden))

                if raw == "/propose":
                    if llm is None:
                        print("  re-run with --assist to use this")
                    else:
                        _review_candidates(llm, case, list(book_principles), expected)
                elif raw.startswith("/s "):
                    _show(_search(book_principles, raw[3:]))
                elif raw.startswith("/a "):
                    pid = raw[3:].strip()
                    if pid not in {p.principle_id for p in book_principles}:
                        print(f"  no such principle_id in this book: {pid!r}")
                    elif pid in expected:
                        print("  already in the expected set")
                    else:
                        expected.append(pid)
                        print(f"  + {pid}  ({len(expected)} expected)")
                elif raw.startswith("/r "):
                    pid = raw[3:].strip()
                    if pid in expected:
                        expected.remove(pid)
                        print(f"  - {pid}")
                elif raw == "/list":
                    print("  expected:", ", ".join(expected) or "(none yet)")
                elif raw == "/sys":
                    revealed = True
                    print(
                        f"  {DIM}system returned: "
                        f"{', '.join(case.retrieved_at_export) or '(nothing)'}"
                    )
                    print(f"  this is the thing under test, not the answer key{RESET}")
                elif raw.startswith("/note "):
                    notes = raw[6:].strip()
                    print("  noted")
                elif raw == "/redact":
                    case.entries = [
                        {**e, "action": input(f"  paraphrase {e.get('action','')!r}: ").strip()}
                        for e in case.entries
                    ]
                    case.redaction = "redacted"
                    print("  redacted")
                elif raw == "/keep":
                    if not expected:
                        print("  refusing to keep a case with an empty expected set")
                        continue
                    case.expected_principle_ids = expected
                    case.label_notes = (
                        notes
                        + (" [saw system output]" if revealed else "")
                        + (" [llm-assisted shortlist]" if llm is not None else "")
                    )
                    case.labelled_by = labeller
                    case.labelled_at = date.today().isoformat()
                    case.validate()
                    golden.append(case)
                    persist(staging[idx:])
                    print(f"  kept -> {GOLDEN_PATH.name} ({len(golden)} labelled, saved)")
                    break
                elif raw == "/drop":
                    persist(staging[idx:])
                    print("  dropped")
                    break
                elif raw == "/skip":
                    skipped.append(case)
                    persist(staging[idx:])
                    break
                elif raw == "/q":
                    raise SystemExit(_finish(skipped + staging[idx - 1 :], golden))
                else:
                    print(__doc__.split("Commands:")[1])
    except SystemExit:
        raise
    except BaseException:
        # Anything else -- a dropped connection, a bug -- must not cost the
        # labels made so far. The current case was never decided, so it goes
        # back to staging and can be redone.
        persist(staging[idx - 1 :] if idx else staging)
        print(f"\n{len(golden)} labelled case(s) saved to {GOLDEN_PATH} before the error below.")
        raise
    finally:
        engine.dispose()

    _finish(skipped, golden)


def _finish(remaining: list[EvalCase], golden: list[EvalCase]) -> int:
    save_cases(STAGING_PATH, remaining)
    save_cases(GOLDEN_PATH, golden)
    print(f"\n{len(golden)} labelled case(s) in {GOLDEN_PATH}")
    print(f"{len(remaining)} still staged")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--labeller", default="bhavyadeep")
    parser.add_argument(
        "--assist",
        action="store_true",
        help="propose a shortlist per case with an LLM; you still accept/reject each one",
    )
    parser.add_argument("--provider", choices=["groq", "ollama"], default=None)
    args = parser.parse_args()
    label(args.database_url, args.labeller, assist=args.assist, provider=args.provider)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
