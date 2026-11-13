import { it, expect } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { InputContractChecks } from "./InputContractChecks";

const check = { status: "PASS", exit_code: 1, reason: "Rejected malformed input" };
const evidence = { candidate_model_id: "candidate", candidate_version: "v2", candidate_bundle_sha256: "a".repeat(64),
  rebuild_run_id: "rebuild", parity_run_id: "parity", executed_at: "2026-11-11T10:00:00Z",
  passed: true, control: { status: "PASS", exit_code: 0, reason: "Scored unchanged rows" },
  checks: ["reject_missing_row_id", "reject_duplicated_row_id", "reject_target_leakage"].map(check_id => ({ ...check, check_id })) };
const render = (data?: unknown) => renderToStaticMarkup(<InputContractChecks evidence={data} candidateId="candidate" rebuildRunId="rebuild" />);
it("does not infer a pass from absent, malformed, duplicate or contradictory evidence", () => {
  expect(render()).toContain("not available in this review yet");
  for (const data of [{ passed: false, error: "Probe failed" }, { ...evidence, checks: [] },
    { ...evidence, checks: [evidence.checks[0], evidence.checks[0], evidence.checks[2]] },
    { ...evidence, passed: false }, { ...evidence, control: { ...evidence.control, status: "FAIL" } }]) {
    expect(render(data)).toContain("No passing result can be confirmed");
    expect(render(data)).not.toContain("All input checks passed.");
  }
});
it("shows the control and each probe separately without offering execution or approval", () => {
  const html = render(evidence);
  for (const label of ["Unchanged input control", "Missing row ID", "Duplicate row ID", "Target leakage", "Exit code 0", "All input checks passed."])
    expect(html).toContain(label);
  expect(html).not.toContain("<button");
  expect(html).not.toContain("<input");
  expect(html).toContain("separate from parity, validation and human approval");
});
it("keeps failed and not-run checks visible and escapes supplied reasons", () => {
  const failed = { ...evidence, passed: false, checks: [{ ...evidence.checks[0], status: "FAIL", exit_code: 0, reason: "<script>bad</script>" }, ...evidence.checks.slice(1)] };
  expect(render(failed)).toContain("Input checks did not all pass.");
  expect(render(failed)).toContain("&lt;script&gt;");
  const stopped = { ...evidence, passed: false, control: { status: "FAIL", exit_code: 0, reason: "Control failed" },
    checks: evidence.checks.map(c => ({ ...c, status: "NOT_RUN", exit_code: null })) };
  expect(render(stopped)).toContain("NOT_RUN");
  expect(render(stopped)).toContain("Control failed");
});
it("rejects foreign, unbound and impossible successful evidence without showing its identifiers", () => {
  for (const data of [
    { ...evidence, candidate_model_id: "other-candidate" }, { ...evidence, rebuild_run_id: "other-run" },
    { ...evidence, candidate_bundle_sha256: "bad" }, { ...evidence, candidate_version: "" },
    { ...evidence, executed_at: "bad" }, { ...evidence, parity_run_id: null },
    { ...evidence, control: { ...evidence.control, exit_code: 1 } },
    ...[0, 124, 137, null].map(exit_code => ({ ...evidence, checks: [{ ...evidence.checks[0], exit_code }, ...evidence.checks.slice(1)] })),
  ]) {
    expect(render(data)).toContain("No passing result can be confirmed");
    expect(render(data)).not.toContain("Candidate version:");
  }
});
