import { test, expect } from "@playwright/test";

const sha = "a".repeat(64);
const metric = (psi: number, band: "green" | "amber" | "red" = "green") => ({
  status: band === "green" ? "PASS" : "REVIEW",
  band,
  psi,
  reference_rows: 200,
  current_rows: 120,
  reason: "PSI is descriptive drift evidence",
});
const psi = {
  schema_version: "1.0.0",
  kind: "model_stability_window",
  model_id: "credit_xgboost_python",
  artifact_sha256: sha,
  baseline_sha256: sha,
  policy_version: "1.0.0",
  reference_scope: "development",
  reference_rows: 200,
  current_rows: 120,
  status: "REVIEW",
  band: "red",
  score_band: "green",
  feature_band: "red",
  revalidation_required: true,
  revalidation_run_id: "validation-run-1",
  red_features: ["LIMIT_BAL"],
  score: metric(0.04),
  features: { LIMIT_BAL: metric(0.31, "red"), AGE: metric(0.11, "amber") },
  limitations: [
    "Marginal shift only: no outcomes, performance, calibration or concept drift assessed",
    "Thresholds are project heuristics",
    "Frozen bins cannot be refitted",
  ],
  simulated: true,
  shift: { feature: "LIMIT_BAL", factor: 1.5 },
  dataset_version_id: "dataset-version-1",
  package_sha256: sha,
  runtime_image: "sha256:fixture",
  input_scope: "test_partition_simulation",
  baseline_partition: "train",
  window_label: "Declared test shift",
};
const event = {
  id: "event-1",
  model_id: "credit-id",
  run_id: "monitor-run-1",
  created_at: "2026-11-07T13:00:00Z",
  payload: psi,
};
const model = {
  id: "credit-id",
  name: "Credit model",
  version: "1.0.0",
  language: "Python",
  dataset_id: "uci_credit",
  purpose: "Credit default",
  owner: null,
  lifecycle_status: "active",
  created_at: "2026-11-07T11:00:00Z",
  inventory: {
    lifecycle_stage: "approved",
    manifest: {
      task: "binary_classification",
      feature_order: ["LIMIT_BAL", "AGE"],
    },
    bundle_sha256: sha,
    dataset_version_id: "dataset-version-1",
    approval: {
      review_id: "review-gate3",
      decision: "accepted",
      reviewer_id: "independent-approver",
      conditions: [],
    },
    applicability: {
      document: "applicable",
      rebuild: "applicable",
      validate: "applicable",
      monitor: "applicable",
    },
  },
};
const dashboard = {
  models: 2,
  packages: 1,
  reports: 3,
  monitoring_events: 1,
  runs: { completed: 4, awaiting_review: 1 },
  scoring_runs: {},
  reviews: { pending: 1, decided: 1 },
  findings: { open: 2 },
  lifecycle: { active: 1, revalidation_due: 1 },
  stage_elapsed_seconds: { monitor: { count: 1, mean: 15, total: 15 } },
  review_elapsed_seconds: { count: 1, mean: 20, total: 20 },
  llm_cost_usd: 0.015,
  red_alerts: 1,
  source: "persisted_records",
  measurement_limitations: [
    "Elapsed times are not person-hours or active review effort",
  ],
};
const monitoringRun = {
  id: "monitor-queued",
  model_id: "credit-id",
  model_version: "1.0.0",
  dataset_id: "uci_credit",
  operation: "monitor",
  status: "queued",
  created_at: "2026-11-07T13:20:00Z",
  started_at: null,
  finished_at: null,
  stage: null,
  error: null,
};
const user = (role = "admin") => ({
  id: "operator",
  username: "operator",
  role,
  active: true,
});

