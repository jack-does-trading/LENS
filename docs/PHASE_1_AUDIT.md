# Phase 1 Audit Report

**Date:** 2026-07-08
**Scope:** Data model + read-only catalogue (books, principles) + daily_logs CRUD, per `docs/Architecture.md` §4.
**Method:** Fresh Postgres container (`docker compose down && docker compose up -d`, no persisted volume — genuinely clean DB), `alembic upgrade head` from revision `None` to `001`, schema dumped via `psql \d+`, then full `pytest -v` run.

---

## 1. Migration run (clean DB)

```
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 001, initial schema
```

Single migration, applied cleanly, no errors.

## 2. Schema dump — tables present

```
$ \dt
             List of relations
 Schema |       Name       | Type  | Owner
--------+------------------+-------+-------
 public | alembic_version  | table | lens
 public | analyses         | table | lens
 public | books            | table | lens
 public | daily_logs       | table | lens
 public | principles       | table | lens
 public | streaks_progress | table | lens
 public | suggestions      | table | lens
 public | users            | table | lens
(8 rows)
```

All 7 application tables from architecture §4 present (`alembic_version` is Alembic's own bookkeeping table).

## 3. Schema dump — per-table verification against architecture §4

| Table | Columns match §4? | Constraints verified |
|---|---|---|
| `books` | Yes | PK `book_id`; `ck_books_core_thesis_word_cap` CHECK `word_count(core_thesis) <= 150`; defaults for `tone`/`review_status`/`version` match spec |
| `principles` | Yes | PK `principle_id`; FK `book_id → books.book_id ON DELETE CASCADE`; `ck_principles_summary_word_cap` CHECK `word_count(summary) <= 200` |
| `users` | Yes | PK `user_id` (uuid); UNIQUE `auth_provider_id`; FK `active_book_id → books.book_id ON DELETE SET NULL` |
| `daily_logs` | Yes | PK `log_id`; UNIQUE `(user_id, date)` as `uq_daily_logs_user_date`; FK `user_id → users.user_id ON DELETE CASCADE`; FK `chosen_book_id → books.book_id ON DELETE RESTRICT`; CHECK `mood IS NULL OR (mood BETWEEN 1 AND 5)` |
| `analyses` | Yes | PK `analysis_id`; UNIQUE `log_id` (`uq_analyses_log_id`, enforces 1:1); FK `log_id → daily_logs.log_id ON DELETE CASCADE` |
| `suggestions` | Yes | PK `suggestion_id`; FK `analysis_id → analyses.analysis_id ON DELETE CASCADE`; FK `principle_id → principles.principle_id ON DELETE RESTRICT` |
| `streaks_progress` | Yes | PK `streak_id`; UNIQUE `(user_id, book_id, metric_id)` (`uq_streaks_progress_user_book_metric`); FKs to `users` and `books`, both `ON DELETE CASCADE` |

Full raw `\d+` output captured during this session for all 7 tables — column types, defaults, and constraint names all match the mapping in `docs/HANDOFF.md` §7 and architecture §4 exactly. No discrepancies found.

## 4. Fragility fix applied

`tests/conftest.py`'s `db_session` fixture previously called `transaction.rollback()` unconditionally in its `finally` block. When a test itself triggers an expected `IntegrityError` and calls `db_session.rollback()` inline (e.g. the word-cap and FK/unique-violation tests), the connection-level transaction is already inactive by teardown time, and the unconditional rollback raised `SAWarning: transaction already deassociated from connection`.

**Fix:** guarded the fixture teardown with `if transaction.is_active:` before calling `transaction.rollback()` (`tests/conftest.py:52`).

**Verification:** full suite re-run with `pytest -v -W error::sqlalchemy.exc.SAWarning` (escalating that warning class to a hard error) — all 11 tests still pass, confirming the warning no longer fires even with the 3 new tests that deliberately trigger and locally roll back `IntegrityError`.

## 5. New tests added (closing the 5 gaps from HANDOFF.md §10)

| File | Test | Gap closed |
|---|---|---|
| `tests/test_book_word_cap.py` | `test_core_thesis_over_word_cap_rejected_by_pydantic` | core_thesis Pydantic cap |
| `tests/test_book_word_cap.py` | `test_core_thesis_over_word_cap_rejected_by_db` | core_thesis DB cap |
| `tests/test_book_word_cap.py` | `test_valid_core_thesis_is_accepted` | boundary case (150 words exactly accepted) — not a gap, added for symmetry with the existing principle-summary boundary test |
| `tests/test_constraints.py` | `test_daily_logs_unique_user_id_date_rejected_by_db` | daily_logs unique(user_id, date) |
| `tests/test_constraints.py` | `test_principle_with_nonexistent_book_id_rejected_by_fk` | FK enforcement (principle → nonexistent book_id) |
| `tests/test_routes.py` (extended `test_books_are_read_only`) | `PUT /api/books/{book_id}` → 405 | books PUT-rejected |
| `tests/test_routes.py` (extended `test_principles_are_read_only`) | `PUT /api/principles/{principle_id}` → 405 | principles PUT-rejected |

## 6. Full test run (raw output, this session, fresh DB)

```
============================= test session starts ==============================
platform darwin -- Python 3.13.5, pytest-9.1.1, pluggy-1.6.0
rootdir: /Users/bhavyadeephada/Desktop/LENS/backend
configfile: pytest.ini
testpaths: tests
plugins: anyio-4.14.1
collecting ... collected 11 items

tests/test_book_word_cap.py::test_core_thesis_over_word_cap_rejected_by_pydantic PASSED [  9%]
tests/test_book_word_cap.py::test_core_thesis_over_word_cap_rejected_by_db PASSED [ 18%]
tests/test_book_word_cap.py::test_valid_core_thesis_is_accepted PASSED   [ 27%]
tests/test_constraints.py::test_daily_logs_unique_user_id_date_rejected_by_db PASSED [ 36%]
tests/test_constraints.py::test_principle_with_nonexistent_book_id_rejected_by_fk PASSED [ 45%]
tests/test_principle_word_cap.py::test_principle_summary_over_word_cap_rejected_by_pydantic PASSED [ 54%]
tests/test_principle_word_cap.py::test_principle_summary_over_word_cap_rejected_by_db PASSED [ 63%]
tests/test_principle_word_cap.py::test_valid_principle_summary_is_accepted PASSED [ 72%]
tests/test_routes.py::test_books_are_read_only PASSED                    [ 81%]
tests/test_routes.py::test_principles_are_read_only PASSED               [ 90%]
tests/test_routes.py::test_daily_logs_crud PASSED                        [100%]

=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/fastapi/testclient.py:1
  StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.

tests/test_book_word_cap.py::test_core_thesis_over_word_cap_rejected_by_db
  DeprecationWarning: No path_separator found in configuration; falling back to legacy splitting...

======================== 11 passed, 2 warnings in 0.88s ========================
```

Both warnings are pre-existing, unrelated to this audit (an `httpx`/Starlette test-client deprecation notice, and an Alembic config deprecation notice) — not schema or data-integrity issues, no action needed.

## 7. Requirement → test → pass/fail matrix (architecture §4)

| Entity | Requirement | Test | Result |
|---|---|---|---|
| books | `core_thesis` ≤ 150 words, DB-enforced | `test_core_thesis_over_word_cap_rejected_by_db` | ✅ PASS |
| books | `core_thesis` ≤ 150 words, Pydantic-enforced | `test_core_thesis_over_word_cap_rejected_by_pydantic` | ✅ PASS |
| books | 150-word boundary accepted | `test_valid_core_thesis_is_accepted` | ✅ PASS |
| books | read-only via API (no POST) | `test_books_are_read_only` | ✅ PASS |
| books | read-only via API (no PUT) | `test_books_are_read_only` | ✅ PASS |
| principles | `summary` ≤ 200 words, DB-enforced | `test_principle_summary_over_word_cap_rejected_by_db` | ✅ PASS |
| principles | `summary` ≤ 200 words, Pydantic-enforced | `test_principle_summary_over_word_cap_rejected_by_pydantic` | ✅ PASS |
| principles | 200-word boundary accepted | `test_valid_principle_summary_is_accepted` | ✅ PASS |
| principles | FK `book_id` must reference an existing book | `test_principle_with_nonexistent_book_id_rejected_by_fk` | ✅ PASS |
| principles | read-only via API (no POST) | `test_principles_are_read_only` | ✅ PASS |
| principles | read-only via API (no PUT) | `test_principles_are_read_only` | ✅ PASS |
| daily_logs | unique `(user_id, date)` | `test_daily_logs_unique_user_id_date_rejected_by_db` | ✅ PASS |
| daily_logs | full CRUD (create/read/update/delete) | `test_daily_logs_crud` | ✅ PASS |
| daily_logs | `mood` 1–5 range (CHECK constraint) | Verified via schema dump (§3 above); no dedicated negative test yet | ⚠️ SCHEMA VERIFIED, NO TEST |
| users | UUID PK, `email_encrypted` bytes, unique `auth_provider_id`, FK `active_book_id` SET NULL | Verified via schema dump + `seed_user`/`seed_book` fixtures used across suite | ⚠️ SCHEMA VERIFIED, NO DEDICATED TEST |
| analyses | 1:1 with `daily_logs` (unique `log_id`), FK CASCADE | Verified via schema dump only — table not yet exercised by any route (no routes exist yet, correctly deferred to Phase 2) | ⚠️ SCHEMA VERIFIED, NO TEST (no routes yet) |
| suggestions | FK `analysis_id` CASCADE, FK `principle_id` RESTRICT | Verified via schema dump only — same as above | ⚠️ SCHEMA VERIFIED, NO TEST (no routes yet) |
| streaks_progress | unique `(user_id, book_id, metric_id)` | Verified via schema dump only — same as above | ⚠️ SCHEMA VERIFIED, NO TEST (no routes yet) |

**Note on the ⚠️ rows:** `analyses`, `suggestions`, and `streaks_progress` have no API routes in Phase 1 by design (HANDOFF.md §8, "Not exposed"), so there is nothing to test at the integration level yet beyond what's already confirmed in the schema dump. The `mood` range CHECK and the `users` table constraints are schema-verified but don't yet have dedicated negative-path tests; these are lower-risk gaps (simple CHECK/unique constraints identical in shape to ones already proven to work) rather than newly-discovered problems, and can be picked up opportunistically in Phase 2 rather than blocking it.

## 8. Verdict

- All 7 tables match architecture §4 exactly (schema dump, this session).
- All previously-identified test gaps that had corresponding API/ORM behavior to exercise are now closed: 11/11 tests pass on a clean DB.
- The `db_session` fixture fragility is fixed and verified not to regress under the new violation-triggering tests.
- No design flaws found. Code remains a faithful, correct implementation of the architecture doc for what's built so far.
- **Phase 1 audit is now complete.** Phase 2 is unblocked per the user's own stated condition.
