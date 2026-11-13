import { test, expect } from "@playwright/test";

const sha = "a".repeat(64);
const metric = (psi: number, band: "green" | "amber" | "red" = "green") => ({
  status: band === "green" ? "PASS" : "REVIEW",
  band, psi, reference_rows: 200, current_rows: 120,
  reason: "PSI is descriptive drift evidence",
});
const psi = {
  schema_version: "1.0.0", kind: "model_stability_window",
  model_id: "credit_xgboost_python", artifact_sha256: sha, baseline_sha256: sha,
  policy_version: "1.0.0", reference_scope: "development",
  reference_rows: 200, current_rows: 120,
  status: "REVIEW", band: "red", score_band: "green", feature_band: "red",
  revalidation_required: true, revalidation_run_id: "validation-run-1",
  red_features: ["LIMIT_BAL"], score: metric(0.04),
  features: { LIMIT_BAL: metric(0.31, "red"), AGE: metric(0.11, "amber") },
  limitations: [
    "Marginal shift only: no outcomes, performance, calibration or concept drift assessed",
    "Thresholds are project heuristics",
    "Frozen bins cannot be refitted",
  ],
  simulated: true, shift: { feature: "LIMIT_BAL", factor: 1.5 },
  dataset_version_id: "dataset-version-1", package_sha256: sha,
  runtime_image: "sha256:fixture", input_scope: "validation_partition_simulation",
  baseline_partition: "train", window_label: "Declared test shift",
};
const event = { id: "event-1", model_id: "credit-id", run_id: "monitor-run-1",
  created_at: "2026-11-07T13:00:00Z", payload: psi };
const model = {
  id: "credit-id", name: "Credit model", version: "1.0.0", language: "Python",
  dataset_id: "uci_credit", purpose: "Credit default", owner: null,
  lifecycle_status: "active", created_at: "2026-11-07T11:00:00Z",
  inventory: {
    lifecycle_stage: "approved", manifest: { task: "binary_classification",
      feature_order: ["LIMIT_BAL", "AGE"] },
    bundle_sha256: sha, dataset_version_id: "dataset-version-1",
    approval: { review_id: "review-gate3", decision: "accepted",
      reviewer_id: "independent-approver", conditions: [] },
    applicability: { document: "applicable", rebuild: "applicable",
      validate: "applicable", monitor: "applicable" },
  },
};
const dashboard = {
  models: 2, packages: 1, reports: 3, monitoring_events: 1,
  runs: { completed: 4, awaiting_review: 1 },
  scoring_runs: {}, reviews: { pending: 1, decided: 1 },
  findings: { open: 2 }, lifecycle: { active: 1, revalidation_due: 1 },
  stage_elapsed_seconds: { monitor: { count: 1, mean: 15, total: 15 } },
  review_elapsed_seconds: { count: 1, mean: 20, total: 20 },
  llm_cost_usd: 0.015, red_alerts: 1, source: "persisted_records",
  measurement_limitations: ["Elapsed times are not person-hours or active review effort"],
};
const monitoringRun = {
  id: "monitor-queued", model_id: "credit-id", model_version: "1.0.0",
  dataset_id: "uci_credit", operation: "monitor", status: "queued",
  created_at: "2026-11-07T13:20:00Z", started_at: null,
  finished_at: null, stage: null, error: null,
};
const user = (role = "admin") => ({ id: "operator", username: "operator", role, active: true });

