# Evaluating Lens

This project had 124 passing tests before any of this existed. Every one of them
proved the code *ran*. Not one measured whether the advice was any **good**.

That gap is not academic here. Two Groq bugs once made every LLM call in this app
fail for an unknown period, and nothing looked broken — because the pipeline fails
closed to a template, and the template is a perfectly reasonable answer. No 500s,
no alerts, no complaints. The README already drew the lesson:

> a fail-closed design is the right call, but it converts an outage into a
> quality regression. If your safe fallback is indistinguishable from success,
> add a log line or a metric at the moment you fall back — otherwise the system
> is *supposed* to be doing the interesting thing and quietly isn't.

This doc is the rest of that sentence. It describes a harness that measures
retrieval, verification and faithfulness, enforces floors on every pull request,
and — the part that matters most — **calibrates its own LLM judge against human
labels before quoting anything the judge says.**

---

## 1. Where the numbers are, right now

All figures are n=16 unless stated. Sixteen is small. Every report prints `n` and
a standard error next to every rate for exactly that reason; the honest reading of
anything below is *directional*, not established.

### Retrieval (golden set, k=5)

| metric | before | after | floor |
|---|---|---|---|
| recall@k | 0.07 | **0.122** | 0.10 |
| precision@k | 0.19 | **0.213** | 0.18 |
| MRR | 0.27 | **0.494** | 0.42 |
| hit rate | 38% | **75%** | 68% |

"Before" is production as it shipped: `top_k=3`, additive tag weight of 2.0.
"After" is Reciprocal Rank Fusion at `k=5` with `TAG_MATCH_WEIGHT=0.5`. MRR nearly
doubled and the share of situations that surfaced at least one relevant principle
went from just over a third to three quarters.

Recall looks terrible in isolation and mostly isn't: several golden cases have 8+
expected principles, so recall@5 **cannot** exceed 0.62 on this set by
construction. The harness therefore always reports `recall_ceiling` beside it —
0.122 is 21% of what was achievable. §6's target of 0.80 is unreachable at k=5 and
is tracked as an aspiration, not a gate.

### Verifier (seeded bad outputs, rule half)

| metric | value | threshold |
|---|---|---|
| catch rate | 10/10 = 1.00 | ≥ 0.90 |
| false-positive rate | 0/3 = 0.00 | ≤ 0.10 |

### Faithfulness judge, calibrated against human labels

| rubric field | κ | raw agreement | reading |
|---|---|---|---|
| faithfulness (ordinal, quadratic-weighted) | **0.87** | 81% | almost perfect |
| hallucination (nominal) | **1.00** | 100% | almost perfect |
| suggestion groundedness (ordinal) | **0.95** | 75% | almost perfect |

Read raw agreement next to κ, not instead of it, and in both directions. κ is
high and raw agreement is 75–81%, which means the judge disagrees on a quarter of
cases but almost always by one grade — a rater that is consistently a little
strict, which is usable. The reverse pattern would be the dangerous one.

---

## 2. Running it

```bash
cd backend
docker compose up -d                       # Postgres + pgvector
export TEST_DATABASE_URL=postgresql://lens:lens@localhost:5432/lens

python -m pytest                           # 207 tests. No evals, no network.
python -m pytest -m eval                   # 15 gates. Cassette-replayed, free.
python -m pytest -m eval_live              # 4 checks. Needs GROQ + VOYAGE keys.
```

The full Markdown report, which is what CI publishes:

```bash
docker compose exec -T db psql -U lens -d postgres -c 'CREATE DATABASE lens_report'
python scripts/eval_report.py \
  --database-url postgresql://lens:lens@localhost:5432/lens_report \
  --out eval-report.md
```

It exits 1 on any threshold breach, so it is a gate as well as a report. It
refuses a non-local database host unless `LENS_ALLOW_REMOTE_TEST_DB=1`: it writes
1213 principles into whatever it is pointed at.

---

## 3. What each metric means, and why it is that one

**recall@k** — of the principles a human said should have surfaced, what fraction
did. Missing an obviously relevant principle is worse than returning one extra,
which is why §6 set a recall target rather than a precision one. Always read with
`recall_ceiling`.

