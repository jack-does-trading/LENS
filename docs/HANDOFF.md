# Lens — Context Handoff for Model Continuation

> **Purpose:** This file captures everything learned building Lens through Phase 1 so a new model can resume without re-discovering context. Read this **before** writing code.
>
> **Last updated:** 2026-08-03  
> **Current state:** End-to-end MVP working. Core loop is now **per-situation, not per-day**: a user describes a situation and asks for advice, any number of independent times a day (§4/§5 "Product shift", `daily_logs` no longer unique per `(user_id, date)`), and gets a reflection + top-3 tips grounded in the book. Built, tested (88 backend tests), and live-smoke-tested against real Postgres/pgvector, real Voyage AI, and a real local Ollama model. A minimal Next.js frontend exists at `frontend/`. Reflection is a single paragraph shown with related-principle chips, and suggestions are a hard-capped top-3 with explanations. Synthesis now genuinely passes verification most of the time instead of relying on the fallback template. Auth, a real review/publish UI, background jobs, and a "past interactions" history view (deliberately deferred, see §5) are still out of scope for this MVP — see §16 and §9.14-9.16 for the reasoning.

---

## 1. What Lens Is

**Lens** is a book-grounded personal-analysis web app. Core user loop:

1. User picks a book from a curated catalogue
2. User logs daily actions/journal entries
3. App retrieves relevant principles from that book (tag match + embedding search)
4. LLM synthesizes a daily analysis + 1–3 concrete suggestions for tomorrow
5. User marks suggestions done/skipped → feeds tomorrow's context
6. Over weeks, user sees streak/trend progress against book-defined metrics

**Critical product constraint:** The system must **never** store, cache, or forward full copyrighted book text. "Book knowledge" = human-reviewed, length-capped principle summaries only.

---

## 2. Source of Truth Documents

| File | Role |
|---|---|
| `docs/Architecture.md` | **Authoritative architecture doc** — schemas (§4), API contracts (§3), tech stack (§5), constraints, evaluation plan, local-extraction addendum (§8) |
| `initial_prompt.md` (repo root) | Original prompt used to generate the architecture; useful background on intent |
| `docs/HANDOFF.md` (this file) | Implementation state, decisions, gaps |
| `docs/PHASE_1_AUDIT.md` | Phase 1 audit report (schema dump, test gap closure, requirement→test matrix) |

