# Lens

**Describe a real situation. Get advice grounded in a book you actually trust**

Lens is a small web app that sits between you and a self-help / nonfiction book you've chosen to live by. You type out something that's actually happening — a hard conversation you're dreading, a habit you keep breaking, a decision you're stuck on — and Lens hands back a short reflection plus three concrete, doable suggestions, all traceable to specific ideas in that book. Nothing it tells you is invented. Every claim has to point at a passage a human already reviewed.

**Live app:** https://lens-taupe-eight.vercel.app
**API:** https://lens-wf82.onrender.com (Render free tier — the first request after idle can take 30–60s to wake up; the shelf still loads instantly regardless, see [Design choices](#design-choices))

---

## Table of contents

- [The idea, in one breath](#the-idea-in-one-breath)
- [How it actually works](#how-it-actually-works)
- [Design choices — and why](#design-choices)
- [Tech stack](#tech-stack)
- [Repository layout](#repository-layout)
- [Running it locally](#running-it-locally)
- [Adding a new book](#adding-a-new-book)
- [Docs index](#docs-index)
- [Building something like this yourself](#building-something-like-this-yourself)

---

## The idea, in one breath

Most "AI + self-help book" demos are a chat window bolted onto a PDF — you ask a question, an LLM answers *as if it read the book*, and you have no way to know whether that's true. That's a hallucination machine wearing a book cover.

Lens refuses that shape on purpose. The book itself never touches the running app. What the app actually retrieves from and reasons over is a small, **human-reviewed set of paraphrased principles** — short, capped-length summaries of the book's real ideas, written or approved by a person who read the book. The LLM's job is narrower than "answer questions about this book": it's "write a reflection and three suggestions, using *only* the principles I hand you, and don't claim anything those principles don't support." A separate step then checks that claim automatically, before you ever see the output.

The product bet: **a smaller, honest, verifiable answer beats a fluent, unverifiable one** — especially for something people are using to actually change how they act.

---

## How it actually works

```
You describe a situation
        │
        ▼
Retrieval (no LLM)  ──  tag match + embedding similarity over
                        that book's reviewed principles only
        │
        ▼
Synthesis (1 LLM call)  ──  "using ONLY these principles, write
                             a reflection + up to 3 suggestions"
        │
        ▼
Verification (rule check + 1 LLM call)  ──  reject unknown
        principle_ids, reject over-length quotes, ask a second
        model pass "does this claim anything the principles
        don't support?"
        │
    ┌───┴────┐
   pass     fail (retry once, then fall back)
    │           │
    ▼           ▼
 shown to    a template built from the reviewed principle
   you        text itself — provably grounded, zero generation
```

Three separate, independently-testable steps — not one prompt doing everything. If verification can't confirm an LLM response is grounded, you never see it; you see a deterministic fallback built straight from already-reviewed text instead. The app would rather show you something slightly blunter than show you something invented.

Full architecture, schemas, and the actual prompt templates: [`docs/Architecture.md`](docs/Architecture.md).

### Getting a book *into* the app is a separate, offline, human-gated pipeline

This is the part that makes the "no hallucination" promise actually hold: **nothing about how a book enters the catalogue happens at request time, and nothing about it is fully automatic.**

1. Someone (an admin) picks a book.
2. Its ideas get turned into a draft set of short, tagged "principles" — either hand-written by a human who read the book, or drafted by a local LLM run against the PDF on the admin's own machine (the PDF never leaves that machine, never touches the app's servers).
3. **A human reads the draft against the book and approves or rejects it.** This step cannot be skipped programmatically — there's no code path that publishes a principle without `review_status` being flipped by a person.
4. Only after approval do embeddings get generated and the principles become retrievable by the live app.

A user of the live app can never cause new "book knowledge" to appear — that channel doesn't exist in the runtime. See [Adding a new book](#adding-a-new-book) and [`docs/ADDING_A_BOOK.md`](docs/ADDING_A_BOOK.md) for the full walkthrough.

---

## Design choices — and why

**The book never lives in the running app — only paraphrased, length-capped, human-reviewed summaries do.**
Every principle's summary is capped at 200 words and checked *at the database level*, not just in application code — a `CHECK` constraint using a Postgres function, not something an app bug could quietly bypass. Quotes are limited to 15 words and one per principle, enforced by a rule-based scan, not "the prompt asked it nicely." This is a copyright-safety and trust decision as much as an engineering one: the app can genuinely say it never stores or serves the book's actual text.

**Retrieval, synthesis, and verification are three separate function calls, not one prompt.**
Retrieval (which principles are relevant) uses no LLM at all — it's tag matching plus embedding similarity, a plain deterministic function you can unit-test with fixed inputs and expected outputs. Synthesis is one LLM call, scoped to only the retrieved principles. Verification is a second, independent pass — half rule-based (regex/length checks), half a second LLM call whose only job is "does this claim something the source material doesn't support?" Collapsing this into a single mega-prompt would make grounding untestable and unfalsifiable; splitting it means each piece can be wrong in isolation and caught.

**Verification fails *closed*, not open.**
If the grounding check can't confirm an answer is safe, the user sees a deterministic, template-built response made of the *reviewed principle text itself* — not a warning next to a possibly-hallucinated answer, not a best-effort guess. The fallback is uninteresting by design: it's provably grounded because nothing in it was generated.

**The book catalogue is data, not code.**
Adding book #8 is a JSON submission and a review click, not a new code path, new prompt template, or new UI branch. This is what makes "human review gate, not an engineering bottleneck" actually true in practice.

**Local-first extraction, with one narrow, explicit, opt-in exception.**
The default way to turn a PDF into draft principles runs entirely on the admin's own machine against a local model (Ollama) — the book text never goes to a third party. A `--provider groq` flag exists as a conscious, opt-in tradeoff for when a local model's quality is the bottleneck; it's tested to be the *only* non-localhost network call anywhere in that tool (`tests/test_no_network.py` greps the whole source tree for it). Nothing about this exception is silent or default-on.

**Runtime synthesis does call a hosted LLM (Groq) — a different, and differently-justified, tradeoff.**
Once the app is meant to serve more than one person on their own machine, "run everything locally" stops being a coherent option — there's no local machine for a hosted deployment to run a model on. This is flagged explicitly in the architecture doc as a real privacy tradeoff (a user's situation text and the retrieved principle summaries go to Groq on every request), not something quietly slipped in. It's a different exception, made for a different, honestly-stated reason, from the extraction-time one above.

**No PII, no password, for this MVP.**
The app generates an anonymous local ID for you rather than collecting an email or building half-secured auth around something with no real lock on it yet. Fewer things to leak beats a login form that only looks safer than it is.

**The mobile shelf renders instantly from a static snapshot, then reconciles live.**
Render's free tier sleeps the backend after ~15 minutes idle; a cold start takes 30–60 seconds. Rather than block the whole UI behind a spinner for that window, the 3D bookshelf renders immediately from a bundled point-in-time copy of the real catalogue — fully explorable with zero backend round-trip — while the live data fetches in the background and silently swaps in. Only the one action that truly needs a live backend (submitting a situation) waits, and only shows a "waking up" message if that wait is still unresolved a few seconds in.

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Frontend | Next.js (React, TypeScript) — deployed on Vercel | SSR for fast first paint; static hosting is free and simple for a small app |
| 3D bookshelf | Three.js, custom `ShelfEngine` | Physical, browsable metaphor for "pick a book" rather than a dropdown |
| API | FastAPI (Python) — deployed on Render | Async-first, Pydantic doubles as request/response schema validation for the strict JSON contracts the LLM steps depend on |
| Database | Postgres — hosted on Supabase | Relational integrity for the book → principle → log → analysis → suggestion chain |
| Vector search | `pgvector` extension on the same Postgres instance | Keeps infra to one database at this corpus size (hundreds–low thousands of principle rows); no need for a dedicated vector DB yet |
| Embeddings | Voyage AI | Purpose-built retrieval embeddings |
| LLM (synthesis + verification) | Groq (`openai/gpt-oss-120b`) | Fast hosted inference; instruction-following quality matters here because the strict-JSON, grounded-only constraints are exactly where weaker models fail. (Was `llama-3.3-70b-versatile` until Groq decommissioned it 2026-08-16.) |
| Book extraction (offline, admin-only) | Ollama (local, default) or Groq (opt-in) | See [Design choices](#design-choices) — deliberately kept separate from the runtime LLM client |
| Migrations | Alembic | |
| CI | GitHub Actions | Runs the full backend + frontend test suite on every push/PR — does **not** deploy anything; Render/Vercel redeploy on push via their own native GitHub integration |

Full stack table with rejected alternatives and reasoning: [`docs/Architecture.md` §5](docs/Architecture.md).

---

## Repository layout

```
LENS/
├── README.md                    # you are here
├── initial_prompt.md            # the original design prompt that produced Architecture.md
├── render.yaml                  # Render blueprint (backend deploy config)
├── docs/
│   ├── Architecture.md          # source of truth: schemas, API contracts, prompts, eval plan
│   ├── DEPLOYMENT.md            # full deploy runbook (Supabase/Render/Vercel/Groq)
│   ├── ADDING_A_BOOK.md         # admin walkthrough: PDF → live in the app
│   └── HANDOFF.md               # running build log / implementation decisions
├── backend/                     # FastAPI app
│   ├── app/
│   │   ├── routers/             # books, principles, daily_logs, analyses, suggestions, users, ingestion
│   │   ├── models/              # SQLAlchemy models (books, principles, users, daily_logs, analyses, suggestions, streaks)
│   │   ├── retrieval.py         # Step A — tag match + embedding search, no LLM
│   │   ├── synthesis.py         # Step B — the one LLM call that drafts a reflection + suggestions
│   │   ├── verification.py      # Step C — rule checks + grounding LLM call
│   │   ├── llm.py               # LLMClient protocol; Groq + Ollama implementations
│   │   └── embeddings.py        # Voyage AI client
│   ├── scripts/
│   │   ├── submit_local_draft.py   # push a draft JSON into the ingestion queue
│   │   └── mark_reviewed.py        # flip review_status + generate embeddings (the "publish" step)
│   └── tests/                   # 119 tests, pytest
├── frontend/                    # Next.js app
│   ├── app/                     # page.tsx, api.ts, bookCatalogFallback.ts
│   └── components/BookShelf3D/  # Three.js shelf: engine, cover art, motion, config
├── tools/local_extraction/      # standalone CLI: PDF → draft principles JSON (never deployed, never imported by backend/)
└── books/                       # source PDFs for extraction (gitignored — never committed, never uploaded anywhere)
```

---

## Running it locally

```bash
# 1. Database (Postgres + pgvector)
cd backend
docker compose up -d

# 2. Backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in VOYAGE_API_KEY at minimum; LLM_PROVIDER=ollama works with no key
alembic upgrade head
uvicorn app.main:app --reload

# 3. Frontend
cd ../frontend
npm install
cp .env.local.example .env.local   # NEXT_PUBLIC_API_BASE=http://localhost:8000
npm run dev
```

Backend test suite: `cd backend && pytest` (119 tests, no external services required beyond the local Postgres container — LLM/embedding calls are faked in tests via dependency overrides).

Full production deploy steps (Supabase, Render, Vercel, Groq, env vars, CORS, order of operations): [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

---

## Adding a new book

Short version: extract → submit → **human review** → publish. The mandatory review step is the whole point — see [`docs/ADDING_A_BOOK.md`](docs/ADDING_A_BOOK.md) for the complete, copy-pasteable walkthrough from "I have a PDF" to "it's live and retrievable in the deployed app," including the global-`principle_id`-collision gotcha that's bitten this project before.

---

## Docs index

| Doc | What's in it |
|---|---|
| [`docs/Architecture.md`](docs/Architecture.md) | The authoritative design doc — system diagram, data model, full prompt templates, tech stack rationale, evaluation plan, and the running addendum log of every real deviation from the original design |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Step-by-step production deploy runbook |
| [`docs/ADDING_A_BOOK.md`](docs/ADDING_A_BOOK.md) | Admin walkthrough: taking a book PDF from zero to live in the app |
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | Build log — what was built, in what order, and why, including every deviation from the original architecture and the reasoning behind it |
| [`initial_prompt.md`](initial_prompt.md) | The original prompt used to generate the architecture doc — useful background on intent if you're wondering *why* the constraints are shaped the way they are |
| [`tools/local_extraction/README.md`](tools/local_extraction/README.md) | The standalone PDF-extraction CLI's own docs |

---

## Building something like this yourself

If you're taking apart the idea rather than the code, the transferable pieces are:

1. **Decide what "grounded" means before you write a prompt.** For Lens it's "every claim traces to a human-approved summary, checked by a second automated pass, checked by a human before that summary ever entered the system." Pick your own definition, but pick one concrete enough to write a test against.
2. **Split retrieval from generation from verification.** One opaque prompt can't be partially wrong in a way you can debug. Three small, independently-testable functions can.
3. **Put your safety property in the data model, not just the prompt.** A `CHECK (word_count(summary) <= 200)` constraint at the database level survives a bug that a prompt instruction doesn't.
4. **Decide what "fails closed" looks like, and make it boring on purpose.** The fallback response should be the least interesting output in the whole system — that's what makes it trustworthy.
5. **Keep the thing that produces trusted content separate from the thing that serves it.** Ingestion here is offline, admin-gated, and structurally incapable of running at request time — that's not a UI restriction, there's no code path for it.
6. **Say the quiet part in your docs, especially the tradeoffs you didn't love making.** Every deviation from the original design in this repo — Groq instead of a fully local model, no real auth yet, the free-tier cold start — is written down with the actual reason, not smoothed over. Future-you (or whoever picks this up) needs the "why," not just the "what."
