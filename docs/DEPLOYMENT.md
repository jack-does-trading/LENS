# Deployment Runbook

Stack: **Supabase** (Postgres + pgvector) · **Render** (FastAPI backend) ·
**Vercel** (Next.js frontend) · **Groq** (synthesis/verification LLM) ·
**Voyage AI** (embeddings, unchanged) · **GitHub Actions** (CI).

## 0. One thing to get straight first: what actually gets deployed

**Both `backend/` and `frontend/` get deployed** — to two different
platforms. Vercel only serves the compiled Next.js frontend (static
HTML/JS); it has no database and can't run Python. The frontend is a
client-side app that calls the backend over HTTP for everything (books,
analyses, principles, streaks) — without the backend running somewhere
public, the deployed frontend would load and then fail every single
request. Render is what makes the backend "somewhere public."

The one piece that genuinely **never** gets deployed anywhere is
`tools/local_extraction/` — that's deliberate, per the architecture doc: it
only ever runs on your own machine against your own book PDFs, and its only
connection to the rest of the system is the draft JSON it hands to the
ingestion endpoint. Render and Vercel never touch it, and it stays out of
both platforms' build configs entirely.

So: one GitHub repo (this one), two deploy targets pointed at two different
subfolders of it.

## 1. Push this repo to GitHub

Already done locally (git initialized, `.gitignore`d, first commit made).
From here:

```bash
# Create the repo on GitHub first (via github.com or `gh repo create`),
# then, from /Users/bhavyadeephada/Desktop/LENS:
git remote add origin git@github.com:<you>/lens.git   # or the HTTPS URL
git push -u origin main
```

**Before you push**, double check `books/` never shows up in `git status`
— it's gitignored, but if it was ever committed before the `.gitignore`
existed you'd need to purge it from history too (`git log --all --
books/` to check; it should return nothing). Same goes for `backend/.env`
and `tools/local_extraction/.env` — real secrets, never committed.

## 2. Supabase (database)

1. Create a project at supabase.com (free tier).
2. Open the SQL Editor and run:
   ```sql
   create extension if not exists vector;
   ```
   (pgvector ships with Supabase but isn't enabled by default.)
3. Go to **Project Settings → Database → Connection string**. Use the
   **connection pooling** URI (port `6543`, `?pgbouncer=true`) for the
   app's runtime `DATABASE_URL` — Render's free tier and Supabase's pooler
   are both built for exactly this kind of intermittent connection pattern.
   For running migrations (`alembic upgrade head`) use the **direct**
   connection (port `5432`) instead — some migration operations don't play
   well through a transaction pooler.
4. Run migrations once, from your own machine, pointed at Supabase:
   ```bash
   cd backend
   DATABASE_URL="postgresql://postgres:<password>@<host>:5432/postgres" \
     .venv/bin/python -m alembic upgrade head
   ```
5. Keep both the pooled and direct URLs handy — you'll need the pooled one
   for Render's `DATABASE_URL` env var.

## 3. Groq (LLM)

1. Create a free account at console.groq.com, generate an API key.
2. That's it code-side — `backend/app/llm.py`'s `GroqLLMClient` and the
   `LLM_PROVIDER=groq` / `GROQ_API_KEY` / `GROQ_MODEL` settings already
   exist (this session's work). Nothing else to wire up.

**Worth knowing**: this is a real, conscious privacy tradeoff, not a
free upgrade. `OllamaLLMClient`'s whole reason for existing was that a
user's daily journal entries and the book principles they're compared
against never leave their machine. Routing synthesis through Groq means
that content now goes to Groq's servers on every single analysis. You've
already decided this tradeoff is worth it for a real deployment — just
flagging it explicitly, the way the codebase flags every other deviation
from its original privacy stance.

## 4. Voyage AI (embeddings) — no change

Same key you're already using. Its free tier (3 RPM / 10K TPM without a
payment method, 200M free tokens) is what you're starting on; revisit only
if 5-10 concurrent users actually start hitting those ceilings in practice
— not worth solving preemptively.

## 5. Render (backend)

**Option A — Blueprint (recommended, uses the `render.yaml` already in the
repo root):**
1. Render dashboard → **New +** → **Blueprint** → connect the GitHub repo.
2. Render reads `render.yaml` and proposes the `lens-backend` service.
3. Fill in the secrets it left blank (`sync: false` in the file):
   `DATABASE_URL` (Supabase pooled URI), `VOYAGE_API_KEY`, `GROQ_API_KEY`,
   `INGESTION_API_KEY` (any long random string you generate — this gates
   the `/api/ingestion/draft-submissions` endpoint), and `CORS_ALLOW_ORIGINS`
   (circle back to this in step 6, once you know the Vercel URL).
4. Deploy. First deploy will be slow (installs deps + runs migrations).

**Option B — manual, if you'd rather not use the blueprint:** New Web
Service → connect repo → **Root Directory**: `backend` → **Build Command**:
`pip install -r requirements.txt && alembic upgrade head` → **Start
Command**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT` → **Health
Check Path**: `/health` → add the same env vars as above.

