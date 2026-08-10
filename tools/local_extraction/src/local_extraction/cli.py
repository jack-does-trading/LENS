from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from tqdm.contrib.logging import logging_redirect_tqdm

from local_extraction.model_client import (
    DEFAULT_GROQ_HOST,
    DEFAULT_GROQ_REQUESTS_PER_MINUTE,
    DEFAULT_OLLAMA_HOST,
    GroqModelClient,
    OllamaModelClient,
)
from local_extraction.pipeline import (
    OutputExistsError,
    PipelineConfig,
    ValidationFailed,
    run_pipeline,
)
from local_extraction.schema import BOOK_TONES

# tools/local_extraction/src/local_extraction/cli.py -> repo root is 4 parents up.
_TOOL_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_PDF = _REPO_ROOT / "books" / "The-Six-Pillars-of-Self-Esteem.pdf"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-extraction",
        description=(
            "Standalone book-extraction tool (architecture §2 Path B). By "
            "default (--provider ollama) it runs entirely against a local "
            "Ollama model; the PDF and all raw model output never leave this "
            "machine. --provider groq is a deliberate, opt-in exception to "
            "that — it sends book chapter text to Groq's hosted API "
            "(architecture §5/§8)."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=("ollama", "groq"),
        default="ollama",
        help="Model backend. 'ollama' (default) is fully local — the PDF and "
        "raw model output never leave this machine. 'groq' sends book "
        "chapter text to Groq's hosted API; only use it if you've deliberately "
        "accepted that tradeoff.",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Model name to use — for --provider ollama, an Ollama model tag "
        "(e.g. llama3.1); for --provider groq, a Groq model slug (e.g. "
        "llama-3.3-70b-versatile). Required, no default — model choice "
        "affects extraction quality and is your call, not this tool's.",
    )
    parser.add_argument(
        "--groq-api-key",
        default=None,
        help="Groq API key, only used with --provider groq. Falls back to the "
        "GROQ_API_KEY environment variable (preferred, so the key doesn't end "
        "up in shell history/process listings).",
    )
    parser.add_argument(
        "--title",
        required=True,
        help="Book title (required — not guessed).",
    )
    parser.add_argument(
        "--author",
        required=True,
        help="Book author (required — not guessed).",
    )
    parser.add_argument(
        "--book-id",
        required=True,
        help="Slug book_id matching architecture §4 shape, e.g. six-pillars-of-self-esteem.",
    )
    parser.add_argument(
        "--tone",
        required=True,
        choices=BOOK_TONES,
        help="Book tone per architecture §4 enum (your call, appears verbatim "
        "in the runtime synthesis prompt).",
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        default=_DEFAULT_PDF,
        help=f"Path to the source PDF (default: {_DEFAULT_PDF}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to write the validated draft JSON "
        "(default: tools/local_extraction/output/<book-id>.json).",
    )
    parser.add_argument(
        "--scratch-dir",
        type=Path,
        default=None,
        help="Directory for raw PDF text / raw per-chunk model output, "
        "gitignored, local debugging only "
        "(default: tools/local_extraction/scratch/<book-id>/).",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Backend host. For --provider ollama: must be localhost "
        f"(default: {DEFAULT_OLLAMA_HOST}). For --provider groq: Groq API base "
        f"URL (default: {DEFAULT_GROQ_HOST}), override only for e.g. a proxy.",
    )
    parser.add_argument(
        "--groq-requests-per-minute",
        type=float,
        default=DEFAULT_GROQ_REQUESTS_PER_MINUTE,
        help="Client-side throttle for --provider groq, to stay under Groq's "
        f"free-tier rate limit (default: {DEFAULT_GROQ_REQUESTS_PER_MINUTE} RPM — "
        "the safe common denominator across the free models; raise it only if "
        "your chosen model's free-tier limit is higher).",
    )
    parser.add_argument("--max-chunk-words", type=int, default=3000)
    parser.add_argument("--overlap-words", type=int, default=150)
    parser.add_argument("--timeout", type=float, default=300.0, help="Per-request timeout, seconds.")
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.2,
        help="Model sampling temperature (default: 0.2 — lower than Ollama's "
        "default for more reliably well-formed JSON).",
    )
    parser.add_argument(
        "--context-length",
        type=int,
        default=8192,
        help="Model context window in tokens, passed as Ollama's num_ctx "
        "(default: 8192, sized to comfortably fit --max-chunk-words plus "
        "prompt overhead). Ollama's own runtime default (2048-4096) can be "
        "smaller than a chunk once tokenized, which silently truncates "
        "input — raise both this and --max-chunk-words together if needed, "
        "but note larger contexts cost meaningfully more RAM.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow overwriting an existing output/<book-id>.json. Off by "
        "default — a previously-written draft may still be under human "
        "review, so a re-run refuses to silently replace it.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # Only sets GROQ_API_KEY (etc.) if not already present in the environment
    # — an exported shell var still wins over .env. Missing tools/local_extraction/.env
    # is not an error; --provider ollama needs nothing from it.
    load_dotenv(_TOOL_DIR / ".env")
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    output_path = args.output or (_TOOL_DIR / "output" / f"{args.book_id}.json")
    scratch_dir = args.scratch_dir or (_TOOL_DIR / "scratch" / args.book_id)

    if not args.pdf.exists():
        print(f"error: PDF not found at {args.pdf}", file=sys.stderr)
        return 1

    config = PipelineConfig(
        pdf_path=args.pdf,
        model=args.model,
        book_id=args.book_id,
        title=args.title,
        author=args.author,
        tone=args.tone,
        output_path=output_path,
        scratch_dir=scratch_dir,
        max_chunk_words=args.max_chunk_words,
        overlap_words=args.overlap_words,
        overwrite_output=args.overwrite,
    )
    if args.provider == "groq":
        api_key = args.groq_api_key or os.environ.get("GROQ_API_KEY")
        if not api_key:
            print(
                "error: --provider groq requires --groq-api-key or a GROQ_API_KEY "
                "environment variable",
                file=sys.stderr,
            )
            return 1
        client = GroqModelClient(
            model=args.model,
            api_key=api_key,
            host=args.host or DEFAULT_GROQ_HOST,
            timeout=args.timeout,
            temperature=args.temperature,
            requests_per_minute=args.groq_requests_per_minute,
        )
    else:
        client = OllamaModelClient(
            model=args.model,
            host=args.host or DEFAULT_OLLAMA_HOST,
            timeout=args.timeout,
            temperature=args.temperature,
            context_length=args.context_length,
        )

    try:
        # Log records emitted while a tqdm bar is active would otherwise
        # print in the middle of the line and push the bar down, making it
        # look like the same bar reprints over and over. logging_redirect_tqdm
        # routes those records through tqdm.write() instead, so the bar stays
        # pinned to the bottom and log lines scroll above it in place.
        with logging_redirect_tqdm():
            run_pipeline(config, client, show_progress=True)
    except OutputExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except ValidationFailed as exc:
        print("REJECTED — aggregated draft failed validation, nothing written to output:", file=sys.stderr)
        for issue in exc.issues:
            print(f"  - {issue}", file=sys.stderr)
        return 2

    print(f"wrote validated draft to {output_path}")
    print(f"raw scratch artifacts (PDF text, per-chunk output) in {scratch_dir}")
    print("This is a DRAFT for human review — nothing has been submitted or published.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
