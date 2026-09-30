"use client";

import Link from "next/link";
import { benchmarks } from "../benchmarksData";
import { useQuality } from "../useQuality";

/**
 * The numbers behind the advice.
 *
 * Two data sources, deliberately distinguished on screen because they answer
 * different questions and have different reliability:
 *
 *   * `benchmarks.json` -- a committed snapshot from scripts/export_benchmarks.py.
 *     Every figure on it is recomputed by tests/test_benchmarks_snapshot.py in the
 *     same `pytest -m eval` run that enforces the floors, so it cannot drift from
 *     what the harness actually produces. Imported, not fetched: it renders
 *     instantly and survives a sleeping backend.
 *   * `/api/metrics/quality` -- live production health, fetched client-side. This
 *     is the only part of the page that can be missing, and it says so rather
 *     than rendering zeros, because "0% fallback over 0 analyses" is arithmetic
 *     pretending to be information.
 *
 * The caveats section at the bottom is not decoration. n is 16; a page that
 * printed 75% without the standard error beside it would be overclaiming, and
 * the whole argument for building this harness was that unmeasured confidence is
 * what let a silent outage run for an unknown length of time.
 */

const { golden_set: golden, retrieval, verifier, judge, tests, prompt_versions } = benchmarks;

const pct = (v: number) => `${Math.round(v * 100)}%`;

/** Fixed decimal places, never trimmed. Trimming trailing zeros puts 0.1 next
 *  to 0.122 in the same column, which reads as two different precisions rather
 *  than one measurement and its floor -- and prints a κ of 1.00 as a bare "1".
 *  Metric values get three places, thresholds and κ get two. */
const dec = (v: number, places = 2) => v.toFixed(places);

function metricValue(m: { now: number; percent?: boolean }) {
  return m.percent ? pct(m.now) : dec(m.now, 3);
}

function metricBefore(m: { before: number; percent?: boolean }) {
  return m.percent ? pct(m.before) : dec(m.before, 3);
}

/** Change as a multiple rather than a percentage-point delta: "1.8x" reads
 *  honestly at these magnitudes where "+22pp" on a 0.27 baseline does not. */
function delta(before: number, now: number): string | null {
  if (!before) return null;
  const ratio = now / before;
  if (ratio > 1.02) return `${ratio.toFixed(1)}x`;
  if (ratio < 0.98) return `${ratio.toFixed(2)}x`;
  return "flat";
}

