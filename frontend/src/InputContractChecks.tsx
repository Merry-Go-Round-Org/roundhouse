/** Read-only presentation of persisted Gate 2 evidence bound to its candidate and run.
 * Never use this display to authorize a review decision or infer parity. */
const probeLabels = {
  reject_missing_row_id: "Missing row ID",
  reject_duplicated_row_id: "Duplicate row ID",
  reject_target_leakage: "Target leakage",
} as const;
type Result = { status: "PASS" | "FAIL" | "NOT_RUN"; exit_code: number | null; reason: string };
type Evidence = { passed: boolean; control: Result; checks: (Result & { check_id: keyof typeof probeLabels })[];
  candidate_model_id: string; candidate_version: string; candidate_bundle_sha256: string;
  rebuild_run_id: string; parity_run_id: string; executed_at: string };
const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
function result(value: unknown): value is Result {
  return object(value) && ["PASS", "FAIL", "NOT_RUN"].includes(String(value.status)) &&
    (value.exit_code === null || (typeof value.exit_code === "number" && Number.isInteger(value.exit_code))) &&
    typeof value.reason === "string";
}
function read(value: unknown, candidateId: string | null | undefined, rebuildRunId: string): Evidence | null {
  if (!object(value) || typeof value.passed !== "boolean" || !result(value.control) ||
      !["PASS", "FAIL"].includes(value.control.status) || !Array.isArray(value.checks) || value.checks.length !== 3) return null;
  if (!candidateId || value.candidate_model_id !== candidateId || value.rebuild_run_id !== rebuildRunId ||
      !["candidate_version", "parity_run_id", "executed_at"].every(key => typeof value[key] === "string" && !!value[key].trim()) ||
      !Number.isFinite(Date.parse(String(value.executed_at))) ||
      typeof value.candidate_bundle_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(value.candidate_bundle_sha256)) return null;
  const ids = new Set<string>();
  for (const check of value.checks) {
    if (!object(check)) return null;
    const id = check.check_id;
    if (typeof id !== "string" || !Object.hasOwn(probeLabels, id) || ids.has(id) || !result(check)) return null;
    ids.add(id);
  }
  const checks = value.checks as Evidence["checks"];
  // Preserve reported states, but refuse contradictory success summaries.
  const passed = value.control.status === "PASS" && checks.every(check => check.status === "PASS");
  if (passed !== value.passed || (value.control.status === "FAIL" && checks.some(check => check.status !== "NOT_RUN"))) return null;
  const control = value.control;
  if (control.exit_code === null || (control.status === "PASS" && control.exit_code !== 0) ||
      checks.some(check => check.status === "NOT_RUN" ? check.exit_code !== null || control.status !== "FAIL"
        : check.exit_code === null || (check.status === "PASS" && [0, 124, 137].includes(check.exit_code)))) return null;
  return value as Evidence;
}
export function InputContractChecks({ evidence, candidateId, rebuildRunId }: {
  evidence?: unknown; candidateId?: string | null; rebuildRunId: string;
}) {
  const data = read(evidence, candidateId, rebuildRunId);
  const unavailable = evidence === undefined || evidence === null;
  return <section className="lifecycle-card" aria-label="Rebuild input checks">
    <h3>Rebuild input checks</h3>
    {unavailable ? <p>Saved input-check results are not available in this review yet. Parity does not prove that malformed inputs are rejected.</p>
      : !data ? <p role="status">Input-check results are unavailable or incomplete. No passing result can be confirmed here.</p>
      : <>
        <p>{data.passed ? "All input checks passed." : "Input checks did not all pass."}</p>
        <p>Candidate version: {data.candidate_version} · Executed: {data.executed_at}</p>
        <dl>
          {[{ check_id: "control", label: "Unchanged input control", ...data.control },
            ...data.checks.map(check => ({ ...check, label: probeLabels[check.check_id] }))].map(check => <div key={check.check_id}>
            <dt>{check.label}</dt><dd><strong>{check.status}</strong> — {check.reason || "No explanation supplied"}
              {check.exit_code !== null && <span> · Exit code {check.exit_code}</span>}</dd>
          </div>)}
        </dl>
        <p>These development-input checks are separate from parity, validation and human approval.</p>
        <p>Gate 2 eligibility follows official parity. Refresh reads saved results; Run parity does not repeat these probes.</p>
        <details><summary>Input-check evidence identifiers</summary>
          <p>Candidate: {data.candidate_model_id} · Bundle SHA-256: {data.candidate_bundle_sha256}</p>
          <p>Rebuild run: {data.rebuild_run_id} · Agent parity run: {data.parity_run_id}</p>
        </details>
      </>}
  </section>;
}