test("P2 dashboard and monitoring portfolio show only authenticated persisted records", async ({ page }) => {
  const seen: string[] = [];
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    seen.push(path);
    if (path === "/api/v1/auth/me") return route.fulfill({ json: user() });
    if (path === "/api/v1/models") return route.fulfill({ json: [model] });
    if (path === "/api/v1/dashboard") return route.fulfill({ json: dashboard });
    if (path === "/api/v1/monitoring") return route.fulfill({ json: [
      { model_id: "credit-id", model_name: "Credit model", lifecycle_status: "revalidation_due",
        latest: psi, last_run: "2026-11-07T13:00:00Z" },
      { model_id: "synthetic-id", model_name: "Cox", lifecycle_status: "development",
        latest: null, last_run: null },
    ] });
    if (path === "/api/v1/models/credit-id") return route.fulfill({ json: model });
    if (path === "/api/v1/models/credit-id/monitoring") return route.fulfill({ json: [event] });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Dashboard", exact: true }).click();
  const dash = page.getByRole("region", { name: "Live dashboard" });
  await expect(dash).toBeVisible();
  await expect(dash.getByText("PSI events", { exact: true })).toBeVisible();
  await expect(dash.getByText("Revalidation due", { exact: true })).toBeVisible();
  await dash.getByText("How to read these numbers", {exact:true}).click();
  await expect(dash.getByText("Elapsed times are not person-hours or active review effort")).toBeVisible();
  await expect(dash.getByText("$0.0150")).toBeVisible();
  await expect(dash.getByText(/Revalidation due counts models/)).toBeVisible();
  await page.getByRole("button", { name: "Inspect PSI monitoring" }).click();
  const portfolio = page.getByRole("region", { name: "Live monitoring" });
  await expect(portfolio).toBeVisible();
  await expect(portfolio.getByRole("table", { name: "Monitoring portfolio records" })).toBeVisible();
  await expect(portfolio.getByText("No persisted PSI evidence")).toBeVisible();
  await portfolio.getByRole("button", { name: "Inspect latest PSI" }).click();
  await expect(portfolio.getByRole("heading", { name: "Declared test shift" })).toBeVisible();
  await expect(portfolio.getByText("validation-run-1")).toBeVisible();
  await expect(portfolio.getByText(/artificial shift of the frozen validation partition/)).toBeVisible();
  await expect(portfolio.getByText(/not measure accuracy/)).toBeVisible();
  await portfolio.getByRole("button", { name: "Open model monitoring" }).first().click();
  await expect(page.getByRole("tab", { name: "Monitoring" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("region", { name: "Model PSI monitoring" })).toBeVisible();
  await expect(page.getByRole("article", { name: "PSI window Declared test shift" })).toBeVisible();
  expect(seen).toContain("/api/v1/dashboard");
  expect(seen).toContain("/api/v1/monitoring");
  expect(seen).toContain("/api/v1/models/credit-id/monitoring");
  expect(seen.some(path => path.includes("demo"))).toBe(false);
});

test("P2 simulation requires explicit consent, submits one request and reads persisted run and PSI", async ({ page }) => {
  const sent: { path: string; payload: unknown }[] = [];
  let completed = false;
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/auth/me") return route.fulfill({ json: user("validator") });
    if (path === "/api/v1/models") return route.fulfill({ json: [model] });
    if (path === "/api/v1/models/credit-id") return route.fulfill({ json: model });
    if (path === "/api/v1/models/credit-id/monitoring" && route.request().method() === "GET")
      return route.fulfill({ json: completed ? [event] : [] });
    if (path === "/api/v1/models/credit-id/monitoring/simulate") {
      sent.push({ path, payload: route.request().postDataJSON() });
      return route.fulfill({ status: 202, json: monitoringRun });
    }
    if (path === "/api/v1/runs/monitor-queued") {
      completed = true;
      return route.fulfill({ json: {
        ...monitoringRun, status: "completed", stage: "completed",
        started_at: "2026-11-07T13:20:00Z", finished_at: "2026-11-07T13:21:00Z",
      } });
    }
    return route.fulfill({ json: [] });
  });
  await page.goto("/?model=credit-id");
  await page.getByRole("tab", { name: "Monitoring" }).click();
  const region = page.getByRole("region", { name: "Model PSI monitoring" });
  await expect(region.getByText("No completed PSI windows recorded for this model.")).toBeVisible();
  const submit = region.getByRole("button", { name: "Submit monitoring job" });
  await expect(submit).toBeDisabled();
  await region.getByLabel("Monitoring window label").fill("Declared test shift");
  await region.getByLabel("Simulation feature").selectOption("LIMIT_BAL");
  await region.getByLabel("Simulation factor").fill("1.5");
  await expect(submit).toBeDisabled();
  await region.getByRole("checkbox", { name: /I confirm this is a declared simulation/ }).check();
  await submit.click();
  await expect(region.getByText(/Backend accepted monitoring run monitor-queued/)).toBeVisible();
  expect(sent).toHaveLength(1);
  expect(sent[0].payload).toEqual({
    feature: "LIMIT_BAL", factor: 1.5, window_label: "Declared test shift",
  });
  await region.getByRole("button", { name: "Refresh run status (GET)" }).click();
  await expect(region.getByText(/Persisted monitoring run monitor-queued is completed/)).toBeVisible();
  await expect(region.getByRole("article", { name: "PSI window Declared test shift" })).toBeVisible();
  expect(sent).toHaveLength(1);
});

