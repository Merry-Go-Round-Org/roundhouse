import { test, expect } from "@playwright/test";
const credit = {
  id: "credit-id",
  name: "Credit R",
  version: "1.0.0",
  language: "R",
  dataset_id: "uci_credit",
  purpose: "Default prediction",
  owner: null,
  lifecycle_status: "development",
  created_at: "2026-11-05T12:00:00Z",
};
const fraud = {
  ...credit,
  id: "fraud-id",
  name: "Fraud Python",
  language: "Python",
  dataset_id: "ulb_fraud",
  owner: "Team",
  lifecycle_status: "active",
};
test("confirmed Gate 0 provenance supplies missing owner consistently and exposes conflicting current owner", async ({ page }) => {
  let owner: string | null = null;
  const saved = { ...credit, inventory: { manifest: { ai_native: true, owner: "Risk team", intended_use: "Assess risk", provenance: {
    owner: { value: "Risk team", source: "proposed", confirmed: true, confirmed_by: "reviewer" },
    intended_use: { value: "Assess risk", source: "document", confirmed: true, confirmed_by: "reviewer", evidence: "original/notes.md" },
  } }, provenance: { intended_use: { value: "Assess risk", source: "upload" } } } };
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { id: "user", role: "admin" } });
    if (path.endsWith("/models/credit-id")) return route.fulfill({ json: { ...saved, owner } });
    if (path.endsWith("/models")) return route.fulfill({ json: [{ ...saved, owner }] });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await page.getByLabel("Search this inventory page").fill("Risk team");
  await expect(page.getByRole("button", { name: "Credit R", exact: true })).toBeVisible();
  await page.getByLabel("Owner not supplied", { exact: true }).check();
  await expect(page.getByRole("button", { name: "Credit R", exact: true })).toHaveCount(0);
  await page.getByLabel("Owner not supplied", { exact: true }).uncheck();
  await page.getByRole("button", { name: "Credit R", exact: true }).click();
  const group = page.getByRole("region", { name: "Purpose and ownership", exact: true });
  const ownerField = group.locator(".record-fields > div").filter({ has: page.getByText("Owner", { exact: true }) });
  await expect(ownerField).toContainText("Risk team");
  await ownerField.getByText("Source", { exact: true }).click();
  await expect(ownerField).toContainText("Top-level model owner is not supplied");
  const use = group.locator(".record-fields > div").filter({ has: page.getByText("Intended use", { exact: true }) });
  await use.getByText("Source", { exact: true }).click();
  await expect(use).toContainText("Source: document · Confirmed at Gate 0 · original/notes.md");
  owner = "Updated owner";
  await page.reload();
  await expect(group).toContainText("Updated owner");
  await expect(group).toContainText("Saved Gate 0 owner: Risk team");
});
const datasets = [
  {
    id: "uci_credit",
    name: "UCI credit",
    filename: "credit.xls",
    target_description: "Next month default",
    limitations: ["Historical population"],
  },
  {
    id: "ulb_fraud",
    name: "ULB fraud",
    filename: "fraud.csv",
    target_description: "Fraud",
    limitations: ["No protected groups"],
  },
];
test("inventory filters combine, clear and search IDs without claiming server-wide filtering", async ({
  page,
}) => {
  await page.route("**/api/v1/**", (route) =>
    new URL(route.request().url()).pathname === "/api/v1/auth/me"
      ? route.fulfill({ json: { id: "mock-user", username: "fixture-user", role: "admin", active: true } })
      : route.fulfill({ json: [credit, fraud] }),
  );
  await page.goto("/");
  await page.getByLabel("Language", { exact: true }).selectOption("R");
  await expect(page.getByText("1 of 2 fetched models match.")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Credit R", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Fraud Python", exact: true }),
  ).toHaveCount(0);
  await page.getByLabel("Lifecycle", { exact: true }).selectOption("active");
  await expect(page.getByText("0 of 2 fetched models match.")).toBeVisible();
  await page.getByRole("button", { name: "Clear filters" }).click();
  await page.getByLabel("Owner not supplied", { exact: true }).check();
  await expect(page.getByText("1 of 2 fetched models match.")).toBeVisible();
  await page.getByRole("button", { name: "Clear filters" }).click();
  await page.getByLabel("Search this inventory page").fill("fraud-id");
  await expect(
    page.getByRole("button", { name: "Fraud Python", exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/not portfolio totals/)).toBeVisible();
});
test("model metadata opens dataset description and returns to filtered inventory", async ({
  page,
}) => {
  const paths: string[] = [];
  await page.route("**/api/v1/**", (route) => {
    if (new URL(route.request().url()).pathname === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "mock-user", username: "fixture-user", role: "admin", active: true } });
    const path = new URL(route.request().url()).pathname;
    paths.push(path);
    return route.fulfill({
      json: path.endsWith("/datasets")
        ? datasets
        : path.endsWith("/models/credit-id")
          ? credit
          : [credit, fraud],
    });
  });
  await page.goto("/?model=credit-id");
  await expect(
    page.getByRole("region", { name: "Purpose and ownership", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(
      /Model details and current review status/,
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "View dataset description" }).click();
  await expect(
    page.getByRole("heading", { name: "UCI credit", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Version and partitions", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Historical population", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(/not a verified uploaded dataset version/),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page
    .getByRole("button", { name: "Find models using this dataset" })
    .click();
  await expect(page.getByLabel("Dataset", { exact: true })).toHaveValue(
    "uci_credit",
  );
  await expect(
    page.getByRole("button", { name: "Credit R", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Fraud Python", exact: true }),
  ).toHaveCount(0);
  expect(new URL(page.url()).searchParams.has("model")).toBe(false);
  expect(paths.some((p) => p.startsWith("/api/v1/datasets/"))).toBe(false);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
test("dataset refresh error hides stale details and recovers on explicit refresh", async ({
  page,
}) => {
  let fail = false;
  await page.route("**/api/v1/**", (route) => {
    if (new URL(route.request().url()).pathname === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "mock-user", username: "fixture-user", role: "admin", active: true } });
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/datasets"))
      return fail
        ? route.fulfill({
            status: 503,
            json: { detail: "Catalogue temporarily unavailable" },
          })
        : route.fulfill({ json: datasets });
    return route.fulfill({ json: [credit] });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Datasets", exact: true }).click();
  await page
    .getByRole("button", { name: "View UCI credit", exact: true })
    .click();
  fail = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Catalogue temporarily unavailable",
  );
  await expect(
    page.getByRole("region", { name: "Dataset description", exact: true }),
  ).toHaveCount(0);
  fail = false;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Dataset description", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Back to datasets", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Dataset catalogue", exact: true }),
  ).toBeVisible();
});


test("Demo and Live share the inventory component but not model sources or actions", async ({ page }) => {
  const requested: string[] = [];
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    requested.push(path);
    if (path === "/api/v1/auth/me")
      return route.fulfill({
        json: { id: "shared-ui-user", username: "shared-ui-user", role: "developer", active: true },
      });
    if (path === "/api/v1/models")
      return route.fulfill({ json: [credit] });
    if (path === "/api/v1/models/credit-id")
      return route.fulfill({ json: credit });
    return route.fulfill({ json: [] });
  });

  await page.goto("/");
  const liveTable = page.getByRole("table", { name: "Model inventory" });
  await expect(liveTable).toBeVisible();
  await expect(liveTable.getByRole("columnheader", { name: "Lifecycle (not approval)" })).toBeVisible();
  await expect(liveTable.getByRole("button", { name: "Credit R", exact: true })).toBeVisible();
  await expect(liveTable.getByText("CR-001")).toHaveCount(0);
  await liveTable.getByRole("button", { name: "Credit R", exact: true }).click();
  await expect(page.getByRole("region", { name: "Purpose and ownership" })).toBeVisible();
  expect(requested).toContain("/api/v1/models/credit-id");

  const beforeDemo = requested.length;
  await page.goto("/demo");
  const demoTable = page.getByRole("table", { name: "Model inventory" });
  await expect(demoTable).toBeVisible();
  await expect(demoTable.getByRole("columnheader", { name: "Workflow stage (demo)" })).toBeVisible();
  await expect(demoTable.getByRole("button", { name: "Credit default scorecard", exact: true })).toBeVisible();
  await expect(demoTable.getByText("real-credit-2026")).toHaveCount(0);
  await expect(demoTable.getByText("credit-id")).toHaveCount(0);
  await demoTable.getByRole("button", { name: "Credit default scorecard", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Credit default scorecard" })).toBeVisible();
  expect(requested.length).toBe(beforeDemo);
});


test("Demo and Live share detail tabs and metadata card but preserve evidence origins", async ({ page }) => {
  const paths: string[] = [];
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    paths.push(path);
    if (path === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "reader", username: "reader", role: "validator", active: true } });
    if (path === "/api/v1/models/credit-id")
      return route.fulfill({ json: credit });
    if (path === "/api/v1/models")
      return route.fulfill({ json: [credit] });
    return route.fulfill({ json: [] });
  });

  await page.goto("/?model=credit-id");
  const liveTabs = page.getByRole("tablist", { name: "Model detail" });
  await expect(liveTabs.getByRole("tab")).toHaveCount(6);
  await expect(page.getByRole("region", { name: "Identity" })).toBeVisible();
  await expect(page.getByText(/original upload provenance is unavailable/i)).toBeVisible();
  await expect(page.getByText("Lifecycle (not approval)")).toBeVisible();
  await expect(page.getByText("Risk tier / rationale", { exact: true })).toBeVisible();
  await expect(page.getByText("Not supplied by API", { exact: true }).first()).toBeVisible();
  await liveTabs.getByRole("tab", { name: "Overview" }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(liveTabs.getByRole("tab", { name: "Documentation" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tabpanel")).toHaveAttribute(
    "aria-labelledby",
    await liveTabs.getByRole("tab", { name: "Documentation" }).getAttribute("id") ?? "",
  );
  await page.keyboard.press("End");
  await expect(liveTabs.getByRole("tab", { name: "History" })).toHaveAttribute("aria-selected", "true");
  await liveTabs.getByRole("tab", { name: "Rebuild & parity" }).click();
  await expect(page.getByRole("heading", { name: "Official parity", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Run parity" })).toBeDisabled();

  const backendRequests = paths.length;
  await page.goto("/demo");
  const demoTable = page.getByRole("table", { name: "Model inventory" });
  await demoTable.getByRole("button", { name: "Credit default scorecard", exact: true }).click();
  const demoTabs = page.getByRole("tablist", { name: "Model sections" });
  await expect(demoTabs.getByRole("tab")).toHaveCount(4);
  await expect(page.getByRole("region", { name: "Model record" })).toBeVisible();
  await expect(page.getByText("Illustrative record only; these fields are not Backend evidence.")).toBeVisible();
  await expect(page.getByText("High · illustrative")).toBeVisible();
  await demoTabs.getByRole("tab", { name: "Overview" }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(demoTabs.getByRole("tab", { name: "Documentation" })).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowRight");
  await expect(demoTabs.getByRole("tab", { name: "Rebuild & parity" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByText("Demo fixture only")).toBeVisible();
  expect(paths.length).toBe(backendRequests);
});


test("uploaded dataset view shows Backend hashes, partitions and linked-model count", async ({ page }) => {
  const hash = "a".repeat(64);
  const uploaded = {
    ...datasets[0],
    version_id: "dataset-version-01",
    version: "2026-11",
    sha256: hash,
    split_sha256: hash,
    verified_upload: true,
    statistics: {
      row_count: 10,
      positive_rate: 0.3,
      partition_counts: { train: 5, validation: 4, test: 1 },
      positive_rates: { train: 0.4, validation: 0, test: 1 },
      partition_sha256: { train: hash, validation: hash, test: hash },
    },
    columns: ["ID", "SEX", "default"],
    descriptor: { source_url: "https://archive.ics.uci.edu", license: "CC BY" },
    model_count: 2,
    created_at: "2026-11-07T10:00:00Z",
  };
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "user", username: "user", role: "admin", active: true } });
    if (path === "/api/v1/datasets")
      return route.fulfill({ json: [uploaded] });
    if (path === "/api/v1/models")
      return route.fulfill({ json: [credit] });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Datasets", exact: true }).click();
  await expect(page.getByRole("region", { name: "Dataset catalogue" })).toBeVisible();
  await expect(page.getByText("2026-11", { exact: true })).toBeVisible();
  await expect(page.getByText("30.00%", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "View UCI credit", exact: true }).click();
  await expect(page.getByText("Verified uploaded dataset version.", { exact: false })).toBeVisible();
  await expect(page.getByText("Train 5 · Validation 4 · Test 1")).toBeVisible();
  await expect(page.getByText("2 linked model(s) reported by the Backend.", { exact: false })).toBeVisible();
  await expect(page.getByText("dataset-version-01")).toBeVisible();
});

test("synthetic Cox renders no external dataset and prevents inapplicable job submissions", async ({ page }) => {
  const sent: string[] = [];
  const cox = {
    ...credit,
    id: "cox-synthetic",
    name: "Synthetic Cox PH",
    dataset_id: null,
    language: "Python",
    inventory: {
      lifecycle_stage: "documented",
      synthetic_demo: true,
      manifest: { domain: "credit", task: "survival_analysis", algorithm: "cox_ph" },
      applicability: {
        document: "applicable",
        rebuild: "NOT_APPLICABLE",
        validate: "NOT_APPLICABLE",
        monitor: "NOT_APPLICABLE",
      },
    },
  };
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "POST") sent.push(path);
    if (path === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "user", username: "user", role: "admin", active: true } });
    if (path === "/api/v1/models")
      return route.fulfill({ json: [cox] });
    if (path === "/api/v1/models/cox-synthetic")
      return route.fulfill({ json: cox });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  const table = page.getByRole("table", { name: "Model inventory" });
  await expect(table.getByText("Synthetic / no dataset")).toBeVisible();
  await expect(table.getByText("Survival")).toBeVisible();
  await table.getByRole("button", { name: "Synthetic Cox PH", exact: true }).click();
  await expect(page.getByText("Synthetic survival demonstration.")).toBeVisible();
  await expect(page.getByRole("button", { name: "View dataset description" })).toBeDisabled();
  await page.getByRole("tab", { name: "Validation" }).click();
  await expect(page.getByRole("button", { name: "Run validation" })).toBeDisabled();
  await expect(page.getByText(/NOT_APPLICABLE: this synthetic survival model/)).toBeVisible();
  await page.getByRole("tab", { name: "Rebuild & parity" }).click();
  await expect(page.getByRole("button", { name: "Rebuild in Python" })).toBeDisabled();
  expect(sent).not.toContain("/api/v1/models/cox-synthetic/runs");
});

test("review queue respects real Backend blockers without making a decision POST", async ({ page }) => {
  const sent: string[] = [];
  const review = {
    id: "review-gate2",
    model_id: credit.id,
    model_name: credit.name,
    model_version: credit.version,
    run_id: "run-123",
    gate: 2,
    status: "pending",
    artifact_id: "bundle-version",
    artifact_version: "5",
    created_at: "2026-11-07T10:00:00Z",
    resolution: null,
    approval_available: false,
    blockers: ["official_parity_not_passed"],
    candidate_model_id: "candidate-1",
    requested_by: "developer-01",
  };
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "POST") sent.push(path);
    if (path === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "validator-01", username: "validator", role: "validator", active: true } });
    if (path === "/api/v1/models")
      return route.fulfill({ json: [credit] });
    if (path === "/api/v1/reviews")
      return route.fulfill({ json: [review] });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Review queue", exact: true }).click();
  await page.getByRole("button", { name: "Inspect review-gate2" }).click();
  await expect(page.getByText(/Backend blocking checks: official_parity_not_passed/)).toBeVisible();
  await expect(page.getByText(/Gate 2 acceptance needs a passing official parity run/)).toBeVisible();
  await expect(page.getByRole("option", { name: "Accepted", exact: true })).toHaveAttribute("disabled", "");
  await expect(page.getByRole("button", { name: "Submit decision" })).toBeDisabled();
  expect(sent).not.toContain("/api/v1/reviews/review-gate2/decision");
});

test("a reviewer runs official parity at Gate 2 and acceptance opens once it passes", async ({ page }) => {
  const sent: string[] = [];
  const candidate = { ...credit, id: "candidate-1", version: "1.0.0-r1", language: "Python" };
  const parityRun = (id: string, initiator: string, passed: boolean, at: string) => ({
    id,
    candidate_model_id: candidate.id,
    candidate_version: candidate.version,
    candidate_bundle_sha256: "a".repeat(64),
    parent_model_id: credit.id,
    parent_version: credit.version,
    rebuild_run_id: "run-123",
    initiator,
    actor_id: initiator === "agent" ? "developer-01" : "validator-01",
    status: "completed",
    passed,
    error: null,
    created_at: at,
    result: {
      method: "parity-v1",
      tolerance_absolute: 1e-8,
      n_rows: 100,
      n_above_tolerance: passed ? 0 : 3,
      max_absolute_difference: passed ? 0 : 0.0021,
      worst_rows: passed ? [] : [{ row_id: "r-7", absolute_difference: 0.0021 }],
      limitation: "Development fixture rows only; not validation or final-test evidence.",
    },
  });
  // A checkpoint saved before official parity existed: no run on this candidate yet.
  let runs: ReturnType<typeof parityRun>[] = [];
  let review = {
    id: "review-gate2",
    model_id: credit.id,
    model_name: credit.name,
    model_version: credit.version,
    run_id: "run-123",
    gate: 2,
    status: "pending",
    artifact_id: "bundle-version",
    artifact_version: "5",
    created_at: "2026-11-07T10:00:00Z",
    resolution: null,
    approval_available: false,
    blockers: ["official_parity_not_passed"],
    candidate_model_id: candidate.id,
    requested_by: "developer-01",
  };
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (method === "POST") sent.push(path);
    if (path === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "validator-01", username: "validator", role: "validator", active: true } });
    if (path === "/api/v1/models")
      return route.fulfill({ json: [credit] });
    if (path === "/api/v1/models/candidate-1")
      return route.fulfill({ json: candidate });
    if (path === "/api/v1/models/candidate-1/parity-runs")
      return route.fulfill({ json: runs });
    if (path === "/api/v1/models/candidate-1/versions/1.0.0-r1/parity" && method === "POST") {
      const human = parityRun("parity-human", "human", true, "2026-11-07T10:09:00Z");
      runs = [human, ...runs];
      review = { ...review, blockers: [], approval_available: true };
      return route.fulfill({ status: 201, json: human });
    }
    if (path === "/api/v1/reviews/review-gate2")
      return route.fulfill({ json: review });
    if (path === "/api/v1/reviews")
      return route.fulfill({ json: [review] });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Review queue", exact: true }).click();
  await page.getByRole("button", { name: "Inspect review-gate2" }).click();
  const parity = page.getByRole("region", { name: "Official parity" });
  await expect(parity.getByText(/No parity runs returned/)).toBeVisible();
  await expect(page.getByRole("option", { name: "Accepted", exact: true })).toHaveAttribute("disabled", "");
  await parity.getByRole("button", { name: "Run parity" }).click();
  await expect(parity.getByText(/Newest on this page: PASS/)).toBeVisible();
  await expect(parity.getByRole("article")).toHaveCount(1);
  await expect(page.getByText(/Gate 2 acceptance needs a passing official parity run/)).toHaveCount(0);
  await expect(page.getByRole("option", { name: "Accepted", exact: true })).not.toHaveAttribute("disabled", "");
  expect(sent).toContain("/api/v1/models/candidate-1/versions/1.0.0-r1/parity");
  expect(sent).not.toContain("/api/v1/reviews/review-gate2/decision");
});

