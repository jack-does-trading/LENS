import raw from "./benchmarks.json";

/**
 * The committed eval snapshot, given an explicit shape.
 *
 * TypeScript infers the type of an imported JSON literal structurally, and the
 * four entries in `retrieval.metrics` are not homogeneous -- only recall carries
 * `ceiling` and `target`, only hit rate carries `percent` -- so the inferred
 * element type is a union and every access to an optional field needs narrowing
 * at the call site. Declaring the shape once here, with the optional fields
 * marked optional, is the difference between one documented cast and a dozen
 * `"ceiling" in m` checks scattered through the page.
 *
 * The cast is safe for the reason the file exists at all:
 * backend/tests/test_benchmarks_snapshot.py recomputes every field in the same
 * `pytest -m eval` run that enforces the quality floors, so a shape change in
 * scripts/export_benchmarks.py fails CI rather than reaching a visitor.
 */

export type BenchMetric = {
  key: string;
  label: string;
  now: number;
  before: number;
  floor: number;
  /** Present only where k is smaller than the largest expected set, i.e. recall. */
  ceiling?: number;
  target?: number;
  /** Render as a percentage rather than a bare figure. */
  percent?: boolean;
};

export type Benchmarks = {
  generated_at: string;
  commit: string | null;
  prompt_versions: { synthesis: string; verification: string; judge: string };
  golden_set: {
    n: number;
    books: number;
    principles: number;
    embedding_dimension: number;
    expected_per_case_min: number;
    expected_per_case_max: number;
    all_redacted: boolean;
    standard_error_pp: number;
  };
  retrieval: {
    top_k: number;
    baseline_top_k: number;
    candidates: number;
    tag_match_weight: number;
    rrf_k: number;
    metrics: BenchMetric[];
    recall_vs_ceiling: number;
  };
  verifier: {
    catch_rate: number | null;
    caught: number;
    seeded: number;
    catch_rate_floor: number;
    false_positive_rate: number | null;
    wrongly_rejected: number;
    clean_cases: number;
    false_positive_rate_max: number;
    known_gaps: string[];
    entailment_cases: number;
    prompt_version: string;
  };
  judge: {
    rubric_version: string;
    model: string;
    kappa_floor: number;
    n: number;
    fields: {
      field: string;
      kind: string;
      kappa: number | null;
      raw_agreement: number;
      band: string;
    }[];
  };
  tests: { default: number | null; eval: number | null; eval_live: number | null } | null;
};

export const benchmarks = raw as unknown as Benchmarks;