test("P2 CSV sends actual multipart file with no aggregate PSI, and no automatic retry", async ({ page }) => {
  const posts: { url: string; data: string; headers: Record<string, string> }[] = [];
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url()), path = url.pathname;
    if (path === "/api/v1/auth/me") return route.fulfill({ json: user("developer") });
    if (path === "/api/v1/models") return route.fulfill({ json: [model] });
    if (path === "/api/v1/models/credit-id") return route.fulfill({ json: model });
    if (path === "/api/v1/models/credit-id/monitoring") return route.fulfill({ json: [] });
    if (path === "/api/v1/models/credit-id/monitoring/batches") {
      posts.push({ url: path, data: route.request().postData() ?? "",
        headers: route.request().headers() });
      return route.fulfill({ status: 202, json: monitoringRun });
    }
    return route.fulfill({ json: [] });
  });
  await page.goto("/?model=credit-id");
  await page.getByRole("tab", { name: "Monitoring" }).click();
  const area = page.getByRole("region", { name: "Model PSI monitoring" });
  await area.getByLabel("Monitoring input mode").selectOption("upload");
  await area.getByLabel("Monitoring window label").fill("October production batch");
  await area.getByLabel("Monitoring CSV batch").setInputFiles({
    name: "monitor.csv", mimeType: "text/csv",
    buffer: Buffer.from("row_id,LIMIT_BAL,AGE\ntest-001,4200,35\n"),
  });
  await area.getByRole("checkbox", { name: /I confirm this is a real CSV batch/ }).check();
  await area.getByRole("button", { name: "Submit monitoring job" }).click();
  await expect(area.getByText(/Backend accepted monitoring run monitor-queued/)).toBeVisible();
  expect(posts).toHaveLength(1);
  expect(posts[0].url).toBe("/api/v1/models/credit-id/monitoring/batches");
  expect(posts[0].headers["content-type"]).toContain("multipart/form-data");
  expect(posts[0].data).toContain("monitor.csv");
  expect(posts[0].data).toContain("window_label");
  expect(posts[0].data).not.toContain('"psi"');
});

test("P2 inapplicable Cox and unauthorised roles cannot dispatch monitoring", async ({ page }) => {
  const sent: string[] = [];
  const cox = { ...model, id: "cox", name: "Cox synthetic",
    dataset_id: null, inventory: { synthetic_demo: true,
      manifest: { task: "survival_analysis" },
      applicability: { monitor: "NOT_APPLICABLE" } } };
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "POST") sent.push(path);
    if (path === "/api/v1/auth/me")
      return route.fulfill({ json: user("approver") });
    if (path === "/api/v1/models") return route.fulfill({ json: [cox] });
    if (path === "/api/v1/models/cox") return route.fulfill({ json: cox });
    if (path === "/api/v1/models/cox/monitoring") return route.fulfill({ json: [] });
    return route.fulfill({ json: [] });
  });
  await page.goto("/?model=cox");
  await page.getByRole("tab", { name: "Monitoring" }).click();
  await expect(page.getByText(/Synthetic survival models are NOT_APPLICABLE/)).toBeVisible();
  await expect(page.getByText(/Read-only role/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Submit monitoring job" })).toHaveCount(0);
  expect(sent).toHaveLength(0);
});