**precision@k** — of what was returned, what fraction a human wanted. The
cleanest single number, because it has no ceiling artefact.

**MRR** — how far down the list the first relevant principle sits. This is the
metric closest to the actual product: only the top few principles reach the
synthesis prompt, so *rank* matters more than membership.

**hit rate** — did this situation get *anything* relevant at all. The bluntest
question, and the one a user would ask. It is also the floor that caught the
regression described in §7.

**catch rate / false-positive rate** — on seeded bad outputs. §6 is blunt about
why: *"a verification step that never fails anything is not a verification step."*
The false-positive rate matters more than it sounds — each one burns a synthesis
retry, and five burnt retries hand the user the fallback template.

**faithfulness / hallucination / suggestion groundedness** — §6's own three-part
rubric, unchanged, on the original 1–5 anchors. Reusing it rather than inventing
a new one is deliberate: the rubric is the contract with whoever reads the score,
and changing it silently makes every number before and after incomparable. Hence
`PROMPT_VERSION` in `eval/judge.py`, bumped by hand.

**Cohen's κ** — agreement between the judge and a human, corrected for the
agreement two raters would reach by chance. See §5.

---

## 4. The golden set

16 cases in `backend/eval/cases/golden.jsonl`, **mined from real usage** rather
than invented. §6 originally specified 8 synthetic cases; the substitution is
recorded in Architecture §10. A synthetic case tests the pipeline against your
imagination of a user; a mined one tests it against a user. The cost is that the
set starts small and grows only as the app is used.

Each case keeps two id lists strictly apart:

- `retrieved_at_export` — what the system **did** return.
- `expected_principle_ids` — what a human says it **should** have.

Conflating them would have the harness grade the system against its own past
output, which always scores perfectly and measures nothing.

### Privacy: how real journal entries can be committed at all

Every case is a real visitor's journal entry in their own words. Two of the
sixteen disclose suicidal ideation. Those must not reach a public remote.

So each case carries `redaction: "redacted"`: the entry text is an LLM paraphrase
(`eval/redact.py`), not what anyone typed. The paraphrase threads a needle — it
has to keep the situation, the domain and the emotional register, because the
labels were assigned against the *meaning*, while dropping the phrasing and any
identifying detail. Replacing the text with `[redacted] [Work]` would protect
privacy perfectly and destroy the eval set.

Two mechanical guards, because "the model was told to paraphrase" is not evidence
that it did: `scripts/redact_eval_cases.py` rejects any rewrite sharing four
consecutive words with the original (retrying up to four times and keeping the
best), and it re-scores retrieval before and after. The redaction moved MRR
0.50 → 0.49 and hit rate 81% → 75% — one case, inside the ±12pp standard error —
and left the parameter sweep's optimum unchanged. The verbatim originals stay in
the gitignored `eval/backups/`.

The redaction prompt explicitly **forbids** sanitising self-harm content. Softening
those two entries would have quietly removed the cases that matter most from a
system that has no crisis handling.

### Growth path

`scripts/export_eval_cases.py` is re-runnable and skips `source_log_id_hash`
values already labelled, so the set accretes as the app is used. Labels are
flushed to disk after every single decision, not at the end of the run — an
earlier version buffered them in memory and a dropped database connection threw
away an hour of human labelling while still printing `kept -> golden.jsonl`.

### How the labels were produced

All 16 cases were labelled with `--assist`, which reads every principle in the
book, proposes a shortlist, and then walks the human through it one at a time to
accept, edit or reject. The shortlist is a reading aid, not a vote: a book has
200+ principles and asking a person to hold all of them in their head for each
case is how a labelling session stops after four.

The labels are **ground truth for this harness.** The owner has reviewed and
accepted them as such, so there is no un-assisted control set and none is
planned. Recording the method here rather than burying it is the point — a
reader who wants to discount the numbers for anchoring now has what they need to
do that, and the alternative (an unlabelled set, or a synthetic one) would be
worse on every axis that matters.

---

## 5. Judge calibration — the part that makes the rest credible