export default function BenchmarksPage() {
  const quality = useQuality(90);
  const recall = retrieval.metrics.find((m) => m.key === "recall_at_k");

  return (
    <main className="bench">
      <div className="bench__inner">
        <header className="bench__head">
          <Link className="bench__back" href="/">
            &#8592; Back to the shelf
          </Link>
          <h1>Is it any good?</h1>
          <p className="bench__lede">
            Lens hands you advice and claims every word of it traces back to a book. This page is
            the evidence for that claim, measured rather than asserted — and the places it is weak,
            said out loud.
          </p>
          <p className="bench__meta">
            Snapshot from commit <code>{benchmarks.commit ?? "unknown"}</code> ·{" "}
            {benchmarks.generated_at.replace("T", " ").replace("Z", " UTC")} · prompts{" "}
            <code>{prompt_versions.synthesis}</code> / <code>{prompt_versions.verification}</code> /{" "}
            <code>{prompt_versions.judge}</code>
          </p>
        </header>

        <section className="bench__why">
          <p>
            Before any of this, the repo had 124 passing tests. Every one proved the code{" "}
            <em>ran</em>. None measured whether the advice was <strong>good</strong> — and that
            mattered, because two provider bugs once made every LLM call fail for an unknown
            period and nothing looked broken. No errors, no complaints. The pipeline fails closed
            to a hand-written template, and the template is a perfectly reasonable answer.
          </p>
        </section>

        {/* --- retrieval ------------------------------------------------- */}
        <section className="bench__section">
          <h2>Retrieval</h2>
          <p className="bench__note">
            {golden.n} real situations taken from actual usage, each with a human-decided set of
            principles that <em>should</em> have surfaced. &ldquo;Before&rdquo; is what the app
            genuinely returned at the time, read from the recorded output — not a re-simulation —
            at the top-{retrieval.baseline_top_k} it was then using. &ldquo;Now&rdquo; is
            top-{retrieval.top_k}.
          </p>

          <ul className="bench__metrics">
            {retrieval.metrics.map((m) => {
              const d = delta(m.before, m.now);
              const passing = m.now >= m.floor;
              return (
                <li key={m.key} className="bench__metric">
                  <div className="bench__metric-head">
                    <span className="bench__metric-label">{m.label}</span>
                    {d ? <span className="bench__metric-delta">{d}</span> : null}
                  </div>
                  <div className="bench__metric-figure">
                    <span className="bench__metric-now">{metricValue(m)}</span>
                    <span className="bench__metric-before">from {metricBefore(m)}</span>
                  </div>
                  <div
                    className="bench__bar"
                    role="img"
                    aria-label={`${m.label} ${metricValue(m)}, floor ${m.floor}`}
                  >
                    {/* Bars are scaled to the ceiling where one exists, so recall
                        is drawn against what was actually achievable rather than
                        against an unreachable 1.0. */}
                    <span
                      className="bench__bar-fill"
                      style={{ width: `${Math.min(100, (m.now / (m.ceiling ?? 1)) * 100)}%` }}
                    />
                    <span
                      className="bench__bar-floor"
                      style={{ left: `${Math.min(100, (m.floor / (m.ceiling ?? 1)) * 100)}%` }}
                    />
                  </div>
                  <p className="bench__metric-foot">
                    <span className={passing ? "bench__ok" : "bench__bad"}>
                      {passing ? "above" : "below"} floor {dec(m.floor)}
                    </span>
                    {m.ceiling ? <> · ceiling {dec(m.ceiling)}</> : null}
                  </p>
                </li>
              );
            })}
          </ul>

          {recall ? (
            <p className="bench__callout">
              <strong>Read recall with its ceiling.</strong> Several cases expect up to{" "}
              {golden.expected_per_case_max} principles, so recall@{retrieval.top_k}{" "}
              <em>cannot</em> exceed {dec(recall.ceiling ?? 1)} on this set by construction.{" "}
              {dec(recall.now, 3)} is {pct(retrieval.recall_vs_ceiling)} of what was achievable. The
              original design target of {dec(recall.target ?? 0.8)} is not reachable at this k and is
              tracked as an aspiration, not a gate.
            </p>
          ) : null}

          <p className="bench__note">
            The jump came from finding a bug, not from tuning. A tag match added a flat 2.0 to a
            cosine similarity that maxes at 1.0, so one tag hit essentially always outranked pure
            semantic similarity. Fixing the tags is what exposed it — recall got <em>worse</em>{" "}
            after a data improvement, which is the sort of thing you only ever see if you are
            measuring. The replacement is Reciprocal Rank Fusion, which fuses ranks instead of two
            scales that were never comparable, at <code>tag_weight={retrieval.tag_match_weight}</code>{" "}
            and <code>rrf_k={retrieval.rrf_k}</code> — chosen by a 45-configuration sweep rather
            than by argument.
          </p>
        </section>

        {/* --- judge ----------------------------------------------------- */}
        <section className="bench__section">
          <h2>The judge, graded first</h2>
          <p className="bench__note">
            Faithfulness is scored by an LLM. So the LLM gets evaluated before it is trusted:
            Cohen&rsquo;s κ against {judge.n} hand-labelled cases spanning the whole rubric, with a
            hard floor of {dec(judge.kappa_floor)}. κ rather than raw agreement because a judge
            answering &ldquo;no hallucination&rdquo; unconditionally would score 81% agreement on
            this set while detecting nothing — κ subtracts chance agreement, so that judge scores
            zero.
          </p>

          <table className="bench__table">
            <thead>
              <tr>
                <th>rubric field</th>
                <th>κ</th>
                <th>raw agreement</th>
                <th>reading</th>
              </tr>
            </thead>
            <tbody>
              {judge.fields.map((f) => (
                <tr key={f.field}>
                  <td>
                    {f.field.replace(/_/g, " ")}
                    <span className="bench__kind">{f.kind}</span>
                  </td>
                  <td className="bench__figure">
                    {f.kappa === null ? "—" : dec(f.kappa)}
                    <span
                      className={
                        f.kappa !== null && f.kappa >= judge.kappa_floor
                          ? "bench__ok"
                          : "bench__bad"
                      }
                    >
                      {f.kappa !== null && f.kappa >= judge.kappa_floor ? "pass" : "fail"}
                    </span>
                  </td>
                  <td className="bench__figure">{pct(f.raw_agreement)}</td>
                  <td>{f.band}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <p className="bench__callout">
            <strong>Calibration earned its keep immediately.</strong> The first version of the
            judge cleared every κ floor and was still broken: across all {judge.n} cases it flagged
            a hallucination <em>if and only if</em> it had scored faithfulness 2 or below. The flag
            was a function of its own score, so its κ of 0.73 was re-measuring faithfulness rather
            than validating the flag — and nothing in the κ table showed that. The fix made the
            flag evidence-first: quote the invented phrase <em>before</em> assigning any score.
            Hallucination κ went 0.73 → 1.00, faithfulness κ dropped 0.95 → 0.87. One metric got
            worse and the instrument became trustworthy, which is the right trade.
          </p>
        </section>

        {/* --- verifier -------------------------------------------------- */}
        <section className="bench__section">
          <h2>Does the verifier ever refuse anything?</h2>
          <p className="bench__note">
            A verification step that never fails anything is not a verification step. These are
            deliberately broken outputs — invented statistics, over-length quotes, citations
            pointing at the wrong principle — seeded to check the checker.
          </p>
          <div className="bench__pair">
            <div className="bench__stat">
              <span className="bench__stat-value">{pct(verifier.catch_rate ?? 0)}</span>
              <span className="bench__stat-label">bad output caught</span>
              <span className="bench__stat-foot">
                {verifier.caught}/{verifier.seeded} · floor {pct(verifier.catch_rate_floor)}
              </span>
            </div>
            <div className="bench__stat">
              <span className="bench__stat-value">{pct(verifier.false_positive_rate ?? 0)}</span>
              <span className="bench__stat-label">good output wrongly refused</span>
              <span className="bench__stat-foot">
                {verifier.wrongly_rejected}/{verifier.clean_cases} · max{" "}
                {pct(verifier.false_positive_rate_max)}
              </span>
            </div>
          </div>
          <p className="bench__note">
            A false positive is not harmless: each one burns a synthesis retry, and enough of them
            hand you the fallback template instead of a real answer. A further{" "}
            {verifier.entailment_cases} cases test semantic grounding against a live model and run
            nightly rather than here, because they cost money and cannot be replayed.
          </p>
        </section>

        {/* --- live ------------------------------------------------------ */}
        <section className="bench__section">
          <h2>Right now, in production</h2>
          {quality === null ? (
            <p className="bench__pending">
              Asking the backend… it runs on a free tier and sleeps after 15 minutes idle, so this
              can take up to a minute. Everything above is a committed snapshot and needs no
              server.
            </p>
          ) : quality.total_analyses === 0 ? (
            <p className="bench__pending">
              No analyses in the last {quality.window_days} days, so there is nothing honest to
              report here yet. A percentage over zero requests would be arithmetic, not
              information.
            </p>
          ) : (
            <>
              <div className="bench__pair">
                <div className="bench__stat">
                  <span className="bench__stat-value">{pct(1 - quality.fallback_rate)}</span>
                  <span className="bench__stat-label">answers fully grounded</span>
                  <span className="bench__stat-foot">
                    {quality.total_analyses - quality.fallback_used}/{quality.total_analyses} over{" "}
                    {quality.window_days} days
                  </span>
                </div>
                <div className="bench__stat">
                  <span className="bench__stat-value">
                    {quality.first_attempt_pass_rate === null
                      ? "—"
                      : pct(quality.first_attempt_pass_rate)}
                  </span>
                  <span className="bench__stat-label">passed on the first attempt</span>
                  <span className="bench__stat-foot">
                    {quality.mean_synthesis_attempts === null
                      ? "no attempt data"
                      : `${quality.mean_synthesis_attempts} attempts on average`}
                  </span>
                </div>
              </div>
              {Object.keys(quality.issue_counts).length > 0 ? (
                <>
                  <h3 className="bench__subhead">What the verifier rejected</h3>
                  <ul className="bench__issues">
                    {Object.entries(quality.issue_counts).map(([issue, count]) => (
                      <li key={issue}>
                        <span>{issue.replace(/_/g, " ")}</span>
                        <span className="bench__figure">{count}</span>
                      </li>
                    ))}
                  </ul>
                </>
              ) : null}
              <p className="bench__note">
                Counts only. This endpoint is public, so it exposes no journal text, no reflection
                text and no principle text — by design and enforced by a test.
              </p>
            </>
          )}
        </section>

        {/* --- caveats ---------------------------------------------------- */}
        <section className="bench__section bench__section--weak">
          <h2>Where this is weak</h2>
          <p className="bench__note">
            Stated plainly, because a benchmark that oversells itself is worse than none.
          </p>
          <ul className="bench__weak">
            <li>
              <strong>n = {golden.n}.</strong> Everything above is directional, not established.
              The standard error on a rate at this sample size is roughly ±
              {Math.round(golden.standard_error_pp)} percentage points,
              so the difference between 75% and 68% is inside the noise.
            </li>
            <li>
              <strong>Assisted labelling.</strong> For each case a human accepted, edited or
              rejected an LLM-proposed shortlist rather than reading {golden.principles} principles
              by hand. Nobody labelled a control set blind, so discount for anchoring if you want
              to.
            </li>
            <li>
              <strong>One human rater.</strong> The κ above is judge-versus-human. Nobody has
              measured how much a second person would have disagreed with the first.
            </li>
            <li>
              <strong>The judge is calibrated, not yet applied.</strong> It agrees with a human on
              cases built to span the rubric. Running it over real pipeline output for all{" "}
              {golden.n} golden cases is the next step, and the threshold for it is already
              written down.
            </li>
            <li>
              <strong>Every case is a paraphrase.</strong> These are real people&rsquo;s journal
              entries, so none of the committed text is what anyone actually typed — it is a
              rewrite that preserves the situation, domain and emotional register, checked
              mechanically for shared phrasing and re-scored before and after to prove the meaning
              held.
            </li>
            <li>
              <strong>No crisis handling.</strong> Two of the {golden.n} real entries disclose
              suicidal ideation, and Lens answers them with book advice. The harness measured that;
              it cannot fix it.
            </li>
          </ul>
        </section>

        <footer className="bench__foot">
          <p>
            {tests?.default ?? "—"} tests, plus {tests?.eval ?? "—"} quality gates that run on every
            pull request with no API key and {tests?.eval_live ?? "—"} that call the real providers
            nightly. The harness adds zero dependencies. Reverting the retrieval fix fails the
            build.
          </p>
          <p className="bench__meta">
            Full method, and the reasoning behind each choice, in{" "}
            <a
              href="https://github.com/jack-does-trading/LENS/blob/main/docs/EVALUATION.md"
              target="_blank"
              rel="noopener noreferrer"
            >
              docs/EVALUATION.md
            </a>
            .
          </p>
          <Link className="bench__back" href="/">
            &#8592; Back to the shelf
          </Link>
        </footer>
      </div>
    </main>
  );
}