test("model switcher and Inventory navigation select the intended model",async({page})=>{
 const models=["one","two"].map(id=>({id,name:`Model ${id}`,version:"1",language:"R",dataset_id:"uci_credit",purpose:"Example",owner:null,lifecycle_status:"development",created_at:"2026-11-08T12:00:00Z"}));
 await page.route("**/api/v1/**",route=>{
  const path=new URL(route.request().url()).pathname;
  if(path.endsWith("/auth/me"))return route.fulfill({json:{id:"operator",role:"developer"}});
  if(path.endsWith("/models"))return route.fulfill({json:models});
  const found=models.find(m=>path.endsWith(`/models/${m.id}`));
  return route.fulfill({json:found??[]});
 });
 await page.goto("/");await page.getByRole("button",{name:"Model one",exact:true}).click();
 await page.getByLabel("Switch model",{exact:true}).selectOption("two");
 await expect(page.getByRole("heading",{level:1,name:"Model two"})).toBeVisible();
 await page.getByRole("button",{name:"Inventory",exact:true}).click();
 await expect(page.getByRole("heading",{level:1,name:"Inventory",exact:true})).toBeVisible();
 await expect(page.getByRole("button",{name:"Model one",exact:true})).toBeVisible();
 await expect(page.getByRole("button",{name:"Model two",exact:true})).toBeVisible();
});