§6 specified two human raters and an inter-rater agreement number. That plan does
not survive a golden set that grows with usage: it prices every new case at two
human reads, so the set stops growing the moment the humans get bored. Lens
substitutes an LLM for the second rater.

**That substitution is only legitimate because the judge is evaluated before it is
trusted.** A judge is an instrument. An uncalibrated instrument produces readings,
not measurements, and the failure is invisible — a judge that returns 5 for
everything reports a flawless system and never errors.

### Why κ and not raw agreement

On the hallucination flag, the judge and the human agree 88% of the time mostly
because hallucinations are rare. A judge that answered "no hallucination"
unconditionally would score 81% on this set while detecting nothing. κ subtracts
the agreement two raters would reach by chance, so the degenerate judge scores 0.
There is a test that asserts exactly this (`test_a_constant_judge_scores_zero_not_high`).

Two variants, because the rubric has two kinds of field:

- **nominal κ** for the hallucination flag, where true and false are just different.
- **quadratic-weighted κ** for the 1–5 fields, where 4-vs-5 is a near miss and
  1-vs-5 is total disagreement. Unweighted κ treats those as equally wrong and
  would throw away a rater that is reliably one grade strict.

Both are ~60 lines of stdlib in `eval/calibration.py`. `scikit-learn` would save
the lines and cost a numpy + scipy install in CI for one formula. The formula is
also short enough to be *reviewed*, which matters more than usual: this is the
number that licenses every other number.

### The calibration set

16 hand-authored cases in `eval/labels/judge_calibration.jsonl`, separate from
`eval/adversarial/`. The adversarial set asks a binary question and its cases are
built to trip one specific rule; calibrating a 1–5 rubric needs something it
structurally cannot provide — **cases in the middle of the scale.** A set of only
5s and 1s makes ordinal κ a near-binary agreement score, and a judge that can only
tell perfect from catastrophic would pass it while being useless on the 3s and 4s
real output actually produces. So the set spans all five points on both scales,
and two cases deliberately break the correlation between the flag and the
faithfulness score in opposite directions.

Every human label was authored **with** the case, as its specification, before the
judge ever ran — so no label can have been fitted to an answer.

### What calibration actually caught

This is the part worth reading, because it is the whole argument for doing this at
all.

Under `judge-v1`, κ looked fine: faithfulness 0.95, hallucination 0.73,
groundedness 0.89 — every field above the 0.60 floor. A test asking a different
question failed anyway: **on all 16 cases, the judge flagged a hallucination if
and only if it had given faithfulness ≤ 2.** The flag was a deterministic function
of the judge's own score. Its κ of 0.73 was re-measuring faithfulness, not
validating the flag, and nothing in the κ table showed that.

Two cases exist specifically to detect this, and v1 got both wrong:

- *unsupported-claim-that-is-not-invented* — "self-control runs lowest in the
  evening". Plausibly true, entirely absent from the principles, not attributed to
  the book. Human: faithfulness 2, hallucination **false**. v1 said 1 / **true**.
- *explanation-describes-wrong-principle* — a clean reflection whose suggestion
  misdescribes what a principle says. Human: faithfulness 5, hallucination
  **true**. v1 said 5 / **false**.

The fix was not to the test. `judge-v2` makes the flag **evidence-first**: the
judge must quote the exact invented phrase into a `hallucinated_item` field
*before* it assigns any score, and the flag is defined as "that field is not
null". A flag that has to cite a phrase cannot be derived from a number.

| field | judge-v1 | judge-v2 |
|---|---|---|
| faithfulness κ | 0.95 | 0.87 |
| hallucination κ | 0.73 | **1.00** |
| groundedness κ | 0.89 | 0.95 |
| flag independent of score? | **no** | yes |

Both hard cases flipped to exact agreement. Faithfulness κ *dropped* — v2 is
slightly more generous on over-generalisation — and that is the honest trade: one
metric got worse, the instrument got trustworthy, and the diff says so.

---

## 6. Cassettes: how the eval runs in CI with no API key

Retrieval fuses tag matching (deterministic) with embedding cosine similarity
(needs real vectors). `FakeEmbeddingClient` seeds `random.Random` with
`sha256(text)`, so distinct texts land in roughly orthogonal directions
**regardless of meaning** — "went for a run" sits as far from "went jogging" as
from "filed my taxes". Scoring retrieval against it would measure the tag arm only
and report a number that looks like recall and isn't.

