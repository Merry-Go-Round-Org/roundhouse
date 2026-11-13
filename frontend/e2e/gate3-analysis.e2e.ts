import { test, expect, type Page } from "@playwright/test";
const date = "2026-11-08T12:00:00Z";
const review = {
  id: "q3", model_id: "m1", model_name: "Credit", model_version: "1",
  run_id: "validation1", gate: 3, status: "pending", artifact_id: "report1",
  artifact_version: "v1", created_at: date, resolution: null, requested_by: "validator1",
};
const proposal = {
  id: "finding-proposal", model_id: "m1", source_run_id: "validation1",
  source_task_id: "task1", conversation_id: null, type: "open_finding",
  args: { title: "Review missing assumptions", severity: "medium" },
  rationale: "Confirm the population assumptions", evidence: [{ kind: "check", ref: "population" }],
  required_role: "validator", preconditions: { model_version: "1" },
  status: "pending", decision: null, result: null, created_at: date,
};
async function setup(page: Page, mismatch: "none" | "list" | "detail" = "none", failedAnalysis = false, hardFailure = false) {
  const savedReview = failedAnalysis ? { ...review, approval_available: false, blockers: ["independent_validator_analysis"] } : review;
  const writes: string[] = [], queries: URL[] = [];
  await page.route("**/api/v1/**", route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    if (request.method() !== "GET") writes.push(path);
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { id: "approver1", username: "Reviewer", role: "approver", active: true } });
    if (path.endsWith("/reviews")) return route.fulfill({ json: [savedReview] });
    if (path.endsWith("/reviews/q3")) return route.fulfill({ json: savedReview });
    if (path.endsWith("/artifacts/report1/download")) return route.fulfill({ json: {
      schema_version: "1.0.0", report_kind: "legacy_model_validation", policy_version: "1.0.0",
      model_id: "legacy-credit", domain: "uci", task: "binary_classification", model_kind: "real_dataset",
      artifact_sha256: null, generated_at_utc: date, execution_requested: true,
      checks: [{ check_id: "population", status: "REVIEW", required: false, summary: "Review population", evidence_kind: "fresh_execution", details: {} }, ...(failedAnalysis ? [
        { check_id: "independent_validator_analysis", status: "NOT_ASSESSABLE", required: true, summary: "Independent agent validation did not complete.", evidence_kind: "execution_status", details: { error_code: "turn_limit_exceeded" } },
        { check_id: "split_provenance", status: "REVIEW", required: true, summary: "Original fit rows cannot be verified", evidence_kind: "provenance", details: {} },
      ] : []), ...(hardFailure ? [{ check_id: "reject_target_leakage", status: "FAIL", required: true, summary: "Scorer accepted the target column", evidence_kind: "fresh_execution", details: {} }] : [])],
      summary: { overall_status: hardFailure ? "VALIDATION_FAILED" : failedAnalysis ? "VALIDATION_INCOMPLETE" : "REVIEW_REQUIRED", required_checks_passed: !failedAnalysis && !hardFailure, blocking_check_ids: [...(failedAnalysis ? ["independent_validator_analysis", "split_provenance"] : []), ...(hardFailure ? ["reject_target_leakage"] : [])], review_check_ids: ["population", ...(failedAnalysis ? ["independent_validator_analysis", "split_provenance"] : [])], human_approval_granted: false },
      saved_metrics: {}, limitations: [],
      agent_review: failedAnalysis ? { status: "unavailable", error_code: "turn_limit_exceeded", exports: {} } : { exports: { "validation_report.md": "## Validator assessment\n\nInspect the **population assumptions**." } },
      challenger: { status: "NOT_ASSESSABLE", reason: "No challenger supplied" },
    } });
    if (path.endsWith("/proposals")) {
      queries.push(url);
      return route.fulfill({ json: [{ ...proposal, source_run_id: mismatch === "list" ? "another-run" : "validation1" }] });
    }
    if (path.endsWith("/proposals/finding-proposal")) return route.fulfill({ json: { ...proposal, source_run_id: mismatch === "detail" ? "another-run" : "validation1" } });
    if (path.endsWith("/proposals/actions")) return route.fulfill({ json: { open_finding: { required_role: "validator", execution_available: true, args_schema: {} } } });
    return route.fulfill({ json: [] });
  });
  return { writes, queries };
}
async function openReview(page: Page, chat = false) {
  await page.goto("/");
  if (chat) {
    await page.getByRole("button", { name: "Open agent chat", exact: true }).click();
    await page.getByRole("button", { name: "Open review cards", exact: true }).click();
  } else await page.getByRole("button", { name: "Review queue", exact: true }).click();
  await page.getByRole("button", { name: "Inspect q3", exact: true }).click();
}
for (const chat of [false, true]) test(`Gate 3 ${chat ? "chat card" : "review queue"} shows narrative and run-scoped proposals without writes`, async ({ page }) => {
  const state = await setup(page);
  await openReview(page, chat);
  await page.getByRole("button", { name: "Preview review artifact" }).click();
  await expect(page.getByRole("heading", { name: "Validator assessment", exact: true })).toBeVisible();
  await expect(page.getByText("No challenger supplied", { exact: true })).toBeVisible();
  expect(state.queries).toHaveLength(0);
  await page.getByText("Agent proposals for this validation run", { exact: true }).click();
  await page.getByRole("button", { name: /Open a validation finding/ }).click();
  await expect(page.getByRole("region", { name: "Saved proposal", exact: true })).toContainText("Confirm the population assumptions");
  await expect(page.getByRole("region", { name: "Saved proposal", exact: true })).toContainText("requires the validator role");
  expect(state.queries[0].searchParams.get("source_run_id")).toBe("validation1");
  expect(state.queries[0].searchParams.get("model_id")).toBe("m1");
  await expect(page.getByRole("button", { name: "Submit decision", exact: true })).toBeDisabled();
  expect(state.writes).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
for (const mismatch of ["list", "detail"] as const) test(`Gate 3 rejects another run's proposal in ${mismatch} response`, async ({ page }) => {
  const state = await setup(page, mismatch);
  await openReview(page);
  await page.getByText("Agent proposals for this validation run", { exact: true }).click();
  if (mismatch === "detail") await page.getByRole("button", { name: /Open a validation finding/ }).click();
  await expect(page.getByRole("alert")).toContainText(mismatch === "list" ? "does not match" : "another context");
  await expect(page.getByRole("button", { name: "Submit proposal decision" })).toHaveCount(0);
  expect(state.writes).toEqual([]);
});

for (const chat of [false, true]) test(`Gate 3 unavailable analysis stays blocked in ${chat ? "chat" : "queue"}`, async ({ page }) => {
  const state = await setup(page, "none", true);
  await openReview(page, chat);
  await page.getByRole("button", { name: "Preview review artifact" }).click();
  const agent = page.getByRole("region", { name: "Agent interpretation and challenger evidence" });
  await expect(agent.getByRole("status")).toContainText("turn_limit_exceeded");
  const requirements = page.getByRole("region", { name: "Gate 3 requirements", exact: true });
  await expect(requirements.getByRole("region", { name: "Hard failures", exact: true })).toContainText("No FAIL checks");
  const limitations = requirements.getByRole("region", { name: "Required limitations", exact: true });
  await expect(limitations).toContainText("independent_validator_analysis · NOT_ASSESSABLE");
  await expect(limitations).toContainText("split_provenance · REVIEW");
  await expect(limitations).not.toContainText("population");
  await expect(page.getByText(/Backend blocking checks: independent_validator_analysis/)).toBeVisible();
  await expect(page.getByRole("option", { name: "Accepted", exact: true })).toHaveJSProperty("disabled", true);
  await expect(page.getByRole("option", { name: "Accepted with conditions", exact: true })).toHaveJSProperty("disabled", true);
  await expect(page.getByRole("button", { name: "Submit decision", exact: true })).toBeDisabled();
  expect(state.writes).toEqual([]);
});

test("Gate 3 report separates hard failures from limitations without changing decision permissions", async ({ page }) => {
  const state = await setup(page, "none", true, true);
  await openReview(page);
  await page.getByRole("button", { name: "Preview review artifact" }).click();
  const requirements = page.getByRole("region", { name: "Gate 3 requirements", exact: true });
  const failures = requirements.getByRole("region", { name: "Hard failures", exact: true });
  await expect(failures).toContainText("reject_target_leakage · FAIL");
  await expect(failures).not.toContainText("split_provenance");
  const limitations = requirements.getByRole("region", { name: "Required limitations", exact: true });
  await expect(limitations).toContainText("split_provenance · REVIEW");
  await expect(limitations).not.toContainText("reject_target_leakage");
  await expect(page.getByRole("option", { name: "Accepted with conditions", exact: true })).toHaveJSProperty("disabled", true);
  expect(state.writes).toEqual([]);
});
