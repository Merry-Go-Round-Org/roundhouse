// Official parity runs, as returned by backend parity_service.parity_read.
// Parity is one platform check: the rebuild agent and a person run the same function,
// so a person rerunning it on the same candidate reproduces the agent's result.
export interface ParityWorstRow {
  row_id: string;
  absolute_difference: number;
}
export interface ParityResult {
  method?: string;
  tolerance_absolute?: number;
  tolerance_relative?: number;
  n_rows?: number;
  n_above_tolerance?: number;
  max_absolute_difference?: number | null;
  worst_rows?: ParityWorstRow[];
  limitation?: string;
  [key: string]: unknown;
}
export interface ParityRun {
  id: string;
  candidate_model_id: string;
  candidate_version: string;
  candidate_bundle_sha256: string;
  parent_model_id: string;
  parent_version: string;
  rebuild_run_id: string | null;
  initiator: "agent" | "human";
  actor_id: string;
  status: "completed" | "failed";
  passed: boolean;
  error: string | null;
  result: ParityResult;
  created_at: string;
}

function text(v: unknown): v is string {
  return typeof v === "string" && !!v.trim();
}
function finite(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

export function readParityRun(value: unknown): ParityRun {
  const invalid = () => {
    throw new Error("Parity run does not match the backend contract.");
  };
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  const v = value as Record<string, unknown>;
  if (
    ![
      "id", "candidate_model_id", "candidate_version", "candidate_bundle_sha256",
      "parent_model_id", "parent_version", "actor_id",
    ].every((k) => text(v[k])) ||
    !["agent", "human"].includes(String(v.initiator)) ||
    !["completed", "failed"].includes(String(v.status)) ||
    typeof v.passed !== "boolean" ||
    (v.status === "failed" && v.passed) ||
    !(v.rebuild_run_id === null || text(v.rebuild_run_id)) ||
    !(v.error === null || typeof v.error === "string") ||
    !text(v.created_at) || !Number.isFinite(Date.parse(v.created_at)) ||
    !v.result || typeof v.result !== "object" || Array.isArray(v.result)
  )
    return invalid();
  const r = v.result as Record<string, unknown>;
  if (
    (r.max_absolute_difference !== undefined && r.max_absolute_difference !== null &&
      !finite(r.max_absolute_difference)) ||
    (r.worst_rows !== undefined &&
      (!Array.isArray(r.worst_rows) ||
        !r.worst_rows.every((w) => !!w && typeof w === "object" &&
          text((w as ParityWorstRow).row_id) && finite((w as ParityWorstRow).absolute_difference))))
  )
    return invalid();
  return v as unknown as ParityRun;
}

export function readParityRuns(value: unknown, modelId: string): ParityRun[] {
  if (!Array.isArray(value)) throw new Error("Parity run list must be an array.");
  const runs = value.map(readParityRun);
  if (runs.some((r) => r.candidate_model_id !== modelId && r.parent_model_id !== modelId))
    throw new Error("Backend returned parity runs for another model.");
  return runs;
}

/** The outcome a person reads first: a pass, a fail, or a run that could not score. */
export function parityOutcome(run: ParityRun): "PASS" | "FAIL" | "ERROR" {
  if (run.status === "failed") return "ERROR";
  return run.passed ? "PASS" : "FAIL";
}

/** The candidate a person would rerun parity on: the newest one seen in the history. */
export function latestCandidate(runs: ParityRun[]): { id: string; version: string } | null {
  const run = runs[0];
  return run ? { id: run.candidate_model_id, version: run.candidate_version } : null;
}

/** A rebuild candidate's manifest names its parent; other models have no parity of their own. */
export function derivedFrom(manifest: Record<string, unknown> | undefined): string | null {
  const source = manifest?.derived_from;
  if (!source || typeof source !== "object" || Array.isArray(source)) return null;
  const id = (source as Record<string, unknown>).model_id;
  return text(id) ? id : null;
}