Calling the real providers in CI is worse than it sounds: it needs API keys as
repo secrets, it costs money on every push, and **it is not reproducible.** If
Voyage retrains `voyage-3`, your recall moves and nothing tells you whether your
code regressed or their model shifted.

So: record once, replay forever.

| cassette | what | size |
|---|---|---|
| `corpus.json.gz` | 6 books, 1213 principles, tags and metadata | 108 KB |
| `principle_vectors.bin.gz` | 1213 × 1024 float32 Voyage vectors | 4.4 MB |
| `query_vectors.json` | one vector per golden case's day-text | 432 KB |
| `judge_verdicts.json` | the judge's raw completions, from real Groq | 8 KB |

Re-recording is an explicit, reviewable commit: the cassette changes, the numbers
change, and the diff says which.

**A miss raises.** Never a default, never a fallback vector. A silently-degraded
embedding arm would still produce a plausible-looking recall number, and that is
the single worst failure mode an eval harness can have. Judge verdicts are keyed
by `sha256(model | full prompt)` rather than by case id, so editing the rubric or a
case invalidates its recording and the replay misses loudly instead of happily
replaying a verdict the judge gave to a question it is no longer being asked.

Two implementation notes that were forced rather than chosen:

- **Storage is stdlib `array` + `gzip`, not numpy `.npz`.** numpy is not a
  dependency of this project — the plan assumed pgvector pulled it in; it doesn't.
  1213 × 1024 float32 is ~5 MB either way, and adding a numeric stack so CI can
  read 5 MB of floats is a poor trade in a backend that deliberately uses `urllib`
  over `requests`. Thresholds are JSON rather than YAML for the same reason:
  `pyyaml` is only a transitive dependency here.
- **The corpus is recorded from Postgres, not rebuilt from
  `tools/local_extraction/output/*.json`.** Those committed files still carry the
  placeholder `applies_to_tags` that `scripts/retag_principles.py` replaced, so
  seeding from them would silently restore the bug the retag fixed. There is a
  test guarding this.

The harness seeds the cassette into a throwaway Postgres and calls
`retrieve_principles()` — the actual production function, through the
`EmbeddingClient` Protocol. It grades the system, not a reimplementation of it.
And `test_offline_eval_reproduces_the_live_measurement` asserts the replayed
numbers match the live ones, so CI is measuring the system rather than the cassette.

### Re-recording

```bash
# corpus + principle vectors, from a live database (no Voyage calls: the
# vectors are already in the embedding column)
python scripts/record_cassettes.py --database-url "$DATABASE_URL"

# query vectors — pays Voyage once per new case, cached by content hash
python scripts/measure_retrieval.py --database-url "$DATABASE_URL" --source golden

# judge verdicts — 16 Groq calls, then prints κ
python scripts/calibrate_judge.py --record
```

---

## 7. CI gates

`.github/workflows/ci.yml` gains an `eval-gates` job, kept separate from
`backend-tests` on purpose: a failure there means *the advice got worse*, not *a
test broke*, and that distinction vanishes if it shows up as one more red dot in a
207-test run. It needs no secrets, makes no network calls, runs `pytest -m eval`,
then publishes the Markdown report as an artifact and a PR comment with
`if: always()` — a red build that doesn't say which metric moved is most of the
way to useless.

Floors live in `eval/thresholds.json`, as data. Lowering one is a reviewable diff
rather than an edit buried in an `assert`.

**The gate bites.** Reverting `TAG_MATCH_WEIGHT` from 0.5 back to the shipped 2.0
produces:

```
| hit rate | 0.625 | 0.68 | ❌ |
THRESHOLD BREACH: hit_rate
FAILED tests/test_eval_retrieval.py::test_retrieval_meets_the_recorded_floors
FAILED tests/test_eval_retrieval.py::test_offline_eval_reproduces_the_live_measurement
```