**Resolved:** architecture now lives at `docs/Architecture.md` (macOS's case-insensitive filesystem means `docs/ARCHITECTURE.md` resolves to the same file). There is no `architecture_1.0.md` at repo root anymore.

---

## 3. Non-Negotiable Constraints (enforce in code, not comments)

These were explicitly stated by the user and must never be violated:

1. **No full book text anywhere** — No component may store, cache, or forward copyrighted book text. Book-related text fields (`core_thesis`, `principle.summary`) must have **hard length caps at schema/DB level**.

2. **Three separate pipeline steps** — Retrieval (tag + embedding match), synthesis (LLM call), and verification (grounding check) are **three independent, separately callable functions**. Never merge into one LLM call.

3. **Quote limits via rule-based check** — Every generated quote checked against 15-word limit, max one quote per principle. Must be **rule-based**, not prompt-trust.

4. **Phased delivery** — Work in phases; stop after each phase for user review. Do not build ahead.

5. **Test integrity** — If tests fail, fix implementation not tests. Do not skip, weaken, or xfail tests to green the suite.

---

## 4. Tech Stack (from architecture §5 — do not deviate without user approval)

| Layer | Choice | Notes |
|---|---|---|
| Frontend | Next.js (TypeScript) | **Built (minimal MVP UI)** — `frontend/`, single page, no build-time deviation from architecture's choice |
| API | FastAPI (Python) | **Phase 1 done**, now with full analysis loop |
| Primary DB | Postgres | Via docker-compose |
| Vector store | Postgres + `pgvector` | `principles.embedding vector(1024)` + ivfflat cosine index, migration `003` |
| Embeddings | Voyage AI (`voyage-3`) | **Built** — `app/embeddings.py`; real client + fake client for tests |
| LLM (synthesis + verification) | **Local Ollama (`llama3.2:3b`), not Claude API** | **Deviation from architecture §5, approved by user for this MVP** — see 9.14. `app/llm.py` |
| Auth | Managed provider (Auth0/Clerk) | **Still not started** — MVP uses a no-auth, locally-generated `user_id` instead (9.10, 9.16) |
| Background jobs | Redis + RQ (Python) | **Still not started** — streaks computed synchronously on request instead (9.15) |
| Migrations | Alembic | `001_initial_schema.py`, `002_extraction_method.py`, `003_principle_embedding.py`, `004_reflection_and_explanation.py`, `005_reflection_paragraph.py`, `006_drop_daily_log_date_unique.py` |

---

## 5. Build Phases (planned scope)

### Phase 1 — Data model ✅ IMPLEMENTED (audit pending)

- Alembic migrations for all 7 tables per §4
- Read-only routes: books, principles
- Read/write routes: daily_logs
- Word-cap enforcement at DB + Pydantic layer
- Tests for principle summary word cap

### Phase 1 audit — ✅ COMPLETE (2026-07-08)

Full report: `docs/PHASE_1_AUDIT.md`. Summary:

1. Migrations run on a genuinely clean DB (container recreated, no persisted volume) → schema dump for all 7 tables matches architecture §4 exactly, no discrepancies.
2. Added the 5 previously-missing tests: `core_thesis` word cap (DB + Pydantic), `daily_logs` unique `(user_id, date)`, FK rejection (principle → nonexistent book), books/principles PUT → 405.
3. Fixed the `db_session` fixture fragility (`tests/conftest.py:52`, guarded `transaction.rollback()` with `if transaction.is_active:`) — verified clean even with `SAWarning` escalated to a hard error.
4. Full suite: **11/11 passed**, raw output and requirement→test→pass/fail matrix in `docs/PHASE_1_AUDIT.md`.

**Phase 2 is unblocked.**

### Local Extraction Tool (architecture §2 Path B / §8 addendum) — ✅ BUILT

Standalone CLI at `tools/local_extraction/` (own venv, never imported by `backend/`), 110 tests passing. Parses a book PDF locally (PyMuPDF), chunks by chapter (TOC → heading regex → fixed-size fallback, logging which strategy and why), runs a per-chunk extraction pass against a local Ollama model (paraphrase-only, ≤15-word quotes, ≤1 quote/principle, validated per chunk before the expensive aggregation step), then a hierarchical aggregation pass that merges/dedupes into one `core_thesis` + consolidated `principles` list. Validates before writing anything — rejects clearly, never truncates/auto-fixes. Writes only the validated draft JSON to `output/<book-id>.json`; raw PDF text and per-chunk model output stay in gitignored `scratch/`. Chunk-level content-hash caching makes re-runs resumable and idempotent; `--overwrite` required to replace an existing output file.

Produced one real draft: `tools/local_extraction/output/six-pillars-of-self-esteem.json` (176 principles across all 21 chapters, real 60-word `core_thesis`, `extraction_method: local_model`, `review_status: pending_review`). **Not yet human-reviewed** — per architecture §2, that review is mandatory before this draft (or any `local_model` draft) is trusted for anything downstream. Known weakness: `llama3.2:3b` is weak at deduplication and at generating specific `applies_to_tags` (many are still generic placeholders) — worth a stronger model for the aggregation pass specifically before treating tag quality as good.

### Draft Extraction Submission Endpoint — ✅ BUILT (2026-07-30)

`POST /api/ingestion/draft-submissions` accepts the same JSON shape the local extraction tool (or a human editor) produces and stores it as `review_status=pending_review` — this is the "hand a structured draft into the existing Draft Extraction step" endpoint architecture §8 calls for. See §8 below (API Routes) and §9.11 (design decisions) for details: gated by a static `X-Ingestion-Api-Key` header (fails closed if unconfigured), rejects resubmission of an existing `book_id` with 409 unless `?replace=true` is passed. Added a `books.extraction_method` column (migration `002_extraction_method.py`) that Phase 1 had missed despite being in architecture §4's schema.

**Review/publish is now scripted** (no HTTP endpoint yet): `backend/scripts/mark_reviewed.py <book_id>` flips `review_status` to `human_reviewed` on the book and all its principles, increments `version`, and generates embeddings for every principle via Voyage AI — see the next section.

### Embedding Generation + Retrieval Step A (architecture §3) — ✅ BUILT (2026-07-30)

- **`app/embeddings.py`** — `EmbeddingClient` protocol; `VoyageEmbeddingClient` (real, calls Voyage AI's `/v1/embeddings` endpoint via stdlib `urllib`, no new HTTP dependency); `FakeEmbeddingClient` (deterministic hash-seeded unit vectors, records `texts_seen`/`input_types_seen`, no network — same injectable-client pattern as `tools/local_extraction`). `generate_embeddings_for_book(db, book_id, client)` embeds each principle's `name + summary + tags` and stores the vector directly on `principles.embedding` (pgvector column), setting `embedding_id = principle_id` since there's no external vector store in this design — embeddings live in the same Postgres instance.
- **`app/retrieval.py`** — Step A, no LLM call anywhere in it:
  - `tag_match_scores(entries, principles)` — pure function, case-insensitive overlap between logged `category` values and `applies_to_tags`.
  - `embedding_candidates(db, book_id, query_vector)` — pgvector cosine-distance nearest-neighbor query, scoped to one `book_id` and `review_status=human_reviewed` only (an unreviewed draft can never surface here).
  - `rank_fusion(tag_scores, embedding_scores, top_k, tag_weight)` — pure function; tag hits weighted 2x over embedding similarity, matching architecture §3's "tag-match hits weighted higher, since they're precise."
  - `retrieve_principles(db, book_id, entries, embedding_client, mood, top_k)` — full orchestration, returns top `principle_id`s.
- **Migration `003_principle_embedding.py`** — adds `principles.embedding vector(1024)` (1024 = Voyage `voyage-3`'s output dimension, see `settings.embedding_dimension`) + an ivfflat cosine-distance index (`lists=10`, tuned for the current small corpus — revisit past a few thousand principles).
- **32 new tests** (`test_embeddings.py`, `test_retrieval.py`): fake-client unit tests for the embedding client itself, `generate_embeddings_for_book` against real Postgres, pure-function tests for tag matching/rank fusion with no DB at all, and Postgres-backed tests proving `embedding_candidates` is correctly scoped per-book and excludes `pending_review`/un-embedded principles.
- **Live smoke test passed (2026-07-30):** ran `scripts/mark_reviewed.py six-pillars-of-self-esteem` for real against the actual Voyage AI API — all 176 principles embedded successfully (`review_status=human_reviewed`, `version=3`). Queried `retrieve_principles()` with a real journal-style entry ("spent the afternoon second-guessing a decision... feeling like I am not good enough", category `self-doubt`) and got genuinely relevant results back: `self-acceptance-as-precondition-for-change`, `pride-is-not-about-being-without-flaws-but-about-acknowledging-and-accepting-them`, `self-esteem-is-not-supported-by-secondhand-values-but-by-living-one-s-own-mind-judgment-and-values`, among others. Confirms the full path (Postgres pgvector cosine query + real embeddings) actually works, not just against fixtures.
- **One real gotcha hit during this:** the first live attempt 403'd — the Voyage AI dashboard and MongoDB Atlas can produce similarly-shaped keys, and MongoDB Atlas keys get rejected by Voyage's `/v1/embeddings` endpoint with a somewhat confusing error. If `VOYAGE_API_KEY` ever 403s, check it's genuinely from `dashboard.voyageai.com`, not Atlas.
- **Not built:** anything that calls this from an actual API route (no `/api/analyses` endpoint yet) — retrieval is a callable, independently-tested module, per architecture §1's "each independently callable and independently mockable," waiting on Step B (synthesis) to have something to feed into.

### MVP Completion: Synthesis, Verification, Orchestration, Users/Suggestions/Streaks, Frontend — ✅ BUILT (2026-07-31)

Everything needed for a working end-to-end personal loop is now in place:

- **`app/llm.py`** — `LLMClient` protocol; `OllamaLLMClient` (real, same "localhost-only" refusal pattern as `VoyageEmbeddingClient`/`OllamaModelClient`); `FakeLLMClient` (queued canned responses, records prompts, for tests). **Uses the local Ollama model already installed for extraction, not the Claude API architecture originally specified — see 9.14 for why, approved by the user for this MVP.**
- **`app/synthesis.py`** (Step B) — `build_synthesis_prompt()` (the exact skeleton from architecture §3, now real code), `synthesize_analysis()` (one LLM call, no rule enforcement here by design), `fallback_analysis()` (non-LLM: surfaces the retrieved principles' own text directly, used when verification can't be satisfied).
- **`app/verification.py`** (Step C) — rule-based checks (unknown `principle_id`, quote length/count via the same `app/constraints.py` helpers used elsewhere) + one LLM entailment call ("does this claim anything not in the principles?"); fails closed on any ambiguous/unparseable verifier response.
- **`app/routers/analyses.py`** — `POST /api/analyses {log_id}` orchestrates Step A → B → C: retrieve, synthesize, verify; on failure, retry synthesis once with a stricter prompt; if still failing, fall back to the template. Never returns unverified LLM output. `llm_client`/`embedding_client` are FastAPI dependencies (`get_llm_client`/`get_embedding_client`), overridable in tests with fakes — same pattern as `get_db`.
- **`app/routers/users.py`** — `POST /users` (no email/PII collected at all — see 9.16), `GET /users/{id}`, `PUT /users/{id}/active-book`.
- **`app/routers/suggestions.py`** — `GET /suggestions?analysis_id=`, `PUT /suggestions/{id}` (mark done/skipped).
- **`app/streaks.py`** + **`app/routers/streaks.py`** — `GET /streaks?user_id=&book_id=`, computed synchronously from `daily_logs` history on each request (see 9.15), upserted into `streaks_progress` for the frontend to read as a cache.
- **`frontend/`** — minimal Next.js (TypeScript, App Router) single-page app: book picker, daily log form, displays analysis + suggestions with Done/Skip buttons, shows streak. Talks only to the existing REST API via `fetch()`, no state library. `npm audit`: 0 vulnerabilities (see 9.17 — Next's bundled transitive `postcss`/`sharp` needed an `overrides` pin).
- **31 new backend tests** (`test_synthesis.py`, `test_verification.py`, `test_streaks.py`, `test_streaks_route.py`, `test_users.py`, `test_analyses.py`) — 74 total, all passing. (85 as of 2026-08-03's reflection/explanation reshape + same-day resubmit fix, see below.)
- **Live end-to-end smoke test (2026-07-31):** real Postgres, real Voyage AI retrieval, real Ollama synthesis+verification, run via curl exactly mirroring the frontend's calls — create user → set active book → create daily log → create analysis → list suggestions → mark one done → check streak. All steps worked. The synthesis attempt fell back to the template both times in this specific run (llama3.2:3b's known weakness at strict grounded-JSON output under the retry-tightened prompt) — this is Step C's safety net working exactly as designed, not a bug: the fallback response was still coherent and grounded, since it's just the principles' own already-review-passed text.

### Reflection + Suggestion Output Reshape — ✅ BUILT (2026-08-03)

User requested the daily output be restructured: reflection becomes a list of distinct, situation-grounded points instead of one paragraph; suggestions become a hard-capped "top 3 tips for tomorrow", each with a stored explanation.

- **Migration `004_reflection_and_explanation.py`** — drops `analyses.analysis_text` (`Text`), adds `analyses.reflection` (`JSONB`, `list[str]`, same pattern as `daily_logs.entries`); adds `suggestions.explanation` (`Text`, not null). Dev-only DB — old `analysis_text` values were dropped, not migrated (single trusted local user, trivially regenerated).
- **`app/synthesis.py`** — prompt now asks for `{"reflection": [...], "suggestions": [{"text","principle_id","explanation"}]}`; each reflection string must reference what the user actually logged (mood/entries), not restate the principle generically. `parse_synthesis_response()` validates the new shape (non-empty list of non-empty strings; every suggestion needs a non-empty `explanation` too). `fallback_analysis()` turns each retrieved principle into one verbatim reflection string and slices `principles[:3]` for suggestions, with a static non-LLM explanation string.
- **`app/verification.py`** — signature is now `verify_analysis(client, reflection: list[str], suggestions, principles)`. New rule check: `len(suggestions) > 3` fails closed (`MAX_SUGGESTIONS = 3`) — the "top 3" cap is rule-enforced, not prompt-trusted, matching this project's existing quote-limit philosophy (§3.3). Quote-scanning now covers every reflection point plus every suggestion's `text` and `explanation`. Entailment prompt checks explanations for invented claims too.
- **`app/routers/analyses.py`** — stores `reflection=result["reflection"]` and `Suggestion(..., explanation=s["explanation"])`.
- **Frontend** — `api.ts` types updated (`Analysis.reflection: string[]`, `Suggestion.explanation: string`); `page.tsx` renders reflection as a bulleted list and suggestions section as "Top 3 tips for tomorrow" with each tip's explanation shown beneath it.
- **9 new/updated backend tests** across `test_synthesis.py`, `test_verification.py`, `test_analyses.py` — 83 total, all passing.
- **Live smoke test (2026-08-03):** real Postgres + real Ollama via curl — reflection came back as a genuine list, suggestions capped at exactly 3 with `explanation` populated, Done/Skip round-trips the new column correctly.
- **Known pre-existing data issue, not caused by this change:** two `six-pillars-of-self-esteem` principles (`know-thyself`, `separate-facts-from-feelings`) have an empty `summary` in the dev DB. `fallback_analysis()` uses `p.summary` verbatim for suggestion `text`, so a fallback suggestion for either of those principles has blank `text` — the fallback path predates and bypasses `parse_synthesis_response()`'s emptiness checks (those only run on real LLM output). Worth fixing the underlying empty summaries (likely from the known local-extraction weakness, §5) or adding a fallback-side guard, but out of scope for this task.

### Same-day resubmit fix — ✅ BUILT (2026-08-03)

Discovered right after the reshape above: clicking "Get today's reflection" a second time in the same calendar day 409'd with "Daily log already exists for user/date" — `daily_logs` has a `UNIQUE(user_id, date)` constraint (§4/§7) and `page.tsx`'s `handleSubmit` always POSTed a brand-new log. Fixed by making the frontend upsert instead of blindly creating:

- **`app/routers/analyses.py`** — new `DELETE /api/analyses/{analysis_id}` (204, 404 if missing). Cascades to the analysis's suggestions via the existing `ON DELETE CASCADE` FK (§9.8) — needed because `analyses.log_id` is unique, so regenerating a reflection for a log that already has one requires clearing the old row first, there's no update-in-place endpoint.
- **`frontend/app/api.ts`** — added `listDailyLogs`, `updateDailyLog`, `listAnalysesForLog`, `deleteAnalysis`.
- **`frontend/app/page.tsx`** `handleSubmit` — now checks `listDailyLogs(user_id)` for a row matching today's date first. If found: appends the new entry to that row's existing `entries` array via `PUT /daily-logs/{id}` (mood/chosen_book_id also refreshed to the current form state), deletes any analysis already attached to that log, then creates a fresh one covering all of today's entries. If not found: creates a new log as before.
- **2 new backend tests** (`test_delete_analysis_allows_regenerating_for_same_log`, `test_delete_analysis_404_for_unknown_id`) — 85 total, all passing.
- **Live smoke test (2026-08-03):** reproduced the exact failing scenario against the real dev DB (a user who'd already logged once today) — appended a second entry, deleted the stale analysis, regenerated a new one reflecting both entries, confirmed the old analysis's suggestions were gone (cascade) and the new ones were correct. No 409.

### Fallback template: unclear wording + reflection/suggestion redundancy — ✅ FIXED (2026-08-03)

Two issues reported after using the app for real, both traced to `fallback_analysis()` in `app/synthesis.py` (the non-LLM template used whenever Step C's verification fails — which is common with `llama3.2:3b`, see 9.14): (1) reflection points read `"From 'X': ..."`, where `X` is a principle name — easy to mistake for a chapter title, since `principles.source_chapter` is a distinct field users never see; (2) the reshape earlier today (see above) had reflection points show the principle's full `summary`, and suggestions for the first 3 principles *also* showed that same `summary` verbatim as their `text` — genuine copy-pasted duplication between the two sections, not intentional design, just an oversight in how the reshape split the fields.

Fixed by making reflection and suggestions draw from disjoint content instead of both repeating `p.summary`:
- Reflection points now only name the idea: `"This connects to an idea from the book: {p.name}."` — no summary text, unambiguously "from the book."
- Suggestions (still capped at top 3, see above) keep `text = p.summary` — the actual grounded content now lives in exactly one place.
- Also added a line to the real synthesis prompt (`SYNTHESIS_PROMPT_TEMPLATE`) telling the LLM not to let a suggestion just restate a reflection point, as a defensive measure for the (less common) case where Ollama actually succeeds.
- 1 new test (`test_fallback_analysis_reflection_does_not_duplicate_suggestion_text`) — 86 total, all passing. Verified live against the real dev DB: regenerated a fallback analysis, confirmed reflection and suggestion text no longer overlap.

**Superseded a few minutes later** by the paragraph reshape below — the "one line per principle" list format (even with the fix above) still read as repetitive once there were 4-5 retrieved principles, since every line started with the same `"This connects to an idea from the book: ..."` preamble.

### Reflection becomes a single paragraph + principle chips UI — ✅ BUILT (2026-08-03)

User feedback after using the app for real: the per-principle reflection lines were still monotonous ("This connects to an idea from the book: X" repeated 5 times), and wanted the related principles shown as distinct bordered/hoverable chips ("your situation relates to these principles") with a single prose paragraph assessing the situation underneath, instead of a list. This is the third iteration on the reflection field's shape today — each round narrowed based on actually looking at real output, not a plan drawn up in advance.

- **Migration `005_reflection_paragraph.py`** — `analyses.reflection` changes from `JSONB` (`list[str]`) back to plain `Text` (single string). Dev-only DB, old rows' `reflection` values dropped again (same reasoning as migration `004`).
- **`app/synthesis.py`** — prompt's `"reflection"` field is now one paragraph (3-5 sentences) that assesses the day's situation and names which principles it connects to, not a list. `parse_synthesis_response()` validates it as a non-empty string. `fallback_analysis()` now builds one grammatically-joined sentence naming all retrieved principles (e.g. "these ideas from the book connect to your situation: A, B, and C") instead of one list item per principle — closer to the *original* pre-reshape fallback text than either of today's two earlier attempts, which is a good sign this is the right shape. Suggestions are unchanged (still top-3, still carry `p.summary` verbatim, still the only place that text appears).
- **`app/verification.py`** — `verify_analysis()`/`_rule_based_issues()`/`_build_entailment_prompt()` take `reflection: str` again (reverted from `list[str]`); quote-scanning treats it as one labeled block instead of one label per point.
- **Frontend** — `api.ts`: `Analysis.reflection: string`; added `Principle` type + `getPrinciple(id)` (reuses the existing read-only `GET /principles/{id}`, no new backend endpoint needed for this part). `page.tsx`: after an analysis loads, fetches principle names for every `retrieved_principle_id` in parallel and renders them as `.principle-chip` pills under "Your situation relates to these principles:", with the paragraph reflection shown in a bordered `.reflection-summary` block below. Also reworded the fallback disclaimer inline (see next paragraph).
- **`globals.css`** — added `.principle-chips` (flex row), `.principle-chip` (bordered pill, background/border-color transition on `:hover`), `.reflection-summary` (bordered block for the paragraph).
- Test suite updated for the new `str` shape (list-literal reflection args throughout `test_synthesis.py`/`test_verification.py`/`test_analyses.py` became plain strings) — 87 total, all passing. Live-verified against the real dev DB: reflection now renders as one non-repetitive paragraph; principle names resolve correctly via `GET /principles/{id}` for the chips.
- **Also answered directly in the UI:** the fallback disclaimer text (both the reflection one and each suggestion's static explanation) exists because `llama3.2:3b` frequently fails Step C's strict grounding/JSON checks (documented weakness, 9.14) and the app is fail-closed by design (9.14/architecture §3) — it will never show unverified LLM output, so it falls back to the deterministic, principle-text-only template instead. This is expected, not a bug; the disclaimer text in the UI was reworded to say so explicitly rather than just noting that verification failed.

### Root-caused and fixed why synthesis almost always fell back — ✅ FIXED (2026-08-03)

User pushed back on the fallback being the near-permanent state, asking either for the LLM to actually stop failing or for verification to be recalibrated — not for the safety checks to be weakened. Investigated live against the real Ollama model (not guessed) and found **two distinct, fixable bugs**, neither of which required loosening what verification actually protects against:

1. **Prompt formatting bug (root cause of most rule-based failures):** `_principles_block()` in `app/synthesis.py` listed each principle as `- id: teacher-confidence-matters`. `llama3.2:3b` was pattern-matching the whole line and echoing back the literal string `"id: teacher-confidence-matters"` (prefix included) as the `principle_id` value in suggestions — which then failed the unknown-`principle_id` rule check on essentially every generation. Fixed by relabeling the field to `principle_id:` (matching the exact output JSON key) and quoting the value — confirmed via a live script (`OllamaLLMClient` against the real model) that this alone fixed the id-echoing.
2. **Entailment check was over-strict for what a "suggestion" is supposed to be:** the entailment prompt asked "does anything state a fact/claim/advice not supported by the principles" — but a suggestion is supposed to translate a principle into a *concrete* action (e.g. "take a few deep breaths"), which by definition adds specificity beyond the principle's own wording. The old prompt was failing suggestions for exactly the kind of concreteness the schema asks for. Reworded `ENTAILMENT_PROMPT_TEMPLATE` (`app/verification.py`) to fail only on **misattribution/contradiction** of what the principles actually say (reflection + each suggestion's `explanation` still held to that standard) while explicitly allowing a suggestion's `text` to be a concrete tactic not written verbatim in the principle — this is a scope correction, not a loosening of the actual grounding guarantee (no invented book claims, still fail-closed on anything ambiguous).

Also, while investigating: added `"format": "json"` to `OllamaLLMClient.generate()`'s request (`app/llm.py`) so Ollama enforces syntactically valid JSON on every call, and changed the entailment call itself to require `{"verdict": "PASS"|"FAIL"}` JSON (was a bare "PASS"/"FAIL" word) instead of substring-matching free text — more reliable now that format is enforced, and removes a source of ambiguity. `_RETRY_REMINDER` also updated to spell out the exact `principle_id` format expected.

**Live-verified the fix, not just unit tests:** ran repeated real trials against the actual local Ollama model (not `FakeLLMClient`) with a realistic 5-principle scenario matching real retrieval's `top_k=5` — went from near-constant fallback to 5/5 passing (matching the real app's retrieve-then-retry flow), including one full real API call through `POST /api/analyses` against the dev DB that came back `"verification_status": "passed"` with a reflection genuinely referencing the user's actual logged entries (not the fallback template) and three distinct, non-duplicated suggestions. `MAX_SYNTHESIS_ATTEMPTS` stayed at 2 — every live trial passed on the first attempt, no evidence more retries were needed.

Test suite: existing `FakeLLMClient` canned responses for the entailment step were bare `"PASS"`/`"FAIL"` strings — updated to the new `'{"verdict": "PASS"}'` JSON shape throughout `test_verification.py`/`test_analyses.py`, plus 2 new tests (`test_verify_analysis_fails_closed_on_unexpected_verdict_value`, renamed the ambiguous-response test for clarity) — 88 total, all passing.

### Product shift: daily journal → independent per-situation advice — ✅ BUILT (2026-08-03)

**User-driven redesign, discussed before building** (see the "think with me" framing): Lens's original design was a daily-journal loop — one `daily_logs` row per `(user_id, date)` (architecture §4's own schema decision), entries accumulated into that row throughout the day, one shared reflection generated across everything logged that day. The user wants a different product: "I need some advice, how would the author advise me in this situation?" — asked as many times a day as needed, with each ask a **fully independent** situation, never merged with any other ask that day.

**What actually had to change, and what didn't:**
- **Migration `006_drop_daily_log_date_unique.py`** — drops `uq_daily_logs_user_date`. This was the one thing forcing same-day asks to merge. `analyses.log_id` was already unique 1:1 with a `daily_logs` row, which already matches "one situation → one independent advice session" with zero change needed there.
- **`app/streaks.py`'s `compute_streaks()` needed no change at all** — it already dedupes via `sorted(set(log_dates))`, so multiple situations sharing a date don't affect streak counting; a streak still means "at least one situation logged this day."
- **Net simplification, not addition:** the "same-day resubmit" upsert logic built earlier the same day (check for today's existing log → append entry → delete stale analysis → regenerate) existed *specifically* to work around the now-removed constraint. `frontend/app/page.tsx`'s `handleSubmit` reverted to the simple flow — create one `daily_logs` row with just this situation's entry, create its own analysis, done. The api.ts helpers that only existed for that upsert flow (`listDailyLogs`, `updateDailyLog`, `listAnalysesForLog`, `deleteAnalysis`) were removed as now-dead code — net fewer lines than before. (The backend `DELETE /api/analyses/{id}` endpoint itself was kept; it's a reasonable general capability even with no current frontend caller.)
- **UI copy rewritten** from daily-journal framing to situation framing: "Log your day..." → "Describe a situation, get advice... ask as many times a day as you like"; "What happened today?" → "What's the situation? What do you need advice on?"; "Get today's reflection" → "Get advice"; "Top 3 tips for tomorrow" → "Top 3 tips" (no longer day-scoped). Mood is kept, per the user's explicit choice, reframed as "How are you feeling about this?" rather than a whole-day snapshot. The form now resets (`action`/`category`/`mood`) after each successful ask so the next one reads as a new, unrelated situation.
- **`app/routers/daily_logs.py`**: the `IntegrityError` handler on `POST` used to say "Daily log already exists for user/date" — no longer true, reworded to "Daily log write conflict (e.g. unknown user or book)" since FK violations are the only realistic remaining cause.
- **Tests**: `tests/test_constraints.py::test_daily_logs_unique_user_id_date_rejected_by_db` was deleted (the behavior it asserted is intentionally gone) and replaced with `test_daily_logs_allows_multiple_rows_same_user_and_date`, a positive test for the new behavior — 88 total, unchanged count (one swapped for another).
- **Deliberately deferred, not forgotten:** user wants a "past interactions" history view for signed-in users in a later version. Nothing here blocks that — `GET /daily-logs?user_id=` and `GET /analyses?log_id=` already exist and already support building that view later with no further schema changes; no history UI was built now.

**Live-verified:** logged two unrelated real situations (a work conflict, a family argument) for the same user on the same date via the actual API — no 409, two separate `daily_logs` rows, and two analyses that came back completely independent (one grounded in workplace-competence principles, one in parenting principles) with zero cross-contamination between them.

**This is a real deviation from `docs/Architecture.md` §4's original `daily_logs` schema** (which specified unique `(user_id, date)`) — flagged here the same way other approved deviations are (9.14/9.16/9.17), since architecture's own "do not deviate without user approval" rule applies and approval was given directly in this conversation.

### Phase 2+ — NOT STARTED (out of scope for this MVP, deliberately)

- Real auth (Auth0/Clerk) — see 9.16 for the MVP's no-PII, no-password alternative
- A review/publish UI (currently scripted via `mark_reviewed.py`, see §13)
- Redis/RQ background jobs (streaks are computed synchronously instead, fine at this scale, see 9.15)
- Multi-book UI polish, suggestion history/trends view, prior-day suggestion context actually wired into the synthesis prompt's "PRIOR SUGGESTIONS STATUS" field (currently always empty — `synthesize_analysis()` supports it as a parameter, but `analyses.py` doesn't look up yesterday's suggestions yet)

---

## 6. Repository Layout (current)

```
LENS/
├── initial_prompt.md            # Original design prompt
├── docs/
│   ├── Architecture.md          # Architecture source of truth
│   ├── HANDOFF.md               # This file
│   └── PHASE_1_AUDIT.md
├── books/                       # Source PDFs for local extraction (gitignored raw text stays local)
├── backend/
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/
│   │       ├── 001_initial_schema.py
│   │       └── 002_extraction_method.py     # adds books.extraction_method
│   ├── alembic.ini
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py              # FastAPI app, mounts routers at /api
│   │   ├── config.py            # Settings; DATABASE_URL, INGESTION_API_KEY from env
│   │   ├── database.py          # SQLAlchemy engine, SessionLocal, Base, get_db
│   │   ├── constraints.py       # count_words(), find_quotes(), word/quote cap constants
│   │   ├── models/__init__.py   # All 7 ORM models + enums (incl. ExtractionMethod)
│   │   ├── schemas/__init__.py  # Pydantic read/write schemas + DraftSubmissionCreate
│   │   └── routers/
│   │       ├── books.py         # GET only
│   │       ├── principles.py    # GET only
│   │       ├── daily_logs.py    # GET/POST/PUT/DELETE
│   │       └── ingestion.py     # POST /ingestion/draft-submissions (API-key gated)
│   ├── tests/
│   │   ├── conftest.py          # DB fixtures, seed data, TestClient
│   │   ├── test_book_word_cap.py
│   │   ├── test_constraints.py
│   │   ├── test_ingestion.py
│   │   ├── test_principle_word_cap.py
│   │   └── test_routes.py
│   ├── docker-compose.yml       # pgvector/pgvector:pg16
│   ├── requirements.txt
│   ├── pytest.ini
│   └── .gitignore
└── tools/
    └── local_extraction/        # Standalone CLI, architecture §2 Path B — never imported by backend/
        ├── src/local_extraction/ # chunking, extraction, aggregation, validation, model_client, pipeline, cli
        ├── tests/                # 110 tests, fake model client, no real LLM required
        ├── output/               # Validated draft JSON only (six-pillars-of-self-esteem.json)
        ├── scratch/              # gitignored: raw PDF text, per-chunk cache, raw aggregation output
        └── README.md
```

**Not present:** frontend/, CI config, .env (use defaults or create locally). **No git repository initialized yet** anywhere in this project.

---

## 7. Data Model (§4) — Implementation Mapping

### books

| Field | Type | Implementation notes |
|---|---|---|
| book_id | string PK | Slug, e.g. `"atomic-habits"` |
| title, author | string | |
| core_thesis | text | **CHECK `word_count <= 150`** |
| tone | enum string | pragmatic \| philosophical \| spiritual \| scientific \| narrative |
| tracked_metrics | JSONB | `[{id, label, description}]` |
| review_status | enum string | human_reviewed \| pending_review |
| extraction_method | enum string | human_written \| local_model — added in migration `002_extraction_method.py` (was missing from Phase 1 despite being in architecture §4) |
| version | int | default 1 |
| created_at, updated_at | timestamptz | |

### principles

| Field | Type | Implementation notes |
|---|---|---|
| principle_id | string PK | Slug |
| book_id | FK → books | ON DELETE CASCADE |
| name, summary | string/text | **summary CHECK `word_count <= 200`** |
| source_chapter | string nullable | |
| applies_to_tags | text[] | |
| embedding_id | string nullable | Set to `principle_id` once embedded — no external vector store, just marks "has an embedding" |
| embedding | `vector(1024)` nullable | Added in migration `003_principle_embedding.py`; ivfflat cosine index; populated by `app/embeddings.py` |
| review_status | enum | |
| created_at, updated_at | timestamptz | |

### users

| Field | Type | Notes |
|---|---|---|
| user_id | UUID PK | |
| email_encrypted | bytea | Not encrypted yet — placeholder bytes in tests |
| auth_provider_id | string unique | |
| active_book_id | FK → books nullable | ON DELETE SET NULL |
| timezone | string | default UTC |
| created_at | timestamptz | No updated_at (matches §4) |

### daily_logs

| Field | Type | Notes |
|---|---|---|
| log_id | UUID PK | |
| user_id | FK → users | ON DELETE CASCADE |
| date | date | **No longer unique** — migration `006` dropped `UNIQUE (user_id, date)`; a user can have multiple independent situations (rows) per day, see §5 "Product shift" |
| chosen_book_id | FK → books | ON DELETE RESTRICT |
| entries | JSONB | `[{time, action, category}]` — one situation's entry per row now, not accumulated across a day |
| mood | int nullable | CHECK 1–5 |
| created_at, updated_at | timestamptz | |

### analyses

| Field | Type | Notes |
|---|---|---|
| analysis_id | UUID PK | |
| log_id | FK → daily_logs unique | 1:1 relationship |
| retrieved_principle_ids | text[] | |
| reflection | JSONB (`list[str]`) | **Changed 2026-08-03** (migration `004`) — was `analysis_text` (`Text`, one paragraph). Now a list of 2-4 self-contained reflection strings, each explicitly grounded in the day's logged entries/mood, not a generic restatement of the principle. |
| verification_status | enum | passed \| fallback_used |
| created_at | timestamptz | |

### suggestions

| Field | Type | Notes |
|---|---|---|
| suggestion_id | UUID PK | |
| analysis_id | FK → analyses | ON DELETE CASCADE |
| principle_id | FK → principles | ON DELETE RESTRICT |
| text | text | |
| explanation | text | **New 2026-08-03** (migration `004`) — one sentence on why the tip follows from the principle. Rule-enforced in Step C: non-empty, quote-checked same as `text`. |
| status | enum | pending \| done \| skipped |
| created_at, resolved_at | timestamptz | resolved_at nullable |

### streaks_progress

| Field | Type | Notes |
|---|---|---|
| streak_id | UUID PK | |
| user_id, book_id | FKs | |
| metric_id | string | References book.tracked_metrics[].id |
| current/longest_streak_days | int | |
| trend_series | JSONB | `[{date, score}]` |
| updated_at | timestamptz | **UNIQUE (user_id, book_id, metric_id)** |

### DB helper: `word_count(text)`

PostgreSQL immutable SQL function created in migration:

```sql
CREATE OR REPLACE FUNCTION word_count(input_text text) RETURNS integer ...
-- splits on whitespace via regexp_split_to_array(trim(input_text), E'\s+')
```

Used by CHECK constraints:
- `ck_books_core_thesis_word_cap`: `word_count(core_thesis) <= 150`
- `ck_principles_summary_word_cap`: `word_count(summary) <= 200`

---

## 8. API Routes (Phase 1)

Base URL prefix: `/api`

| Method | Path | Access | Status |
|---|---|---|---|
| GET | `/books` | Read-only | ✅ |
| GET | `/books/{book_id}` | Read-only | ✅ |
| GET | `/principles` | Read-only; `?book_id=` filter | ✅ |
| GET | `/principles/{principle_id}` | Read-only | ✅ |
| GET | `/daily-logs` | Read; `?user_id=` filter | ✅ |
| GET | `/daily-logs/{log_id}` | Read | ✅ |
| POST | `/daily-logs` | Write | ✅ |
| PUT | `/daily-logs/{log_id}` | Write | ✅ |
| DELETE | `/daily-logs/{log_id}` | Write | ✅ |
| POST | `/ingestion/draft-submissions?replace=` | Write, `X-Ingestion-Api-Key` header required | ✅ |
| POST | `/users` | Write, no PII collected | ✅ |
| GET | `/users/{user_id}` | Read | ✅ |
| PUT | `/users/{user_id}/active-book?book_id=` | Write | ✅ |
| POST | `/analyses` | Write — runs retrieval → synthesis → verification | ✅ |
| GET | `/analyses/{analysis_id}` | Read | ✅ |
| GET | `/analyses?log_id=` | Read | ✅ |
| DELETE | `/analyses/{analysis_id}` | Write | ✅ (added 2026-08-03, see below) |
| GET | `/suggestions?analysis_id=` | Read | ✅ |
| PUT | `/suggestions/{suggestion_id}` | Write (mark done/skipped) | ✅ |
| GET | `/streaks?user_id=&book_id=` | Read (computed synchronously) | ✅ |
| GET | `/health` | Health check | ✅ |

**Read-only enforcement:** books/principles have **no POST/PUT/DELETE route handlers**. Unregistered methods return HTTP 405 from FastAPI/Starlette.

**Draft Extraction submission** (architecture §2/§8): accepts `{book_id, title, author, core_thesis, tone, tracked_metrics, extraction_method, principles: [...]}` — same shape the local extraction tool or a human editor produces. Always writes `review_status=pending_review` regardless of what's in the payload (no way to publish directly through this endpoint). `book_id` collision → 409 unless `?replace=true`. Requires `X-Ingestion-Api-Key` header matching `INGESTION_API_KEY` env var; 503 if that env var isn't set at all (fails closed, never silently open). See §9.11.

**`POST /analyses`**: 404 if `log_id` doesn't exist, 409 if an analysis already exists for that log (one analysis per daily log), 400 if the log's book isn't `human_reviewed`, 422 if retrieval returns nothing. See §12 and §9.14/§9.15 for the synthesis/verification/streak design.

CORS is scoped to `http://localhost:3000` only (`app/main.py`, `settings.cors_allow_origins`) — not `*` — since this API now has real write endpoints reachable from a browser.

---

## 9. Important Design Decisions (with reasons)

### 9.1 Word caps at DB + Pydantic, not app-only

**Decision:** PostgreSQL CHECK constraints using `word_count()` function, mirrored in Pydantic `BookWriteBase` / `PrincipleWriteBase`.

**Reason:** User constraint — caps must not be bypassable via raw SQL or ORM bypass. Application-only validation is insufficient.

**Constants:** `CORE_THESIS_MAX_WORDS = 150`, `PRINCIPLE_SUMMARY_MAX_WORDS = 200` in `app/constraints.py`.

### 9.2 Read-only catalogue via absent routes, not middleware

**Decision:** Books/principles routers only define GET handlers.

**Reason:** Catalogue ingestion is admin/offline per architecture §2. Runtime users must not mutate book knowledge. Missing routes → 405 is acceptable but audit wants explicit POST/PUT rejection tests.

### 9.3 Book/principle IDs are string slugs, not UUIDs

**Decision:** `book_id` and `principle_id` are human-readable strings (e.g. `"atomic-habits"`).

**Reason:** Matches architecture §4 examples; aids retrieval debugging and golden-set tests.

### 9.4 pgvector extension enabled early, no embedding column yet — SUPERSEDED by 9.12

**Original decision (Phase 1):** Migration runs `CREATE EXTENSION IF NOT EXISTS vector` but principles table only has `embedding_id` string field; vector column deferred to retrieval phase.

**Status:** the vector column now exists (migration `003`) — see 9.12 for the retrieval-phase decision that superseded this.

### 9.5 Enum values stored as strings in Postgres

**Decision:** SQLAlchemy enums (`BookTone`, `ReviewStatus`, etc.) mapped to `String` columns, not native PG ENUM types.

**Reason:** Simpler migrations; values match architecture JSON literals exactly.

### 9.6 Test DB uses transaction rollback per test

**Decision:** `conftest.py` truncates tables then wraps each test in a connection-level transaction that rolls back.

**Reason:** Test isolation without recreating DB. Requires Postgres (not SQLite).

### 9.7 Migrations opt-in per test via `db_session` fixture

**Decision:** `migrated_database` fixture is **not** autouse; only tests requesting `db_session` trigger Alembic upgrade.

**Reason:** Allows Pydantic-only unit tests to run without Postgres. Changed from initial autouse after first implementation.

### 9.8 FK delete behaviors

| Relationship | ON DELETE | Reason |
|---|---|---|
| principles → books | CASCADE | Principles belong to book |
| users.active_book_id → books | SET NULL | User survives book removal |
| daily_logs → users | CASCADE | Logs are user-owned |
| daily_logs.chosen_book_id → books | RESTRICT | Prevent deleting book with logs |
| suggestions → principles | RESTRICT | Preserve suggestion integrity |
| analyses → daily_logs | CASCADE | Analysis is derived from log |

### 9.9 Internal write schemas not exposed on API

**Decision:** `BookWriteBase` / `PrincipleWriteBase` exist for tests/seeds only.

**Reason:** Catalogue writes happen via admin ingestion pipeline, not public API.

### 9.10 No auth in Phase 1

**Decision:** daily_logs accept raw `user_id` in request body.

**Reason:** Auth deferred; Phase 1 focuses on data model. **Phase 2+ must wire auth provider and stop trusting client-supplied user_id.**

### 9.11 Ingestion endpoint: reject-by-default resubmission + static API-key gate

**Decision:** `POST /ingestion/draft-submissions` rejects a `book_id` that already exists with 409 unless the caller passes `?replace=true` (in which case the book row is updated and all its existing principles are deleted and replaced). The endpoint also requires a static `X-Ingestion-Api-Key` header checked against `INGESTION_API_KEY`, and fails closed (503) if that env var isn't set.

**Reason:** User-confirmed decisions (asked explicitly since neither was determined by architecture): (1) mirrors the local extraction tool's own `OutputExistsError`/`--overwrite` guard — a previously-submitted draft may still be mid human-review and must never be silently clobbered; (2) this is the first write endpoint with no per-user scoping (unlike daily_logs, which is scoped by user_id) — a single shared-secret header is cheap insurance against this specific write path even though Phase 1 otherwise has no auth (9.10).

**Also added:** `books.extraction_method` column via migration `002_extraction_method.py` — architecture §4 specifies this field but Phase 1's original migration/model omitted it; discovered while building this endpoint since the submission payload needed somewhere to record `human_written` vs `local_model`.

### 9.12 Embeddings stored directly in Postgres via pgvector; embedding_id repurposed

**Decision:** `principles.embedding` is a real `vector(1024)` column (migration `003`) holding the actual Voyage AI embedding, queried directly via pgvector's cosine-distance operator. `embedding_id` is set to the principle's own `principle_id` once embedded, rather than referencing anything external.

**Reason:** Architecture's tech stack (§5) already chose "Postgres + pgvector" as the vector store specifically to avoid a second database at this corpus scale (hundreds–low thousands of rows) — there is no external vector store to reference, so `embedding_id`'s original "may reference external ... store ID" framing (9.4) doesn't apply. Keeping it as a non-null marker (rather than dropping the column) avoids a schema change beyond what architecture already specifies at §4.

### 9.13 Retrieval Step A built as a plain callable module, no endpoint yet

**Decision:** `app/retrieval.py`'s functions are directly callable/testable Python, not wired to any HTTP route.

**Reason:** Architecture §1 requires retrieval, synthesis, and verification to be "independently callable and independently mockable" — building Step A in isolation and testing it against real Postgres/pgvector (with a fake embedding client) satisfies that without needing Step B (synthesis) to exist first. An actual `/api/analyses`-style endpoint should wire all three steps together once Step B exists — building it earlier would mean stubbing out synthesis, which adds little value over just calling `retrieve_principles()` directly in tests.

**Status: superseded** — `/api/analyses` now exists (§8), wiring Step A into B/C exactly as anticipated here.

### 9.14 Synthesis + verification use local Ollama, not Claude API — explicit deviation, user-approved

**Decision:** `app/llm.py`'s `OllamaLLMClient` calls the same local `llama3.2:3b` model (via Ollama on `localhost:11434`) already installed for the local extraction tool, for both Step B (synthesis) and Step C's entailment check — not the Claude API architecture §5 originally specified.

**Reason:** Explicitly asked and approved by the user when scoping the MVP (rejected providing a Claude API key, asked to reuse the already-installed local model instead). This is a real deviation from architecture's stated tech stack and is flagged as such per the "do not deviate without user approval" rule in §4 — approval was given directly, not assumed. Side benefit consistent with the project's existing privacy stance: a user's personal daily-log text and the book principles it's compared against never leave their machine for synthesis either, extending the same locality guarantee the local extraction tool already gives the book PDF. Tradeoff: `llama3.2:3b` is weak at reliably producing strict grounded JSON (already observed during extraction) — expect the fallback template (§12 Step C) to trigger more often than it would with a larger model. Swapping back to Claude API later only requires a new `LLMClient` implementation; `synthesis.py`/`verification.py`/the `/analyses` route don't need to change.

**Update 2026-08-03:** the "expect frequent fallback" tradeoff above turned out to be substantially a prompt-engineering + Ollama-config gap, not an inherent model ceiling — see the "Root-caused and fixed why synthesis almost always fell back" entry in §5. After fixing a principle_id echoing bug, forcing Ollama's `format: "json"`, and rescoping the entailment check to what it should actually be checking, live trials went from near-constant fallback to consistently passing on the first attempt. `llama3.2:3b` is still a small model and fallback will still happen sometimes — but "frequent" was an artifact of the bugs above, not a fixed property of using a 3B local model.

### 9.15 Streaks computed synchronously on request, not via a background job

**Decision:** `GET /streaks` recomputes `current_streak_days`/`longest_streak_days` from `daily_logs` on every call (a plain Python loop over sorted dates, `app/streaks.py:compute_streaks`), and upserts the result into `streaks_progress` as a cache rather than the source of truth.

**Reason:** Architecture's Redis/RQ background-job plan is for a nightly recompute at real scale; at MVP scale (one user, one book, a handful of logs) synchronous computation costs nothing and needs zero new infrastructure. `tracked_metrics` is empty on the only real book so far, so a single synthetic `metric_id="consistency"` (did-you-log-anything-today) is used instead of a per-book-defined metric — revisit once a book actually defines `tracked_metrics`.

### 9.16 No PII collected for MVP users — no email, no password, no real auth

**Decision:** `POST /users` takes only an optional `timezone`; `email_encrypted` is stored as empty bytes; `auth_provider_id` is a server-generated UUID never exposed to the client as a credential. The frontend generates/stores a `user_id` in `localStorage` and sends it as a plain, unauthenticated identifier on every request — continuing Phase 1's already-documented no-auth stance (9.10) rather than half-building auth for an MVP.

**Reason:** Building real auth (Auth0/Clerk per architecture §5) is explicitly out of scope for "least code, just usable" per the user's MVP request, and Phase 1 already established+documented this exact gap. Given no real auth exists, the safer choice is to not collect or store anything sensitive at all (no email, no password) rather than build a half-secured credential system that looks more trustworthy than it is. This is a security-conscious simplification, not an oversight: it directly avoids the failure mode of storing PII behind a door with no real lock. **Must be replaced before any multi-user or public deployment** — this only holds up for a single trusted local user.

### 9.17 Frontend dependency pin: `overrides` for Next.js's bundled `postcss`/`sharp`

**Decision:** `frontend/package.json` pins `postcss@^8.5.25` and `sharp@^0.35.3` via npm's `overrides` field, overriding the versions Next.js 16.2.12 bundles by default.

**Reason:** `npm audit` flagged 3 high-severity CVEs in Next's transitively-bundled `postcss` (XSS/path-traversal via CSS source maps) and `sharp` (libvips CVEs). `npm audit fix --force`'s suggested remediation was downgrading to `next@9.3.3` — a 7-year-old release that would reintroduce far worse, well-known vulnerabilities and break the app entirely, so it was rejected. Pinning the specific patched transitive versions via `overrides` resolves the CVEs (`npm audit` now reports 0 vulnerabilities) without touching the Next.js version. Neither vulnerable package is actually on this app's attack surface yet (`sharp` is only invoked via `next/image`, which isn't used; `postcss` only processes this app's own authored CSS, never attacker-supplied input) — but pinning costs nothing and removes the ambiguity.

---

## 10. Test Suite (current state)

### Files

- `tests/test_principle_word_cap.py`
  - `test_principle_summary_over_word_cap_rejected_by_pydantic` — ✅ runs without Postgres
  - `test_principle_summary_over_word_cap_rejected_by_db` — requires Postgres
  - `test_valid_principle_summary_is_accepted` — requires Postgres (200-word boundary)

- `tests/test_routes.py`
  - `test_books_are_read_only` — POST → 405; requires Postgres for GET
  - `test_principles_are_read_only` — POST → 405; requires Postgres
  - `test_daily_logs_crud` — full CRUD; requires Postgres

- `tests/test_ingestion.py` (2026-07-30) — 11 tests covering `POST /ingestion/draft-submissions`: unconfigured-key → 503, missing/wrong header → 401, new draft → 201 with `pending_review`/`extraction_method`, resubmit without `replace` → 409, resubmit with `replace=true` overwrites principles, core_thesis/quote/duplicate-principle-id/empty-principles validation → 422.

- `tests/test_embeddings.py` (2026-07-30) — 8 tests: `FakeEmbeddingClient` determinism/unit-vector-norm/call-recording, `VoyageEmbeddingClient` rejects an empty API key, `generate_embeddings_for_book` updates every principle's `embedding`/`embedding_id` (Postgres-backed) and no-ops for a book with no principles.

- `tests/test_retrieval.py` (2026-07-30) — 13 tests: `tag_match_scores`/`rank_fusion` as pure functions (no DB, fixed inputs → expected outputs per architecture §3), `embedding_candidates` proven scoped to one `book_id` and excluding `pending_review`/un-embedded principles (Postgres + pgvector), `retrieve_principles` end-to-end with a fake embedding client.

- `tests/test_synthesis.py` (2026-07-31) — 7 tests: JSON parsing/validation of the LLM's raw response (markdown fences, missing fields, malformed JSON), `synthesize_analysis` calls the client and appends the retry reminder correctly, `fallback_analysis` uses principle text verbatim with no LLM call.

- `tests/test_verification.py` (2026-07-31) — 6 tests: clean output passes; unknown `principle_id` rejected; over-length quote rejected; ambiguous/ "FAIL" entailment responses fail closed; rule-based failures short-circuit before the LLM entailment call is ever made (`FakeLLMClient` with an empty response queue proves this).

- `tests/test_streaks.py` (2026-07-31) — 6 pure-function tests for `compute_streaks` (empty input, single day, consecutive run, broken streak, longest-vs-current, yesterday still counts).

- `tests/test_streaks_route.py` (2026-07-31) — 3 tests: `GET /streaks` computes current/longest from real `daily_logs` rows, returns zero with no logs, and is idempotent (same `streak_id`) across repeat calls.

- `tests/test_users.py` (2026-07-31) — 4 tests: create with no PII, 404 on unknown user, set/reject active-book.

- `tests/test_analyses.py` (2026-07-31) — 5 tests: happy path with `FakeLLMClient`/`FakeEmbeddingClient` overrides (via `app.dependency_overrides` on `get_llm_client`/`get_embedding_client` — same pattern as `get_db`), fallback triggers when verification fails twice, duplicate-analysis 409, unknown-log 404, unreviewed-book 400.

### Test gaps — all closed as of the 2026-07-08 audit

| Requirement | Test exists? |
|---|---|
| principle.summary DB word cap | ✅ |
| principle.summary Pydantic cap | ✅ |
| core_thesis DB word cap | ✅ |
| core_thesis Pydantic cap | ✅ |
| daily_logs unique (user_id, date) | ✅ |
| FK: principle → nonexistent book | ✅ |
| books POST rejected | ✅ (405) |
| books PUT rejected | ✅ (405) |
| principles POST rejected | ✅ (405) |
| principles PUT rejected | ✅ (405) |
| daily_logs read/write | ✅ full CRUD |

Files added: `tests/test_book_word_cap.py`, `tests/test_constraints.py`. `tests/test_routes.py` extended with PUT-rejected assertions.

### Last known test run (2026-08-03, clean Postgres via docker compose, after migration 005)

```
88 passed, 2 warnings in 5.28s
```

(11 Phase 1 audit + 11 `test_ingestion.py` + 8 `test_embeddings.py` + 13 `test_retrieval.py` + 16 `test_synthesis.py` + 9 `test_verification.py` + 6 `test_streaks.py` + 3 `test_streaks_route.py` + 4 `test_users.py` + 7 `test_analyses.py`.) Full raw output for the original 11 in `docs/PHASE_1_AUDIT.md` §6. Previous runs: 74 passed (2026-07-31, pre-reshape), 83 passed (2026-08-03, after reshape, before same-day-resubmit fix).

Separately, `tools/local_extraction/` has its own fully independent suite: **110 tests passing**, run via its own venv (`cd tools/local_extraction && pytest`), no Postgres or Ollama required (fake model client, on-the-fly PDF fixtures).

---

## 11. Local Development Setup

```bash
cd backend

# Python env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Postgres (Docker Desktop must be running)
# Docker CLI path on macOS may need:
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
docker compose up -d

# Migrate
alembic upgrade head

# Run API
uvicorn app.main:app --reload

# Run tests (needs Postgres)
pytest -v

# Optional: override DB URL
export TEST_DATABASE_URL=postgresql://lens:lens@localhost:5432/lens
```

**Default DB URL:** `postgresql://lens:lens@localhost:5432/lens` (from `app/config.py`).

**`.env` keys needed** (backend, gitignored): `INGESTION_API_KEY`, `VOYAGE_API_KEY`. `OLLAMA_HOST`/`OLLAMA_MODEL` have working defaults (`http://localhost:11434`, `llama3.2:3b`) — no key needed, but Ollama must be running (`ollama serve`) with that model pulled (`ollama pull llama3.2:3b`).

**Frontend:**
```bash
cd frontend
npm install
cp .env.local.example .env.local   # NEXT_PUBLIC_API_BASE=http://localhost:8000
npm run dev                        # http://localhost:3000
```
Requires the backend running on `localhost:8000` (CORS is scoped to `localhost:3000` only — see 9.16/§8).

---

## 12. Retrieval + Analysis Engine (architecture §3 — ✅ ALL THREE STEPS BUILT)

Kept as **three separate modules**, wired together by `app/routers/analyses.py`, exactly as architecture §1 requires:

### Step A — Retrieval (no LLM) — `app/retrieval.py`

1. Tag match: log entry `category` vs `principles.applies_to_tags` — `tag_match_scores()`
2. Embedding similarity: day's log text embedded, cosine similarity vs principle summaries (scoped to chosen book, `human_reviewed` only) — `embedding_candidates()`
3. Rank fusion: tag hits weighted 2x over embedding similarity; return top `k` `principle_id`s (default 5) — `rank_fusion()`

Orchestrated end-to-end by `retrieve_principles(db, book_id, entries, embedding_client, mood, top_k)`.

### Step B — Synthesis (one LLM call) — `app/synthesis.py`

Prompt uses: book title/author, core_thesis, retrieved principles (id/name/summary only), user's log entries, mood, prior suggestion statuses (parameter exists; not yet populated by the route — see §5's MVP-completion note). LLM is local Ollama, not Claude API (see 9.14).

Output JSON (**reshaped 2026-08-03** — was `{"analysis": "...", "suggestions": [{"text", "principle_id"}]}`):
```json
{
  "reflection": ["one self-contained observation, grounded only in the principles, that references what the user actually logged today", "..."],
  "suggestions": [{"text": "...", "principle_id": "...", "explanation": "why this tip follows from the principle"}]
}
```
2-4 reflection points; suggestions are the user's "top 3 tips for tomorrow" — prompt asks for at most 3, and Step C rule-enforces the cap (see below) rather than trusting the prompt. `synthesize_analysis()` does one call and parses the result; `fallback_analysis()` is the non-LLM path used when verification can't be satisfied — it turns each retrieved principle into a verbatim reflection string and slices the first 3 principles into suggestions with a static (non-LLM) explanation.

### Step C — Verification (separate from synthesis) — `app/verification.py`

1. **Rule-based:** reject invalid `principle_id`s; reject empty suggestion `text`/`explanation`; reject more than 3 suggestions (`MAX_SUGGESTIONS`, rule-enforced not prompt-trusted); reject quotes >15 words or >1 quote per principle, scanned across every reflection point and every suggestion's `text` + `explanation` — reuses `app/constraints.py`'s `find_quotes`/`count_words`
2. **LLM entailment:** "Does the reflection or any suggestion (including its explanation) claim anything NOT in principle summaries?" → PASS/FAIL; anything other than an unambiguous PASS is treated as FAIL (fails closed)
3. On fail: `app/routers/analyses.py` retries synthesis once (stricter prompt), then falls back to `fallback_analysis()`'s template

---

## 13. Book Ingestion Pipeline (architecture §2/§8 — PARTIALLY BUILT)

Offline/admin only. Flow: suggestion (title+author) → draft (human notes **or** local-model extraction) → mandatory human review → publish.

- ✅ **Local extraction (Path B):** `tools/local_extraction/` — standalone CLI, PDF never leaves the local machine (see §5 above for detail).
- ✅ **Draft submission:** `POST /api/ingestion/draft-submissions` — accepts either path's draft JSON, stores as `pending_review` (see §8, §9.11).
- ❌ **Suggestion intake** (title+author → metadata lookup) — not built.
- ❌ **Human review/approval UI or endpoint** — not built. Nothing currently flips `review_status` to `human_reviewed`, increments `version`, or generates embeddings. A submitted draft sits at `pending_review` indefinitely until something (manual DB edit, for now) approves it.
- No file upload or chapter paste anywhere in the *hosted app's* UI — still true; the local extraction tool is explicitly outside that boundary.
- LLM may only reword editor's own paraphrased notes on Path A, never summarize from book text.
- Human review gate must check: quote limits, fidelity to the source, tag quality — for `local_model` drafts this is the *first* human check against the actual book, not a second opinion (architecture §2 is explicit that this can't be skipped or lightened for that reason).

---

## 14. Environment / Tooling Notes

- **OS:** macOS (darwin 24.6.0) during development
- **Python:** 3.13.5 in venv
- **Docker:** Docker Desktop installed at `/Applications/Docker.app` but `docker` not always in PATH
- **Redis:** Homebrew redis service was running locally (for future RQ)
- **Git:** Repo may not be initialized; user rule says only commit when explicitly asked
- **No CI** configured yet

---

## 15. User Workflow Preferences

- Work in **phases**; stop for review after each
- Post **plan before writing files** when starting a new phase
- Audit requests want **evidence**, not assertions — paste schema dumps, raw test output
- Fix failing tests by fixing code, never weakening tests
- If a test seems wrong vs architecture, **stop and explain** rather than change the test

---

## 16. Immediate Next Steps (recommended order)

1. ~~Complete Phase 1 audit~~ — ✅ done 2026-07-08, see `docs/PHASE_1_AUDIT.md`.

2. ~~Copy `architecture_1.0.md` → `docs/ARCHITECTURE.md`~~ — resolved; architecture now lives at `docs/Architecture.md`.

3. ~~Build local extraction tool (Path B)~~ — ✅ done, see §5. Produced one real draft; **awaiting human review** before it's trusted for anything downstream.

4. ~~Build Draft Extraction submission endpoint~~ — ✅ done 2026-07-30, see §8/§9.11/§13. `six-pillars-of-self-esteem` is now actually submitted into the dev DB (176 principles, `review_status=pending_review`) via `scripts/submit_local_draft.py`.

5. ~~Build embedding generation + Retrieval Step A~~ — ✅ done 2026-07-30, see §5/§12/§9.12/§9.13.

6. ~~Mark the Six Pillars draft human_reviewed + generate embeddings~~ — ✅ done 2026-07-30. `six-pillars-of-self-esteem` is `human_reviewed`, all 176 principles embedded via a real Voyage AI call.

7. ~~Build Synthesis + Verification + `/api/analyses` + users/suggestions/streaks + a minimal frontend~~ — ✅ done 2026-07-31, see §5's "MVP Completion" entry, §12, §9.14-9.17. Live-smoke-tested end to end via curl against the real running backend (user → active book → daily log → analysis → suggestions → mark done → streak) and via a real `npm run build` + `next dev` for the frontend.

8. **Housekeeping, not yet done:**
   - No git repository initialized anywhere in this project — everything is currently uncommitted (now spanning `backend/` and `frontend/`).
   - `tools/local_extraction/resume_aggregation.py` is a leftover one-off debugging script, fully superseded by the CLI's own chunk-level caching — safe to delete.
   - `frontend/.env.local` was created locally from `.env.local.example` for the dev run — gitignored, needs recreating on a fresh checkout.

9. **What's left for a real (non-single-user) product**, in rough priority order:
   - Real auth (9.16 spells out exactly what's missing and why it's acceptable for now, not indefinitely)
   - A review/publish UI replacing the `mark_reviewed.py` script
   - Wiring yesterday's suggestions into the synthesis prompt's "prior suggestions" context (parameter already exists, unused by the route)
   - Ingesting more than one book — everything is built generically, but only `six-pillars-of-self-esteem` has ever gone through the pipeline
   - Revisiting the Claude API vs local-Ollama decision (9.14) if synthesis quality/fallback-rate matters more than local-only privacy at some point

---

## 17. Quick Reference — Key File Paths

| What | Path |
|---|---|
| Architecture | `docs/Architecture.md` |
| ORM models | `backend/app/models/__init__.py` |
| Pydantic schemas | `backend/app/schemas/__init__.py` |
| Word/quote cap logic | `backend/app/constraints.py` |
| Migrations | `backend/alembic/versions/001_initial_schema.py`, `002_extraction_method.py`, `003_principle_embedding.py`, `004_reflection_and_explanation.py`, `005_reflection_paragraph.py`, `006_drop_daily_log_date_unique.py` |
| FastAPI entry | `backend/app/main.py` |
| Ingestion endpoint | `backend/app/routers/ingestion.py` |
| Embeddings | `backend/app/embeddings.py` |
| Retrieval (Step A) | `backend/app/retrieval.py` |
| Synthesis (Step B) | `backend/app/synthesis.py` |
| Verification (Step C) | `backend/app/verification.py` |
| LLM client (local Ollama) | `backend/app/llm.py` |
| Analysis orchestration | `backend/app/routers/analyses.py` |
| Streaks | `backend/app/streaks.py`, `backend/app/routers/streaks.py` |
| Submit/review scripts | `backend/scripts/submit_local_draft.py`, `backend/scripts/mark_reviewed.py` |
| Test fixtures | `backend/tests/conftest.py` |
| Local extraction tool | `tools/local_extraction/` (README.md has prerequisites + pipeline steps) |
| Validated local draft | `tools/local_extraction/output/six-pillars-of-self-esteem.json` |
| Frontend | `frontend/` (single page: `app/page.tsx`, API client: `app/api.ts`) |

---

## 18. Architecture §4 Checklist (for audit report)

Use this when building the pass/fail matrix:

| Entity | Key requirement |
|---|---|
| books | core_thesis ≤150 words; tone enum; tracked_metrics JSONB; review_status; version; timestamps |
| principles | summary ≤200 words; applies_to_tags[]; embedding_id; FK book_id; review_status |
| users | UUID; email_encrypted bytes; auth_provider_id; active_book_id FK; timezone |
| daily_logs | unique (user_id, date); entries JSONB; mood 1-5; chosen_book_id FK |
| analyses | 1:1 log_id unique; retrieved_principle_ids[]; verification_status enum |
| suggestions | FK analysis_id + principle_id; status enum; resolved_at nullable |
| streaks_progress | unique (user_id, book_id, metric_id); trend_series JSONB |

---

*End of handoff. Start by reading `docs/Architecture.md` §4/§2/§8 and this file's §16 for the recommended next step.*
