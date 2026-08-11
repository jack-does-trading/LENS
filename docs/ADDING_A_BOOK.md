# Admin Walkthrough: Adding a New Book, PDF to Live

**Audience:** you're the admin, you have a book PDF, and you want it live in the deployed app (https://lens-taupe-eight.vercel.app) with real, retrievable principles.

**Time:** roughly 30–90 minutes of hands-on work for extraction/submission, plus however long it takes you to actually read the book against the draft (the mandatory review step — budget real time for this, it's not a formality).

**Prerequisites:**
- The book's PDF, on your own machine, in `books/` (already gitignored — never gets committed).
- [Ollama](https://ollama.com) installed and a model pulled (`ollama pull llama3.1` or similar) — or a [Groq](https://console.groq.com) API key if you'd rather use `--provider groq` for extraction (see [Step 1](#step-1-extract-draft-principles-locally), "opt-in" callout).
- Your production secrets on hand: the deployed backend's `INGESTION_API_KEY` and `DATABASE_URL` (Supabase pooled URI) and `VOYAGE_API_KEY` — same values set in Render's dashboard for `lens-backend`.
- `backend/.venv` set up locally (`pip install -r requirements.txt`) — the submission/review scripts run from your machine, pointed at the *production* database, not from Render itself.

---

## The five stages, at a glance

```
1. Extract   →  2. Check for principle_id collisions  →  3. Submit  →  4. Review (human, mandatory)  →  5. Publish (embeds + goes live)
   (local)         (manual, before submitting)             (script)     (you, reading the real book)      (script, --mark-reviewed)
```

Nothing in stages 1–3 makes the book visible in the app. **Only stage 5 does** — the app filters everything by `review_status = human_reviewed`, and stage 5 is the only thing that sets it.

---

## Step 1: Extract draft principles locally

From `tools/local_extraction/` (its own venv, separate from `backend/`):

```bash
cd tools/local_extraction
source .venv/bin/activate

local-extraction \
  --model llama3.1 \
  --title "The Book's Real Title" \
  --author "The Author's Name" \
  --book-id the-book-slug \
  --tone pragmatic \
  --pdf "../../books/the-book.pdf" \
  --verbose
```

- `--book-id` becomes the book's slug/primary key everywhere downstream — pick it now, it's annoying to rename later. Convention used so far: kebab-case, short (`atomic-habits`, `rich-dad-poor-dad`, `never-split-the-difference`).
- `--tone` must be one of `pragmatic | philosophical | spiritual | scientific | narrative` — pick whichever actually fits the book, this isn't cosmetic (it steers the synthesis prompt's voice at runtime).
- This runs entirely against your local Ollama model. The PDF and all raw per-chunk model output stay in `tools/local_extraction/scratch/` (gitignored) — **nothing from this step leaves your machine.**
- Output lands at `tools/local_extraction/output/<book-id>.json` — a single validated draft: `core_thesis` + a list of `principles`, each with `principle_id`, `name`, `summary`, `source_chapter`, `applies_to_tags`.
- If validation fails (a quote too long, a summary over the word cap), the tool **rejects the draft outright and writes nothing** — it will never silently truncate or "fix" the model's output for you. Fix the underlying issue (usually: try a different/bigger model, or re-run with different chunk settings) and re-run.

**Opt-in alternative — `--provider groq`:** if your local model's extraction quality is the bottleneck, you can send the book's chapter text to Groq's hosted API instead (`--provider groq --groq-api-key ...` or set `GROQ_API_KEY` in `tools/local_extraction/.env`). This is a real, deliberate exception to "the PDF never leaves your machine" — only use it if you've consciously accepted that tradeoff. See `tools/local_extraction/README.md`'s "Running it with Groq" section for the exact flags. Either provider produces the identical draft JSON shape and lands in the identical review queue — nothing downstream cares which one you used.

---

## Step 2: Check for `principle_id` collisions — **do this before submitting**

**This is the step this project has actually gotten burned on before.** `principle_id` is a **global** primary key across *all* books in Postgres, not scoped per-book. The extraction tool generates each `principle_id` by slugifying that principle's name (e.g. `"Start small"` → `start-small`) with no awareness of what other books already used. Two different books both landing on `start-small` will submit fine individually, but the second one will fail with a 409 (or silently collide) the moment both exist in the same database.

Before submitting, diff your new draft's principle IDs against what's already live:

```bash
# From the deployed app, get every principle_id currently in production:
curl -s https://lens-wf82.onrender.com/api/principles | python3 -c \
  "import json,sys; print('\n'.join(sorted(p['principle_id'] for p in json.load(sys.stdin))))" \
  > /tmp/existing-principle-ids.txt

# Your new draft's IDs:
python3 -c \
  "import json; d=json.load(open('tools/local_extraction/output/the-book-slug.json')); \
   print('\n'.join(sorted(p['principle_id'] for p in d['principles'])))" \
  > /tmp/new-principle-ids.txt

# Anything printed here is a collision you must fix before submitting:
comm -12 /tmp/existing-principle-ids.txt /tmp/new-principle-ids.txt
```

If you get hits, open the draft JSON and prefix the colliding IDs with the new book's slug (e.g. `start-small` → `the-book-slug-start-small`), matching the pattern already used for the 4 books this bit previously (see `tools/local_extraction/output/atomic-habits.json`, `rich-dad-poor-dad.json`, `never-split-the-difference.json`, `subtle-art-of-not-giving-a-fck.json` for the precedent). Nothing else needs to change — `principle_id` isn't referenced by anything positional, just by value.

---

## Step 3: Submit the draft to the ingestion queue

`backend/scripts/submit_local_draft.py` flattens the tool's output shape into what `POST /api/ingestion/draft-submissions` expects and sends it, gated by the `X-Ingestion-Api-Key` header.

```bash
cd backend
source .venv/bin/activate

python scripts/submit_local_draft.py \
  ../tools/local_extraction/output/the-book-slug.json \
  --base-url https://lens-wf82.onrender.com
```

- Reads `INGESTION_API_KEY` from `backend/.env` — make sure your local `.env` has the **same value** that's set in Render's dashboard for `lens-backend`, or the request 401s. (Your local `.env` is for your own scripts to authenticate *against the production API* here — it's not "a separate local key," it must match production's.)
- If `the-book-slug` already exists (e.g. you're resubmitting a corrected draft), add `--replace` — this deletes and replaces all of that book's principles. Without `--replace`, a re-submission 409s on purpose, so a draft mid-review is never silently clobbered.
- **Do not pass `--mark-reviewed` yet** — see the next step. Submitting only queues the draft as `review_status=pending_review`; it is not visible in the live app after this step, by design.
- On success you'll see something like: `Submitted 'the-book-slug': 42 principles, review_status='pending_review', version=1`.

---

## Step 4: Review — the mandatory, non-skippable step

This is not a formality and there is no way to script around it — the codebase has no code path that publishes a principle without a human explicitly flipping `review_status`.

**Read the draft against the actual book.** For each principle, check:
- Does the `summary` accurately represent what the book actually argues? (Fidelity — not "does it sound plausible," does it actually match the source.)
- Are there any quotes over 15 words, or more than one quote per principle? (The extraction tool should already reject these, but re-check by eye — this is the actual point of a *human* review gate, not a re-run of the same automated check.)
- Do `applies_to_tags` make sense for retrieval — would a user describing a related situation plausibly hit this principle via tag match?
- Does `core_thesis` (in the draft JSON's `book` object) actually represent the book's argument in ≤150 words?

**If this book's `extraction_method` is `local_model`** (i.e. you ran the extraction tool, not hand-wrote the principles), you are the *first* human to check the draft against the source at all — nobody else did this before you. Treat it as a genuine read-through, not a skim. This is architecturally the *only* defense this system has against a model misstating, inverting, or hallucinating the author's actual argument — there's no other check downstream that catches that class of error.

**Found problems?** Fix the JSON by hand (or re-run extraction with adjustments) and resubmit with `--replace` (Step 3) before proceeding. Do not mark a draft reviewed that you haven't actually checked.

---

## Step 5: Publish (mark reviewed + generate embeddings)

Once you've actually read it and you're satisfied:

```bash
cd backend
DATABASE_URL="postgresql://postgres:<password>@<supabase-host>:5432/postgres" \
  .venv/bin/python scripts/mark_reviewed.py the-book-slug
```

Use the **direct** (port `5432`) Supabase connection string here, not the pooled one — this matches the same guidance as running migrations (see `docs/DEPLOYMENT.md` §2). This script:

1. Flips `review_status` to `human_reviewed` on the book **and every one of its principles**.
2. Increments the book's `version`.
3. Generates a real Voyage AI embedding for every principle (`name + summary + tags`) and stores it directly on `principles.embedding`.

Output: `Marked 'the-book-slug' (and its principles) as human_reviewed (version incremented), 42 principle(s) embedded.`

**This is the step that makes the book live.** Retrieval (`app/retrieval.py`) only ever queries `review_status = human_reviewed` principles — nothing before this point is reachable by a real user's request, no matter how far along the pipeline it is.

Combined shortcut: `submit_local_draft.py` also accepts `--mark-reviewed` to do steps 3 and 5 in one call — **only use this once you've already done the actual reading in Step 4**, since the flag itself does not verify that you have:

```bash
python scripts/submit_local_draft.py \
  ../tools/local_extraction/output/the-book-slug.json \
  --base-url https://lens-wf82.onrender.com \
  --mark-reviewed
```

(Even with the combined flag, `mark_reviewed()`'s embedding step still needs `VOYAGE_API_KEY` — set it in your local `backend/.env` for this to work, same value as production.)

---

## Verify it's actually live

```bash
# Book shows up, human_reviewed:
curl -s https://lens-wf82.onrender.com/api/books | python3 -m json.tool | grep -A3 the-book-slug

# Principles are there and embedded:
curl -s "https://lens-wf82.onrender.com/api/principles?book_id=the-book-slug" | python3 -m json.tool | head -50
```

Then, in the actual app: open https://lens-taupe-eight.vercel.app, find the book on the shelf (may take a Vercel redeploy if you want it in the static fallback catalogue too — see the note below), select it, describe a real situation relevant to one of its principles, and confirm you get a grounded reflection referencing that book rather than a generic one.

**One more place the book needs to land, optionally:** `frontend/app/bookCatalogFallback.ts` is a bundled, point-in-time snapshot of the catalogue used to render the shelf instantly while the live backend is waking up from a cold start (see `docs/Architecture.md` §9's "optimistic shelf render" addendum). It is **not** required for the book to be functionally live — the real `listBooks()` fetch will pick it up automatically on every page load once published above — but if you want the new book visible *immediately*, even during a cold-start window, update that file with the new book's entry and push to `main` (Vercel redeploys automatically).

---

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| 409 on submit | `book_id` already exists | Add `--replace` if intentional, or pick a new `book_id` |
| 409 on submit mentioning a `principle_id` conflict | Global `principle_id` collision with an existing book — see [Step 2](#step-2-check-for-principle_id-collisions--do-this-before-submitting) | Prefix colliding IDs with the book slug, resubmit |
| 401 on submit | `INGESTION_API_KEY` in your local `.env` doesn't match Render's | Copy the exact value from Render's dashboard → `lens-backend` → Environment |
| 503 on submit | `INGESTION_API_KEY` isn't set on the *live* backend at all | Set it in Render's dashboard first |
| Book never appears in the app after `mark_reviewed.py` | Ran the script against your **local** dev DB instead of the production `DATABASE_URL` | Re-run with the Supabase connection string explicitly passed |
| `mark_reviewed.py` errors on `VOYAGE_API_KEY` | Not set in the `.env` your shell is using when you run the script | Set it in `backend/.env` (must match production's key) |
| Extraction tool rejects the draft outright, writes nothing | A quote exceeded 15 words, a summary exceeded 200 words, or similar validation failure | Check `--verbose` output for the specific rule violated; try a stronger model or adjust chunking, then re-run |

---

## Why it's this many steps, on purpose

Every gate here exists to keep one promise: **nothing a live user does can cause new "book knowledge" to enter the system, and nothing enters the system without a human having actually checked it against the source.** Collapsing steps 3–5 into one automatic action would technically be less typing, and would also mean an unreviewed, possibly-hallucinated draft could go live off a single command. See [`docs/Architecture.md` §2](Architecture.md) for the full reasoning, and the README's [Design choices](../README.md#design-choices) section for the short version.