Two gates are written specifically to prove they can fail:
`test_the_gate_can_actually_fail` and `test_the_calibration_gate_can_actually_fail`,
the latter feeding in a judge that scores everything 5/no-hallucination — the exact
degenerate rater a raw-agreement check would wave through.

### The nightly job, and what only it can catch

The PR gate is blind to the providers by construction. `nightly-eval.yml` is the
other half: `pytest -m eval_live` against real Groq and real Voyage, on a
schedule, opening (or commenting on) a single tracking issue rather than blocking
anyone. It asks three questions the offline gate cannot:

- does the entailment judge still catch semantic hallucination?
- does the faithfulness judge still agree with the human labels?
- does live `voyage-3` still reproduce the recorded query vectors? (cosine ≥ 0.999
  against the cassette, one batched request for all 16 queries)

This is the job that would have caught the Groq decommission. A failure here with
no code change means the cassette is stale or a provider moved — not that the
pipeline broke — and the issue body says so and lists the three things to check in
order.

It needs no database. Pointing a scheduled job at the production instance to
measure quality is how a read-only eval becomes an incident.

---

## 8. Production telemetry

Gates catch regressions before merge. They say nothing about right now.

`analyses` used to record the outcome and nothing else — an analysis that passed
on attempt 1 and one that passed on attempt 5 were indistinguishable. Migration
`008_analysis_telemetry` adds `synthesis_attempts`, `verification_issues`,
`llm_provider`, `llm_model`, `prompt_version` and `latency_ms`, all nullable.

`GET /api/metrics/quality` answers "is it working right now" in one request:
fallback rate over the last N days, mean attempts, and an issue-type histogram.
**Counts only, never user text** — test-enforced. This is the endpoint that would
have caught the Groq outage.

`InstrumentedLLMClient` wraps any `LLMClient` and times every call, classifying it
as synthesis or entailment from the prompt itself. Because `LLMClient` is a
`typing.Protocol`, `synthesis.py` and `verification.py` needed no changes at all.

### Langfuse

`app/tracing.py` exports one trace per analysis: a retrieval span, a generation
per LLM call (numbered, so a trace shows the retry self-correcting), and scores
for `verification_passed`, `synthesis_attempts` and `latency_ms`. Scores rather
than metadata, because a score is what Langfuse can chart and alert on — and "what
fraction of analyses fell back this week" is the exact question that went
unanswered through the outage.

Three decisions here each invert something the rest of the backend does:

1. **Tracing fails open.** Everything else in this pipeline fails closed, and that
   is right when the failure would hand a user a bad answer. It is exactly wrong
   here: a Langfuse outage that turned into a fallback analysis would trade a real
   feature for a diagnostic. There is a test that switches tracing on, makes
   Langfuse unreachable, and asserts the request still returns 201.
2. **It speaks HTTP directly over `urllib`, not via the `langfuse` SDK.** Unlike
   the rest of the harness this code runs in production, so the SDK would go in
   `requirements.txt` and pull an OpenTelemetry tree into a free-tier Render build
   for what is one authenticated POST. Going direct also puts every failure mode
   in one file, which matters for a component whose contract is "never raise".
3. **Sending happens after the response**, on FastAPI's `BackgroundTasks`. A
   third-party network call on the critical path of a feature that fails open is a
   contradiction, not a design.

**Privacy.** Prompts contain journal entries verbatim, so enabling this sends
personal data to a third party. Architecture §9 made that tradeoff once already,
for Groq, and wrote it up rather than quietly shipping it; §10 carries the second
instance. `LANGFUSE_MASK_INPUTS` defaults to **on**: structure, ids, scores,
latencies and character counts still go, and every piece of user text becomes a
stable 16-hex digest. You keep every operational metric and lose only the ability
to read the prompt in the UI. Masking also controls whether the request retains
the text in memory at all, and the test asserts the secret appears nowhere in the
serialised payload — not merely that the `input` field was masked.

`langfuse_enabled` and `langfuse_mask_inputs` are separate switches because "send
nothing" and "send structure" are different decisions, and one flag would hide one
of them.

---

## 9. Adding to the harness

**A golden case.** Run `export_eval_cases.py` against a database, then
`label_eval_cases.py`. Redact before committing (`redact_eval_cases.py`), then
re-record the query vector for the new case and re-run
`scripts/measure_retrieval.py`. Commit the case, the cassette and the metric
change together.

