import { test, expect } from "@playwright/test";
const model = {
  id: "m1",
  name: "Fraud",
  version: "1.0.0",
  language: "Python",
  dataset_id: "ulb_fraud",
  purpose: "Fraud",
  owner: null,
  lifecycle_status: "development",
  created_at: "2026-11-05T12:00:00Z",
};
for (const variant of ["missing", "scored", "unavailable", "agent_failed"] as const) {
test(`validation sections and ${variant} agent evidence`, async ({
  page,
}) => {
  const checks = [
    {
      check_id: "saved_metric_consistency",
      status: "PASS",
      required: true,
      summary: "Arithmetic crosscheck only",
      evidence_kind: "saved_evaluation_crosscheck",
      details: { does_not_recompute_test_metrics: true },
    },
    {
      check_id: "demographic_fairness",
      status: "NOT_ASSESSABLE",
      required: false,
      summary: "No protected attributes",
      evidence_kind: "saved_fairness",
      details: {},
    },
    {
      check_id: "migration_parity",
      status: "NOT_APPLICABLE",
      required: false,
      summary: "Independent Python fit",
      evidence_kind: "applicability",
      details: {},
    },
    {
      check_id: "fixture_scoring",
      status: "NOT_ASSESSABLE",
      required: true,
      summary: "Scorer skipped",
      evidence_kind: "fresh_execution",
      details: {},
    },
    {
      check_id: "future_psi_check",
      status: "REVIEW",
      required: false,
      summary: "New check retained",
      evidence_kind: "future_evidence",
      details: { value: 0.3 },
    },
  ];
  if (variant === "agent_failed") checks.push({
    check_id: "independent_validator_analysis", status: "NOT_ASSESSABLE", required: true,
    summary: "Independent agent validation did not complete; Gate 3 acceptance is blocked.",
    evidence_kind: "execution_status", details: {},
  });
  await page.route("**/api/v1/**", (route) => {
    if (new URL(route.request().url()).pathname === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "mock-user", username: "fixture-user", role: "admin", active: true } });
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/models")) return route.fulfill({ json: [model] });
    if (path.endsWith("/models/m1")) return route.fulfill({ json: model });
    if (path.endsWith("/reports"))
      return route.fulfill({
        json: [
          {
            id: "v1",
            model_id: "m1",
            run_id: "r1",
            kind: "validation",
            artifact_id: "a1",
            metrics: [],
            review_status: "decided",
            review_resolution: { decision: "accepted_with_conditions", rationale: "Restricted use reviewed",
              conditions: ["fixture_scoring: Require a controlled scoring run before wider use"], reviewer_id: "approver1" },
          },
        ],
      });
    if (path.endsWith("/download"))
      return route.fulfill({
        json: {
          schema_version: "1.0.0",
          report_kind: "legacy_model_validation",
          policy_version: "1.0.0",
          model_id: "fraud_logistic_python",
          domain: "ulb",
          task: "binary_classification",
          model_kind: "real_dataset",
          artifact_sha256: null,
          checks,
          ...(variant === "missing" ? {} : {
            agent_review: variant === "agent_failed"
              ? { status: "unavailable", error_code: "turn_limit_exceeded", exports: {} }
              : { ai_native_run_id: "harness-review-1", artifact_ids: ["artifact-review-1"], proposal_ids: ["proposal-review-1"], exports: { "validation_report.md": "## Agent assessment\n\n**Inspect** the reported evidence." } },
            challenger: variant === "scored" ? {
              validation: { auc: 0.81, n_rows: 5982, status: "REVIEW" },
              test: { auc: null, n_rows: 6000, status: "REVIEW" },
            } : { status: "NOT_ASSESSABLE", reason: "Validator did not export a challenger" },
          }),
          summary: {
            overall_status: "VALIDATION_INCOMPLETE",
            required_checks_passed: false,
            blocking_check_ids: ["fixture_scoring", ...(variant === "agent_failed" ? ["independent_validator_analysis"] : [])],
            review_check_ids: [
              "fixture_scoring",
              "demographic_fairness",
              "future_psi_check",
            ],
            human_approval_granted: false,
          },
          saved_metrics: {
            validation: { rows: 10, roc_auc: 0.75 },
            test: {
              rows: 20,
              roc_auc: 0.8,
              brier: 0,
              bootstrap: { roc_auc_ci95: [0.7, 0.9], resamples: 250 },
            },
          },
          limitations: ["Saved test previously inspected"],
          generated_at_utc: model.created_at,
          execution_requested: false,
        },
      });
    return route.fulfill({ status: 501, json: {} });
  });
  await page.goto("/?model=m1");
  await page.getByRole("tab", { name: "Validation", exact: true }).click();
  await page
    .getByRole("button", { name: "View available documents and reports" })
    .click();
  await page.getByRole("button", { name: "View validation evidence" }).click();
  await expect(page.getByRole("region", { name: "Recorded report decision" })).toContainText("fixture_scoring: Require a controlled scoring run before wider use");
  const agent = page.getByRole("region", { name: "Agent interpretation and challenger evidence" });
  if (variant === "missing") {
    await expect(agent).toContainText("No saved agent narrative");
    await expect(agent).toContainText("No challenger evaluation supplied");
  } else if (variant === "agent_failed") {
    await expect(agent.getByRole("status")).toContainText("Agent analysis unavailable");
    await expect(agent.getByRole("status")).toContainText("turn_limit_exceeded");
    await expect(agent.getByRole("status")).toContainText("review service determines which decisions are allowed");
    await expect(agent.getByRole("status")).toContainText("History");
    await expect(agent).not.toContainText("No saved agent narrative");
  } else {
    await expect(agent.getByRole("heading", { name: "Agent assessment", exact: true })).toBeVisible();
    await agent.getByText("Agent analysis source references", { exact: true }).click();
    const sources = agent.locator("details");
    await expect(sources).toContainText("validation_report.md");
    await expect(sources).toContainText("harness-review-1");
    await expect(sources).toContainText("proposal-review-1");
    await expect(sources).not.toContainText("Inspect the reported evidence");
    await expect(sources.locator("pre")).toHaveCount(0);
    if (variant === "scored") {
      const table = agent.getByRole("table", { name: "Challenger evaluation metrics" });
      await expect(table).toContainText("0.81");
      await expect(table).toContainText("5982");
      await expect(table.getByRole("row").filter({ has: page.getByRole("rowheader", { name: "test", exact: true }) })).toContainText("Not supplied");
    } else {
      await expect(agent).toContainText("NOT_ASSESSABLE");
      await expect(agent).toContainText("Validator did not export a challenger");
      await expect(agent.getByRole("table")).toHaveCount(0);
    }
  }
  const performance = page.getByRole("table", {
    name: "Saved performance metrics",
  });
  await expect(
    performance.getByRole("row").filter({ hasText: "ROC AUC" }),
  ).toContainText("0.75");
  await expect(
    performance.getByRole("row").filter({ hasText: "ROC AUC" }),
  ).toContainText("0.8");
  await expect(
    performance.getByRole("row").filter({ hasText: "Gini" }),
  ).toContainText("Not supplied");
  const calibration = page.getByRole("table", {
    name: "Saved calibration and uncertainty",
  });
  await expect(
    calibration
      .getByRole("row")
      .filter({
        has: page.getByRole("rowheader", { name: "test", exact: true }),
      }),
  ).toContainText("[0.7, 0.9]");
  const fairness = page.getByRole("region", { name: "Fairness", exact: true });
  await expect(
    fairness.getByText("NOT_ASSESSABLE", { exact: true }),
  ).toBeVisible();
  const parity = page.getByRole("region", {
    name: "Migration parity",
    exact: true,
  });
  await expect(
    parity.getByText("NOT_APPLICABLE (N/A)", { exact: true }),
  ).toHaveClass(/neutral/);
  await expect(
    page
      .getByRole("region", { name: "Other reported checks", exact: true })
      .getByRole("heading", { name: "future_psi_check", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Executable checks", exact: true }),
  ).toContainText("NOT_ASSESSABLE does not mean it ran");
  await page.getByRole("button", { name: "Stability", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Stability", exact: true }),
  ).toBeFocused();
  await expect(
    page.getByRole("region", { name: "Stability", exact: true }),
  ).toContainText("not a PSI result");
  await expect(
    page.getByRole("region", {
      name: "Findings requiring attention",
      exact: true,
    }),
  ).toContainText(
    "Severity, owner, due date and resolution state are not supplied",
  );
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

}

test("candidate validation rejection explains its own Gate 1 without retrying", async ({ page }) => {
  let submissions = 0;
  const message = "This rebuilt candidate is a separate model/version. Open its Documentation tab and have an independent reviewer accept its current Gate 1 document before starting validation.";
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { id: "validator1", username: "Validator", role: "validator", active: true } });
    if (path.endsWith("/models/m1/runs") && route.request().method() === "POST") {
      submissions++;
      return route.fulfill({ status: 409, json: { detail: { code: "gate_1_required", message, model_id: "m1", model_version: "1.0.0", document_run_id: "candidate-doc" } } });
    }
    if (path.endsWith("/models")) return route.fulfill({ json: [model] });
    if (path.endsWith("/models/m1")) return route.fulfill({ json: model });
    return route.fulfill({ json: [] });
  });
  await page.goto("/?model=m1");
  await page.getByRole("tab", { name: "Validation", exact: true }).click();
  const lifecycle = page.getByRole("region", { name: "Model lifecycle", exact: true });
  await lifecycle.getByRole("button", { name: "Run validation", exact: true }).click();
  await expect(lifecycle.getByRole("alert")).toContainText(message);
  await expect(lifecycle.getByRole("alert")).toContainText("No automatic retry");
  await page.getByRole("tab", { name: "Documentation", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Model documentation", exact: true })).toBeVisible();
  expect(submissions).toBe(1);
});
