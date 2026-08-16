# Local Extraction Tool

Standalone CLI implementing architecture `docs/Architecture.md` §2 Path B
("local-model extraction") and §8 (addendum). **This tool is not part of the
Lens web app.** It is never imported by `backend/`, has its own virtualenv,
and its only connection to the rest of the system is the (still-manual)
Draft Extraction submission step described below — nothing here talks to the
app's database, and nothing here is triggered by the app.

It runs a book PDF through a model and produces a single JSON draft matching
the `books`/`principles` schema shape, for you to read and review before
anything is submitted anywhere. Two backends are supported:

- **`--provider ollama` (default):** a local model via [Ollama](https://ollama.com)
  on your own machine. **What never leaves this machine on this path:** the
  PDF, all per-page/per-chunk raw text, and all raw per-chunk/aggregation
  model output. Only the final, validated JSON draft is meant to be looked at
  outside this directory.
- **`--provider groq`:** a **deliberate, opt-in exception** to the above —
  sends book chapter text to [Groq](https://groq.com)'s hosted API for every
  extraction and aggregation call, in exchange for a materially better free
  model than what typically fits on a personal machine (see "Running it with
  Groq" below). Only use this if you've consciously accepted that tradeoff;
  architecture §5/§8 explains why local-only is the default. `--provider
  ollama` remains fully local either way.

Either way, the final JSON draft still requires your manual review and
manual submission; this tool does not submit or publish anything on its own
(see "What this tool does NOT do" below).

## Prerequisites — checked, not assumed

As of this tool's initial build, on this machine:

| Prerequisite | Status |
|---|---|
| [Ollama](https://ollama.com) installed | ❌ **Not installed** |
| Ollama serving on `localhost:11434` | ❌ **Not running** |
| A model pulled (e.g. `ollama pull llama3.1`) | ❌ **N/A — no runtime yet** |
| PyMuPDF (PDF parsing) | ✅ Installed in this tool's own `.venv` |

**Before the real smoke test (below) can run, you need to:**

```bash
brew install ollama          # or download from https://ollama.com
ollama serve                 # starts the local server on :11434
ollama pull <model-name>     # e.g. `ollama pull llama3.1` — YOUR choice of model
```

Which model to use is deliberately not decided for you here: model choice
materially affects extraction quality/hallucination rate, and
`docs/Architecture.md` §7 explicitly lists "which local model to standardize
on for extraction" as a decision for you, not an engineering default. The
`--model` flag has no default value anywhere in this tool — you must pass it
every time.

All unit/mock tests below run today, with none of the above installed —
they never touch Ollama or the network.

## Setup

```bash
cd tools/local_extraction
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install pytest  # dev-only, not a runtime dependency of the tool itself
```

## Pipeline

1. **Parse** the PDF locally (PyMuPDF). Tries, in order:
   - Embedded PDF TOC (bookmarks) — most reliable, publisher-provided chapter boundaries.
   - Regex-detected "Chapter N" / "Part N" headings in the page text, if the TOC is missing or too sparse (fewer than 2 usable entries).
   - Fixed-size overlapping word windows (default 1500 words, 150-word overlap) as the last resort.
   Whichever strategy is used is logged (`--verbose`) along with why, and recorded in the output's `extraction_metadata`.
2. **Per-chunk pass:** one local-model call per chunk, under the same
   constraints as the runtime synthesis prompt (architecture §3) — paraphrase
   only, ≤15-word quotes, ≤1 quote per candidate principle.
3. **Aggregation pass:** one more local-model call merges/dedupes all
   per-chunk candidates into a single `core_thesis` + consolidated
   `principles` list.
4. **Validation** (rule-based, no LLM): `core_thesis` ≤150 words, every
   `principle.summary` ≤200 words, no quoted span >15 words, no principle
   with >1 quote. **On any failure, the draft is rejected outright and
   nothing is written to `output/`** — this tool never truncates or silently
   "fixes" a model's output to make it pass.
5. **Output:** on success, writes *only* the validated JSON to
   `output/<book-id>.json`, tagged `extraction_method: "local_model"`,
   `review_status: "pending_review"`. Raw PDF text and raw per-chunk/
   aggregation model output go to `scratch/<book-id>/` — gitignored, for your
   own debugging only, never read by anything else in this tool or referenced
   from the output file.

## What this tool does NOT do

- With `--provider ollama` (default): does not call any host other than
  `localhost`/`127.0.0.1` (enforced in `OllamaModelClient`, which refuses to
  construct against any other host — see `tests/test_no_network.py` for the
  grep-based check across all source). `--provider groq` is the one
  documented exception to this, opt-in only — see above.
- Does not submit the draft into the app's Draft Extraction queue. That's a
  separate, manual step you trigger yourself after reading `output/*.json` —
  matching architecture §2's requirement that the human review gate for
  `extraction_method: local_model` drafts is a full read-through by a human
  who hasn't otherwise read the source material, not a rubber stamp.
- Does not publish to the catalogue, generate embeddings, or touch the
  Postgres database in any way.

## Running it

### Real run (manual smoke test — not part of the automated test suite)

Requires Ollama running with a model pulled (see Prerequisites above):

```bash
cd tools/local_extraction
source .venv/bin/activate
local-extraction \
  --model llama3.1 \
  --title "The Six Pillars of Self-Esteem" \
  --author "Nathaniel Branden" \
  --book-id six-pillars-of-self-esteem \
  --tone scientific \
  --verbose
```

(`--tone` must be one of `pragmatic | philosophical | spiritual | scientific
| narrative` per architecture §4 — pick whichever actually fits; `scientific`
above is just an example, not a recommendation baked into the tool.)

The CLI shows a live progress bar across all chunks (`tqdm`), plus a note
when the aggregation pass starts/finishes. `--verbose` additionally logs
which chunking strategy was used and why, and any JSON-parse retries.

**Hardware note (measured on an 8GB M1 Mac):** the model server's memory
footprint scales with the requested context window — roughly 2.9GB RSS at
the default `--context-length 8192`, versus 4.3-4.6GB at 32768. Defaults
(`--max-chunk-words 3000`, `--context-length 8192`) were chosen to leave
headroom on constrained machines; the aggregation pass picks its own larger
context automatically if the candidate volume needs it (see
`aggregation.select_context_length`). Raise `--max-chunk-words`/
`--context-length` together only if you have RAM to spare.

**Sleep/power note:** this all runs as a local process — if the machine goes
to full system sleep (e.g. closing the lid with no external display), the
whole pipeline pauses along with everything else, and the in-flight request
will likely time out on wake once the timeout window (`--timeout`, default
300s) is exceeded by the sleep duration. Keep the machine awake for the
duration of a real run.

`--pdf` defaults to `books/The-Six-Pillars-of-Self-Esteem.pdf` at the repo
root; override with `--pdf /path/to/other.pdf` for a different book.

On success this writes `output/six-pillars-of-self-esteem.json` and prints
its path. On validation failure, it prints every violation to stderr and
writes nothing.

### Running it with Groq (opt-in, sends book text off this machine)

Requires a free [Groq](https://console.groq.com) API key.

Rather than exporting `GROQ_API_KEY` in your shell every session, put it in
`tools/local_extraction/.env` (git-ignored — see `.env.example`) and the CLI
picks it up automatically on every run:

```bash
cd tools/local_extraction
cp .env.example .env
# edit .env, set GROQ_API_KEY=gsk_...
source .venv/bin/activate
local-extraction \
  --provider groq \
  --model openai/gpt-oss-120b \
  --title "12 Rules for Life" \
  --author "Jordan B. Peterson" \
  --book-id 12-rules-for-life \
  --tone philosophical \
  --pdf "../../books/12 Rules for Life_ An Antidote to Chaos ( PDFDrive ).pdf" \
  --verbose
```

Notes specific to this path:

- **`.env` is loaded automatically** from `tools/local_extraction/.env`
  (via `python-dotenv`) regardless of your current directory — an
  already-exported `GROQ_API_KEY` in your shell still takes precedence over
  it. A missing `.env` is not an error; `--provider ollama` needs nothing
  from it. `--groq-api-key` still works as a one-off override if you'd
  rather not use either.
- **Model deprecation, 2026-08-16:** Groq decommissioned `llama-3.3-70b-versatile`
  and `llama-3.1-8b-instant` on this date — requests to either now fail
  outright. `openai/gpt-oss-120b` (used above) is Groq's recommended
  same-class replacement for the 70B model; `openai/gpt-oss-20b` replaces
  the 8B one. If a run suddenly starts failing with a model-not-found-style
  error, check Groq's [deprecations page](https://console.groq.com/docs/deprecations)
  before assuming it's this tool's bug.
- **Free-tier limits** (as of this writing): check Groq's own docs for
  current per-model rate limits before relying on any specific numbers here —
  free-tier terms and limits change independently of this file. The client
  throttles itself to `--groq-requests-per-minute` (default 30) and retries
  on HTTP 429 honoring `Retry-After`, up to 5 attempts; a 429 that persists
  past that (e.g. a daily cap, not just the per-minute one) is raised rather
  than retried forever.
- **No `--context-length` knob** — Groq manages context server-side per
  model; the flag only applies to `--provider ollama`.
- **No sleep/power note applies** — there's no local model server to pause;
  ordinary network hiccups are handled by `--timeout` (default 300s) same as
  the Ollama path.
- Other Groq models as of this writing: `openai/gpt-oss-20b`,
  `meta-llama/llama-4-scout-17b-16e-instruct`, `qwen/qwen3.6-27b`. Check
  Groq's model list (console.groq.com/docs/models) for what's currently live
  before picking one — this list goes stale.

### Automated tests (no Ollama, no network, no real PDF required)

```bash
cd tools/local_extraction
source .venv/bin/activate
pytest -v
```

49 tests, all passing as of this writing:
- `test_chunking.py` — TOC/heading/fixed-size strategy selection, including
  against tiny real PDFs built on the fly with PyMuPDF (no LLM involved).
- `test_validation.py` — word caps and quote-limit rules in isolation.
- `test_model_client.py` — the Ollama client refuses non-localhost hosts and
  a missing model name; the fake client behaves as a deterministic test double.
- `test_extraction_and_aggregation.py` — prompt building and response parsing
  against a `FakeModelClient`.
- `test_pipeline.py` — full pipeline against `FakeModelClient`, including a
  deliberately bad mock response (a 40-word quote, and separately an
  over-cap `core_thesis`) to prove the validator rejects both and writes
  nothing to `output/`.
- `test_no_network.py` — greps the entire `src/` tree for `http(s)://` URL
  literals and asserts every one points at `localhost`/`127.0.0.1`/`::1`.

## Directory layout

```
tools/local_extraction/
├── README.md
├── pyproject.toml
├── requirements.txt
├── .gitignore              # ignores .venv/ and scratch/ (raw text/output never committed)
├── src/local_extraction/
│   ├── chunking.py         # PDF parsing, TOC/heading detection, fixed-size fallback
│   ├── model_client.py     # LocalModelClient protocol, OllamaModelClient, FakeModelClient
│   ├── extraction.py       # per-chunk prompt + response parsing
│   ├── aggregation.py      # aggregation-pass prompt + response parsing
│   ├── validation.py       # word-cap / quote-limit checks (reject, never auto-fix)
│   ├── pipeline.py         # orchestration, principle_id assignment, output writing
│   ├── schema.py           # CandidatePrinciple / AggregatedDraft dataclasses
│   └── cli.py              # argparse entrypoint (`local-extraction` console script)
├── tests/                  # pytest suite described above
├── scratch/                # gitignored — raw PDF text + raw model output, local only
└── output/                 # validated JSON drafts, e.g. six-pillars-of-self-esteem.json
```