**About the free-tier cold start** ("the loader for the wakeup" you
asked about): Render's free web services sleep after ~15 minutes idle and
take roughly 30-60 seconds to wake on the next request. Rather than fight
that with an external keep-alive pinger (a gray area against most free
tiers' intent, and not guaranteed to even work), the frontend now handles
it honestly: `frontend/app/page.tsx` shows "Pulling your shelf together…"
immediately, and upgrades to "Waking up the server — this can take up to a
minute on the free tier…" if the very first request hasn't resolved within
4 seconds — a warm backend never sees that message, only a cold one does.
Already implemented and tested this session — no further action needed
here.

## 6. Vercel (frontend)

1. Vercel dashboard → **Add New → Project** → import the same GitHub repo.
2. **Root Directory**: `frontend`. Framework preset (Next.js) auto-detects.
3. Add env var `NEXT_PUBLIC_API_BASE` = your Render service's public URL
   (e.g. `https://lens-backend.onrender.com`).
4. Deploy. Vercel gives you a `*.vercel.app` URL (and lets you attach a
   custom domain later, for free).
5. **Go back to Render** and set `CORS_ALLOW_ORIGINS` to a JSON array
   containing that exact Vercel URL, e.g.
   `["https://lens-frontend.vercel.app"]` — `backend/app/config.py`'s
   `cors_allow_origins` already parses a JSON array straight from the env
   var, no code change needed, just don't forget this step or every
   frontend request will fail with a CORS error despite everything else
   working.

## 7. GitHub Actions — what it does here, exactly

`.github/workflows/ci.yml` (already added) runs on every push and PR to
`main`:
- **backend-tests**: spins up a `pgvector/pgvector:pg16` service container
  (same image as local dev), installs `backend/requirements.txt`, runs the
  full `pytest` suite against it.
- **frontend-checks**: `npm ci`, `tsc --noEmit`, `next build`.

**This does not deploy anything, on purpose.** Once you connect the repo
in both Vercel and Render's dashboards (steps 5-6 above), *they* watch
`main` and redeploy automatically on every push — that's their native
GitHub integration, not something Actions needs to orchestrate. So "update
the site on every push" is already fully satisfied the moment both
dashboards are connected; the Actions workflow's job is purely to make
sure main doesn't drift into a broken state, matching this project's own
"if tests fail, fix implementation, don't skip tests" rule from
`docs/HANDOFF.md`.

### Optional: gate the deploy on tests passing

Right now Vercel/Render deploy independently of whether CI passes — a
broken push still goes live, CI just tells you *after*. If you'd rather
Actions be the thing that decides whether a deploy happens at all:

1. In Vercel: Project Settings → Git → turn off "Automatically deploy" (or
   restrict production deploys to a branch you never push to directly).
   In Render: Settings → turn off "Auto-Deploy".
2. In each platform, generate a **Deploy Hook** URL (Vercel: Project
   Settings → Git → Deploy Hooks; Render: Settings → Deploy Hook) — a
   secret URL that triggers a deploy when POSTed to.
3. Add both as GitHub Actions secrets: `VERCEL_DEPLOY_HOOK_URL`,
   `RENDER_DEPLOY_HOOK_URL` (repo Settings → Secrets and variables →
   Actions).
4. Add this job to `.github/workflows/ci.yml`:
   ```yaml
     deploy:
       needs: [backend-tests, frontend-checks]
       if: github.ref == 'refs/heads/main' && github.event_name == 'push'
       runs-on: ubuntu-latest
       steps:
         - run: curl -fsS -X POST "${{ secrets.VERCEL_DEPLOY_HOOK_URL }}"
         - run: curl -fsS -X POST "${{ secrets.RENDER_DEPLOY_HOOK_URL }}"
   ```

Not done by default because it requires the manual dashboard changes in
step 1 first — shipping it pre-wired would look like it works while
silently double-deploying (once from the native integration, once from the
hook) until you actually disable native auto-deploy.

## 8. Order of operations, start to finish

1. Push repo to GitHub (§1).
2. Supabase project + `vector` extension + run `alembic upgrade head`
   against it (§2).
3. Get a Groq API key (§3).
4. Deploy backend on Render with all secrets set (§5) — `CORS_ALLOW_ORIGINS`
   can stay as just `localhost:3000` for now, fix it in step 6.
5. Deploy frontend on Vercel, pointed at the Render URL (§6).
6. Update Render's `CORS_ALLOW_ORIGINS` to include the real Vercel URL,
   redeploy the backend (§6, last bullet).
7. Visit the Vercel URL. First load will show the "waking up" message if
   Render's instance was asleep — that's expected, not broken.
8. Submit + review + mark-reviewed your books against the *deployed* DB
   (same `scripts/submit_local_draft.py` / `scripts/mark_reviewed.py`,
   just pointed at the Supabase `DATABASE_URL` via `.env` instead of local
   Postgres) — nothing is retrievable in the live app until this runs at
   least once.