test("all live pages and model tabs remain usable at desktop and mobile widths", async ({
  page,
}) => {
  test.setTimeout(120000);
  const review = {
    id: "review-1",
    model_id: model.id,
    model_name: model.name,
    model_version: model.version,
    run_id: "r1",
    gate: 1,
    status: "pending",
    artifact_id: "doc-artifact",
    artifact_version: "1",
    created_at: "2026-11-07T10:00:00Z",
    resolution: null,
    requested_by: "another-user",
    approval_available: true,
    blockers: [],
  };
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/me")) return route.fulfill({ json: user() });
    if (path.endsWith("/auth/users"))
      return route.fulfill({
        json: [
          user(),
          {
            id: "reviewer",
            username: "Independent reviewer",
            role: "approver",
            active: true,
          },
        ],
      });
    if (path.endsWith("/models"))
      return route.fulfill({
        json: [
          model,
          {
            ...model,
            id: "model-two",
            name: "Retail credit portfolio — long descriptive model title for layout review",
            owner: "Model development and independent oversight team",
          },
        ],
      });
    if (path.endsWith("/models/credit-id"))
      return route.fulfill({ json: model });
    if (path.endsWith("/dashboard")) return route.fulfill({ json: dashboard });
    if (path.endsWith("/models/credit-id/monitoring"))
      return route.fulfill({ json: [event] });
    if (path.endsWith("/monitoring"))
      return route.fulfill({
        json: [
          {
            model_id: model.id,
            model_name: model.name,
            lifecycle_status: "revalidation_due",
            latest: psi,
            last_run: "2026-11-07T13:00:00Z",
          },
        ],
      });
    if (path.endsWith("/datasets"))
      return route.fulfill({
        json: [
          {
            id: "uci_credit",
            name: "UCI credit",
            filename: "credit.xls",
            target_description: "Default next month",
            limitations: [
              "Historical population; review representativeness before use",
            ],
          },
        ],
      });
    if (path.endsWith("/reviews")) return route.fulfill({ json: [review] });
    if (path.endsWith("/reports"))
      return route.fulfill({
        json: [
          {
            id: "doc1",
            model_id: model.id,
            run_id: "r1",
            kind: "documentation",
            artifact_id: "doc-artifact",
            metrics: [],
          },
        ],
      });
    if (path.endsWith("/download"))
      return route.fulfill({
        contentType: "text/markdown",
        body:
          "# Credit model documentation\n\n## Purpose\nReview credit default predictions.\n\n## Limitations\nDevelopment evidence only.\n\n| Field | Value |\n| --- | --- |\n| Input hash | " +
          "a".repeat(64) +
          " |",
      });
    if (path.endsWith("/runs"))
      return route.fulfill({
        json: [
          {
            ...monitoringRun,
            status: "failed",
            error:
              "Scoring unavailable. Refresh saved state before requesting another execution.",
          },
        ],
      });
    if (path.endsWith("/findings/owners")) return route.fulfill({ json: [] });
    if (path.endsWith("/findings"))
      return route.fulfill({
        json: [
          {
            id: "f1",
            model_id: model.id,
            report_id: "report1",
            check_id: "fairness",
            severity: "review",
            status: "open",
            summary: "Review fairness across the approved population",
            owner_id: null,
            due_date: null,
            comments: [],
          },
        ],
      });
    return route.fulfill({ json: [] });
  });
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/");
    async function capture(name: string) {
      await page.evaluate(() => document.fonts.ready);
      await expect(
        page.getByRole("button", { name: "Sign out", exact: true }),
      ).toBeVisible();
      const brand = await page.locator(".sidebar .brand").boundingBox();
      expect
        .soft(brand?.x, "Brand must not scroll out of the viewport")
        .toBeGreaterThanOrEqual(0);
      for (const button of await page
        .locator(
          ".workspace-main > .form-actions button, .workspace-main > div > .form-actions button",
        )
        .all()) {
        const box = await button.boundingBox();
        if (box) {
          expect.soft(box.x).toBeGreaterThanOrEqual(0);
          expect.soft(box.x + box.width).toBeLessThanOrEqual(width);
        }
      }
      await page.screenshot({
        path: `test-results/visual-audit/${width}-${name}.png`,
        fullPage: true,
      });
      expect
        .soft(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
          `${width} ${name}: page overflow`,
        )
        .toBe(true);
    }
    await expect(
      page.getByRole("button", { name: "Credit model", exact: true }),
    ).toBeVisible();
    await capture("inventory");
    for (const name of [
      "Datasets",
      "Review queue",
      "Monitoring",
      "Dashboard",
      "Accounts",
    ]) {
      await page.getByRole("button", { name, exact: true }).click();
      await expect(
        page.getByRole("heading", { level: 1, name, exact: true }),
      ).toBeVisible();
      await expect(
        page.getByText(
          /Loading (datasets|reviews|monitoring portfolio|dashboard|accounts)…/,
        ),
      ).toHaveCount(0);
      if (name === "Review queue")
        await page.getByRole("button", { name: "Inspect review-1" }).click();
      if (name === "Datasets") {
        await page
          .getByRole("button", { name: "Upload dataset", exact: true })
          .click();
        await expect(page.getByRole("dialog")).toBeVisible();
        await capture("upload-dataset");
        await page.getByRole("button", { name: "Close", exact: true }).click();
      }
      if (name === "Monitoring")
        await page.getByRole("button", { name: "Inspect latest PSI" }).click();
      await capture(name.toLowerCase().replaceAll(" ", "-"));
    }
    await page.goto("/?model=credit-id");
    for (const tab of [
      "Overview",
      "Documentation",
      "Rebuild & parity",
      "Validation",
      "Monitoring",
      "History",
    ]) {
      await page.getByRole("tab", { name: tab, exact: true }).click();
      await expect(
        page.getByRole("tab", { name: tab, exact: true }),
      ).toHaveAttribute("aria-selected", "true");
      if (tab === "Documentation") {
        await page
          .getByRole("button", { name: "View available documents and reports" })
          .click();
        await page
          .getByRole("button", { name: "Read documentation", exact: true })
          .click();
        await expect(
          page.getByRole("region", { name: "Document reader", exact: true }),
        ).toBeVisible();
      }
      if (tab === "Validation")
        await page
          .getByRole("button", { name: "View persisted findings" })
          .click();
      await capture("model-" + tab.toLowerCase().replaceAll(/[^a-z]+/g, "-"));
    }
    await page
      .getByRole("button", { name: "Upload model", exact: true })
      .click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await capture("upload-model");
  }
});

test("sign-in and labelled demo layout", async ({ page }) => {
  await page.route("**/api/v1/**", (route) =>
    route.fulfill({ status: 401, json: { detail: "Sign in required" } }),
  );
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "Sign in to the workspace" }),
    ).toBeVisible();
    await page.screenshot({
      path: `test-results/visual-audit/${width}-login.png`,
      fullPage: true,
    });
    await page.goto("/demo");
    await expect(page.locator(".sidebar .brand")).toBeVisible();
    await page.screenshot({
      path: `test-results/visual-audit/${width}-demo.png`,
      fullPage: true,
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  }
});
