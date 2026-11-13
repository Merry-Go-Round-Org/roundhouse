import { test, expect } from "@playwright/test";
const model = {
  id: "m1",
  name: "Credit model",
  version: "1.0.0",
  language: "R",
  dataset_id: "uci_credit",
  purpose: "Default prediction",
  owner: null,
  lifecycle_status: "development",
  created_at: "2026-11-05T12:00:00Z",
};
const statuses = ["PASS", "FAIL", "REVIEW", "NOT_ASSESSABLE", "NOT_APPLICABLE"];
const evidence = {
  schema_version: "1.0.0",
  report_kind: "legacy_model_validation",
  policy_version: "1.0.0",
  model_id: "credit_logistic_r",
  domain: "uci",
  task: "binary_classification",
  model_kind: "real_dataset",
  artifact_sha256: "a".repeat(64),
  generated_at_utc: "2026-11-05T12:00:00Z",
  execution_requested: false,
  checks: statuses.map((status, i) => ({
    check_id: "check-" + i,
    status,
    required: i === 1,
    summary: "Check reason " + i,
    evidence_kind: "saved_evaluation_crosscheck",
    details: { interval: [0.7, 0.8] },
  })),
  summary: {
    overall_status: "VALIDATION_FAILED",
    required_checks_passed: false,
    blocking_check_ids: ["check-1"],
    review_check_ids: ["check-2"],
    human_approval_granted: false,
  },
  saved_metrics: { performance: { test: { auc: 0.75 } } },
  limitations: ["Historical saved evaluation only"],
};
test("validation submits once, filters reports, and displays canonical evidence without approval", async ({
  page,
}) => {
  let posts = 0;
  await page.route("**/api/v1/**", async (route) => {
    if (new URL(route.request().url()).pathname === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "mock-user", username: "fixture-user", role: "admin", active: true } });
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/models") return route.fulfill({ json: [model] });
    if (path === "/api/v1/models/m1") return route.fulfill({ json: model });
    if (path === "/api/v1/models/m1/runs") {
      posts++;
      expect(route.request().postDataJSON()).toEqual({
        operation: "validate",
        model_version: "1.0.0",
        dataset_id: "uci_credit",
      });
      return route.fulfill({
        status: 202,
        json: {
          id: "run1",
          model_id: "m1",
          model_version: "1.0.0",
          dataset_id: "uci_credit",
          operation: "validate",
          status: "awaiting_review",
          stage: "Review needed",
          error: null,
          created_at: model.created_at,
          started_at: null,
          finished_at: null,
        },
      });
    }
    if (path === "/api/v1/models/m1/reports")
      return route.fulfill({
        json: [
          {
            id: "doc",
            model_id: "m1",
            run_id: "run0",
            kind: "documentation",
            artifact_id: "doc1",
            metrics: [],
          },
          {
            id: "v1",
            model_id: "m1",
            run_id: "run1",
            kind: "validation",
            artifact_id: "evidence1",
            metrics: statuses.map((status) => ({
              name: status + " check",
              value: null,
              status,
              reason: "Explicit evidence",
              evidence_ids: [],
            })),
          },
        ],
      });
    if (path === "/api/v1/artifacts/evidence1/download")
      return route.fulfill({ json: evidence });
    return route.fulfill({ status: 501, json: { detail: "Unavailable" } });
  });
  await page.goto("/?model=m1");
  await page.getByRole("tab", { name: "Validation", exact: true }).click();
  await page
    .getByRole("button", { name: "Run validation", exact: true })
    .click();
  await expect(
    page.getByText("run1: awaiting_review · Review needed"),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Run validation", exact: true }),
  ).toBeDisabled();
  expect(posts).toBe(1);
  await page
    .getByRole("button", { name: "View available documents and reports" })
    .click();
  await expect(
    page.getByRole("heading", { name: "documentation · doc" }),
  ).toHaveCount(0);
  await expect(
    page.getByText("NOT_APPLICABLE (N/A)", { exact: true }),
  ).toHaveClass(/neutral/);
  await page.getByRole("button", { name: "View validation evidence" }).click();
  await expect(page.getByText("Blockers: check-1")).toBeVisible();
  await expect(page.getByText(/Human approval: not granted/)).toBeVisible();
  await expect(
    page.getByText("Historical saved evaluation only"),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
test("failed validation requests remain explicit and are not automatically resubmitted", async ({
  page,
}) => {
  let posts = 0;
  await page.route("**/api/v1/**", async (route) => {
    if (new URL(route.request().url()).pathname === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "mock-user", username: "fixture-user", role: "admin", active: true } });
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/models") return route.fulfill({ json: [model] });
    if (path === "/api/v1/models/m1") return route.fulfill({ json: model });
    if (path.endsWith("/runs") && route.request().method() === "POST") posts++;
    return route.fulfill({
      status: 501,
      json: { detail: "Validation execution unavailable" },
    });
  });
  await page.goto("/?model=m1");
  await page.getByRole("tab", { name: "Validation", exact: true }).click();
  await page
    .getByRole("button", { name: "Run validation", exact: true })
    .click();
  await expect(page.getByRole("alert").filter({ hasText: "No automatic retry" })).toContainText("Validation execution unavailable");
  await expect(page.getByRole("alert").filter({ hasText: "Document/report access" })).toBeVisible();
  expect(posts).toBe(1);
});