test("P2 invalid PSI payload reports schema error, not a fabricated green pass", async ({ page }) => {
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/auth/me") return route.fulfill({ json: user() });
    if (path === "/api/v1/models") return route.fulfill({ json: [model] });
    if (path === "/api/v1/models/credit-id") return route.fulfill({ json: model });
    if (path === "/api/v1/models/credit-id/monitoring")
      return route.fulfill({ json: [{ ...event, payload: { ...psi, baseline_sha256: "incorrect" } }] });
    return route.fulfill({ json: [] });
  });
  await page.goto("/?model=credit-id");
  await page.getByRole("tab", { name: "Monitoring" }).click();
  const area = page.getByRole("region", { name: "Model PSI monitoring" });
  await expect(area.getByRole("alert")).toContainText("Invalid monitoring API response");
  await expect(area.getByRole("article", { name: /PSI window/ })).toHaveCount(0);
});


test("P2 unapproved uploaded binary model cannot submit monitoring before Gate 3 sign-off", async ({ page }) => {
  const pending = { ...model, inventory: { ...model.inventory, approval: null } };
  const posts: string[] = [];
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "POST") posts.push(path);
    if (path === "/api/v1/auth/me") return route.fulfill({ json: user("validator") });
    if (path === "/api/v1/models") return route.fulfill({ json: [pending] });
    if (path === "/api/v1/models/credit-id") return route.fulfill({ json: pending });
    if (path === "/api/v1/models/credit-id/monitoring") return route.fulfill({ json: [] });
    return route.fulfill({ json: [] });
  });
  await page.goto("/?model=credit-id");
  await page.getByRole("tab", { name: "Monitoring" }).click();
  const panel = page.getByRole("region", { name: "Model PSI monitoring" });
  await expect(panel.getByText(/accepted Gate 3 validation sign-off/)).toBeVisible();
  await expect(panel.getByRole("button", { name: "Submit monitoring job" })).toHaveCount(0);
  expect(posts).toEqual([]);
});

test("F3 monitoring observes completion and refreshes PSI without resubmitting", async ({ page }) => {
  await page.clock.install();
  const sent: { path: string; payload: unknown }[] = [];
  let completed = false;
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/auth/me") return route.fulfill({ json: user("validator") });
    if (path === "/api/v1/models") return route.fulfill({ json: [model] });
    if (path === "/api/v1/models/credit-id") return route.fulfill({ json: model });
    if (path === "/api/v1/models/credit-id/monitoring" && route.request().method() === "GET")
      return route.fulfill({ json: completed ? [event] : [] });
    if (path === "/api/v1/models/credit-id/monitoring/simulate") {
      sent.push({ path, payload: route.request().postDataJSON() });
      return route.fulfill({ status: 202, json: monitoringRun });
    }
    if (path === "/api/v1/runs/monitor-queued") {
      completed = true;
      return route.fulfill({ json: {
        ...monitoringRun, status: "completed", stage: "completed",
        started_at: "2026-11-07T13:20:00Z", finished_at: "2026-11-07T13:21:00Z",
      } });
    }
    return route.fulfill({ json: [] });
  });
  await page.goto("/?model=credit-id");
  await page.getByRole("tab", { name: "Monitoring" }).click();
  const region = page.getByRole("region", { name: "Model PSI monitoring" });
  await expect(region.getByText("No completed PSI windows recorded for this model.")).toBeVisible();
  const submit = region.getByRole("button", { name: "Submit monitoring job" });
  await expect(submit).toBeDisabled();
  await region.getByLabel("Monitoring window label").fill("Declared test shift");
  await region.getByLabel("Simulation feature").selectOption("LIMIT_BAL");
  await region.getByLabel("Simulation factor").fill("1.5");
  await expect(submit).toBeDisabled();
  await region.getByRole("checkbox", { name: /I confirm this is a declared simulation/ }).check();
  await submit.click();
  await expect(region.getByText(/Backend accepted monitoring run monitor-queued/)).toBeVisible();
  expect(sent).toHaveLength(1);
  expect(sent[0].payload).toEqual({
    feature: "LIMIT_BAL", factor: 1.5, window_label: "Declared test shift",
  });
  await region.getByLabel("Monitoring window label").fill("Keep next window draft");
  await page.clock.fastForward(4000);
  await expect(region.getByText(/Persisted monitoring run monitor-queued is completed/)).toBeVisible();
  await expect(region.getByRole("article", { name: "PSI window Declared test shift" })).toBeVisible();
  await expect(region.getByLabel("Monitoring window label")).toHaveValue("Keep next window draft");
  expect(sent).toHaveLength(1);
});