**An adversarial case.** Add a line to `eval/adversarial/rules.jsonl` (rule half,
runs on every push) or `entailment.jsonl` (needs a live provider). Entailment
cases **must be rule-clean** — `_rule_based_issues` short-circuits, so a case that
also trips a regex never reaches the LLM and proves nothing about the judge.
`test_entailment_cases_are_rule_clean` enforces this and needs no LLM.

**A judge calibration case.** Add a line to `eval/labels/judge_calibration.jsonl`
with its human labels and a `why` explaining which rubric point it is built to sit
at. Then `scripts/calibrate_judge.py --record` and check κ before trusting
anything.

**Moving a threshold.** Edit `eval/thresholds.json` in a commit whose message says
why. That file exists so lowering a gate to make CI green is visible in review.

---

## 10. What this harness does *not* do

Stated plainly, because a harness that oversells itself is worse than none.

- **n=16.** Everything is directional. ±12pp standard error on a rate.
- **The golden set is assisted-labelled** — a human accepted or rejected an
  LLM-proposed shortlist rather than reading 200+ principles per case (§4).
  Accepted as ground truth by the owner; discount for anchoring if you like.
- **The judge is calibrated on 16 hand-authored cases**, not on real pipeline
  output. It agrees with one human on cases built to span the rubric; that is
  evidence it is not broken, not that it is validated.
- **Only one human rater.** §6 wanted two. κ here is judge-vs-human, not
  human-vs-human, so there is no measurement of how much a *second* person would
  have disagreed with the first.
- **Faithfulness is not yet measured on the golden set.** The judge is calibrated
  and ready; running it over real pipeline output for all 16 cases and gating on
  mean faithfulness ≥ 4 is the next step, and the thresholds for it are already
  written down.
- **Ragas is not wired in.** The plan suggested it as a nightly second opinion. It
  would disagree systematically for a reason already understood — Lens's grounding
  definition is asymmetric, a suggestion's *action* is *supposed* to be a concrete
  tactic absent from the principles while its *explanation* is not, and Ragas'
  faithfulness metric does not encode that — so it would produce noise, not
  signal. Skipped deliberately rather than half-wired.
- **`requirements-eval.txt` does not exist**, because it turned out to be empty:
  κ is stdlib, tracing is `urllib`, cassettes are `array` + `gzip`. The whole
  harness adds **zero dependencies** to a project that installs its production
  requirements on a free-tier host.
- **No crisis detection.** Two of sixteen real entries are disclosures of suicidal
  ideation, and Lens responds to them with book advice. That is a product gap this
  harness measured but cannot fix.

---

## 11. Where things live

| Path | What |
|---|---|
| `eval/cases/golden.jsonl` | the 16 labelled cases (redacted, committed) |
| `eval/cases/staging.jsonl` | raw export, **gitignored** |
| `eval/backups/` | verbatim originals, **gitignored** |
| `eval/metrics.py` | recall@k, precision@k, MRR, hit rate, ceilings |
| `eval/harness.py` | seeds a cassette, runs the real retriever, renders the report |
| `eval/cassettes.py` | recorded corpus, query vectors, judge verdicts; replay clients |
| `eval/judge.py` | the faithfulness rubric and its parser |
| `eval/calibration.py` | Cohen's κ, weighted κ, confusion matrices |
| `eval/judge_cases.py` + `eval/labels/` | the calibration set and its human labels |
| `eval/adversarial/` | seeded bad outputs, rule half and entailment half |
| `eval/redact.py` | the paraphrase prompt and its shared-phrase check |
| `eval/thresholds.json` | every gate, as data |
| `app/telemetry.py` | instrumented client, structured logging, issue classification |
| `app/tracing.py` | Langfuse export |
| `scripts/eval_report.py` | the full Markdown report; exits 1 on a breach |
| `scripts/calibrate_judge.py` | record judge verdicts, print κ, gate on it |
| `scripts/record_cassettes.py` | dump the corpus from a database |
| `scripts/measure_retrieval.py` | live measurement + parameter sweep |
