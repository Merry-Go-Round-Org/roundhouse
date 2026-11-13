import { test, expect, type Page } from "@playwright/test";
const model = {
  id: "m1",
  name: "Credit",
  version: "1",
  language: "R",
  dataset_id: "uci_credit",
  purpose: "Demo",
  owner: null,
  lifecycle_status: "development",
  created_at: "2026-11-05T12:00:00Z",
};
const run = {
  id: "r1",
  model_id: "m1",
  model_version: "1",
  dataset_id: "uci_credit",
  operation: "validate",
  status: "running",
  created_at: "2026-11-05T12:00:00Z",
  started_at: null,
  finished_at: null,
  stage: "Checking",
  error: null,
};
async function open(page: Page) {
  await page.goto("/?model=m1");
  await page.getByRole("tab", { name: "History", exact: true }).click();
}
test("history paginates and inspects current run state without writes", async ({
  page,
}) => {
  const requests: string[] = [];
  let status = "awaiting_review";
  await page.route("**/api/v1/**", (route) => {
    if (new URL(route.request().url()).pathname === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "mock-user", username: "fixture-user", role: "admin", active: true } });
    const request = route.request(),
      url = new URL(request.url());
    requests.push(request.method());
    if (url.pathname.endsWith("/models/m1"))
      return route.fulfill({ json: model });
    if (url.pathname.endsWith("/models"))
      return route.fulfill({ json: [model] });
    if (url.pathname.endsWith("/runs"))
      return route.fulfill({
        json:
          url.searchParams.get("offset") === "0"
            ? Array.from({ length: 20 }, (_, i) => ({
                ...run,
                id: `r${i + 1}`,
              }))
            : [],
      });
    if (url.pathname.endsWith("/runs/r1"))
      return route.fulfill({ json: { ...run, status } });
    return route.fulfill({ json: [] });
  });
  await open(page);
  await page.getByRole("button", { name: "Inspect r1", exact: true }).click();
  const detail = page.getByRole("region", { name: "Selected run" });
  await expect(detail).toContainText("awaiting_review");
  await expect(detail).toContainText("Not supplied");
  status = "completed";
  await page.getByRole("button", { name: "Refresh run details" }).click();
  await expect(detail).toContainText("completed");
  await page.getByRole("button", { name: "Next runs" }).click();
  await expect(
    page.getByText(
      "No runs returned on this page. Use Previous runs to return.",
    ),
  ).toBeVisible();
  await expect(detail).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Next runs" })).toBeDisabled();
  await page.getByRole("button", { name: "Previous runs" }).click();
  await expect(page.getByRole("table", { name: "Saved runs" })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(requests.every((method) => method === "GET")).toBe(true);
});
test("history refresh clears stale results and supports error recovery and empty state", async ({
  page,
}) => {
  let mode = "rows";
  await page.route("**/api/v1/**", (route) => {
    if (new URL(route.request().url()).pathname === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "mock-user", username: "fixture-user", role: "admin", active: true } });
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/models/m1"))
      return route.fulfill({ json: model });
    if (url.pathname.endsWith("/runs"))
      return route.fulfill(
        mode === "error"
          ? { status: 503, json: { detail: "History unavailable" } }
          : { json: mode === "rows" ? [run] : [] },
      );
    return route.fulfill({ json: [] });
  });
  await open(page);
  await expect(page.getByRole("table", { name: "Saved runs" })).toBeVisible();
  mode = "error";
  await page.getByRole("button", { name: "Refresh history" }).click();
  await expect(page.getByRole("alert")).toContainText("History unavailable");
  await expect(page.getByRole("table", { name: "Saved runs" })).toHaveCount(0);
  await expect(page.getByText("No runs returned for this model.")).toHaveCount(
    0,
  );
  mode = "empty";
  await page.getByRole("button", { name: "Refresh history" }).click();
  await expect(
    page.getByText("No runs returned for this model."),
  ).toBeVisible();
});
test("history rejects detail belonging to another model", async ({ page }) => {
  await page.route("**/api/v1/**", (route) => {
    if (new URL(route.request().url()).pathname === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "mock-user", username: "fixture-user", role: "admin", active: true } });
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/models/m1"))
      return route.fulfill({ json: model });
    if (url.pathname.endsWith("/runs")) return route.fulfill({ json: [run] });
    if (url.pathname.endsWith("/runs/r1"))
      return route.fulfill({
        json: { ...run, model_id: "other", stage: "Private stage" },
      });
    return route.fulfill({ json: [] });
  });
  await open(page);
  await page.getByRole("button", { name: "Inspect r1", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("another model");
  await expect(page.getByText("Private stage")).toHaveCount(0);
});

test("history distinguishes reviewer revisions from execution failures after refresh and reload", async ({ page }) => {
  const writes: string[] = [];
  const revision = { ...run, operation: "document", status: "failed", stage: "changes_requested", error: null };
  const failure = { ...run, id: "r2", operation: "document", status: "failed", stage: "execution", error: "Worker execution failed" };
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() !== "GET") writes.push(path);
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { id:"operator", role:"admin", active:true } });
    if (path.endsWith("/models/m1")) return route.fulfill({ json:model });
    if (path.endsWith("/models")) return route.fulfill({ json:[model] });
    if (path.endsWith("/runs")) return route.fulfill({ json:[revision, failure] });
    if (path.endsWith("/runs/r1")) return route.fulfill({ json:revision });
    if (path.endsWith("/runs/r2")) return route.fulfill({ json:failure });
    return route.fulfill({ json:[] });
  });
  await open(page);
  const table = page.getByRole("table", { name:"Saved runs" });
  const revisionRow = table.getByRole("row").filter({ has: page.getByRole("button", { name:"Inspect r1", exact:true }) });
  await expect(revisionRow).toContainText("Changes requested");
  await expect(revisionRow).not.toContainText("failed");
  await page.getByRole("button", { name:"Inspect r1", exact:true }).click();
  const detail = page.getByRole("region", { name:"Selected run", exact:true });
  await expect(detail).toContainText("Changes requested");
  await expect(detail).toContainText("Inspect subsequent runs");
  await expect(detail).not.toContainText("failed");
  await page.getByRole("button", { name:"Refresh run details" }).click();
  await expect(detail).toContainText("Changes requested");
  await page.getByRole("button", { name:"Inspect r2", exact:true }).click();
  await expect(detail).toContainText("Worker execution failed");
  await expect(detail).not.toContainText("Changes requested");
  await page.reload();
  await page.getByRole("button", { name:"Inspect r1", exact:true }).click();
  await expect(detail).toContainText("Changes requested");
  expect(writes).toEqual([]);
});
