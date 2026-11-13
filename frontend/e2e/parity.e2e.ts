import { test, expect, type Page } from "@playwright/test";
const model = {
  id: "parent",
  name: "Credit",
  version: "v1",
  language: "R",
  dataset_id: "uci_credit",
  purpose: "Credit",
  owner: null,
  lifecycle_status: "development",
  created_at: "2026-11-07T10:00:00Z",
};
const record = {
  id: "p0",
  candidate_model_id: "c0",
  candidate_version: "v2",
  candidate_bundle_sha256: "bundle-hash",
  parent_model_id: "parent",
  parent_version: "v1",
  rebuild_run_id: "r1",
  initiator: "agent",
  actor_id: "operator",
  status: "completed",
  passed: true,
  error: null,
  created_at: "2026-11-07T10:00:00Z",
  result: {
    method: "parity-v1",
    n_rows: 100,
    n_above_tolerance: 0,
    max_absolute_difference: 0,
    runtime_image: "sandbox@sha256:fixture",
    input_sha256: "input-hash",
    limitation: "Development fixture rows only",
  },
};
async function setup(page: Page) {
  const state = {
    posts: [] as string[],
    reads: [] as number[],
    failRead: false,
    failPost: false,
  };
  await page.route("**/api/v1/**", (route) => {
    const url = new URL(route.request().url()),
      path = url.pathname;
    if (path.endsWith("/auth/me"))
      return route.fulfill({ json: { id: "operator", role: "developer" } });
    if (path.endsWith("/models/parent")) return route.fulfill({ json: model });
    if (path.endsWith("/parity-runs")) {
      const offset = Number(url.searchParams.get("offset"));
      state.reads.push(offset);
      if (state.failRead)
        return route.fulfill({
          status: 503,
          json: { detail: "History unavailable" },
        });
      return route.fulfill({
        json:
          offset === 0
            ? Array.from({ length: 20 }, (_, i) => ({
                ...record,
                id: `p${i}`,
                candidate_model_id: i === 1 ? "c1" : "c0",
              }))
            : [
                {
                  ...record,
                  id: "older",
                  candidate_model_id: "older-candidate",
                },
              ],
      });
    }
    if (path.endsWith("/parity") && route.request().method() === "POST") {
      state.posts.push(path);
      if (state.failPost)
        return route.fulfill({
          status: 503,
          json: { detail: "Connection lost after dispatch" },
        });
      return route.fulfill({
        status: 201,
        json: {
          ...record,
          id: "human",
          initiator: "human",
          candidate_model_id: path.includes("/c1/") ? "c1" : "c0",
        },
      });
    }
    return route.fulfill({ json: [] });
  });
  await page.goto("/?model=parent");
  await page
    .getByRole("tab", { name: "Rebuild & parity", exact: true })
    .click();
  return state;
}
test("parity history paginates, selects a candidate and exposes recorded evidence", async ({
  page,
}) => {
  const state = await setup(page);
  const panel = page.getByRole("region", { name: "Official parity" });
  await expect(panel.getByRole("article")).toHaveCount(20);
  await panel.getByText("Evidence for p0", { exact: true }).click();
  await expect(
    panel.getByRole("article", { name: "Parity run p0", exact: true }),
  ).toContainText("input-hash");
  await expect(
    panel.getByRole("article", { name: "Parity run p0", exact: true }),
  ).toContainText("sandbox@sha256:fixture");
  await panel.getByLabel("Candidate to compare").selectOption("c1:v2");
  await panel.getByRole("button", { name: "Run parity", exact: true }).click();
  await expect
    .poll(() => state.posts)
    .toEqual(["/api/v1/models/c1/versions/v2/parity"]);
  await panel.getByRole("button", { name: "Next parity runs" }).click();
  await expect(panel.getByRole("article")).toHaveCount(1);
  await expect(panel).toContainText("older-candidate");
  expect(state.reads).toContain(20);
  await expect(
    panel.getByRole("button", { name: "Next parity runs" }),
  ).toBeDisabled();
});
test("uncertain parity requires a successful history read and explicit new-execution consent", async ({
  page,
}) => {
  const state = await setup(page);
  state.failPost = true;
  const panel = page.getByRole("region", { name: "Official parity" }),
    run = panel.getByRole("button", { name: "Run parity", exact: true });
  await run.click();
  await expect(panel).toContainText("No automatic retry");
  await expect(run).toBeDisabled();
  state.failRead = true;
  await panel.getByRole("button", { name: "Refresh parity history" }).click();
  await expect(panel.getByRole("article")).toHaveCount(0);
  await expect(panel).toContainText("History unavailable");
  await expect(run).toBeDisabled();
  state.failRead = false;
  await panel.getByRole("button", { name: "Refresh parity history" }).click();
  const consent = panel.getByRole("checkbox");
  await expect(consent).toBeVisible();
  await expect(run).toBeDisabled();
  expect(state.posts).toHaveLength(1);
  await consent.check();
  await run.click();
  await expect.poll(() => state.posts.length).toBe(2);
  await expect(run).toBeDisabled();
});

test("mobile parity evidence is keyboard accessible without page overflow", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await setup(page);
  const panel = page.getByRole("region", { name: "Official parity" });
  await expect(panel.getByRole("article")).toHaveCount(20);
  const choice = panel.getByLabel("Candidate to compare");
  await choice.focus();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(choice).toHaveValue("c1:v2");
  const evidence = panel.getByText("Evidence for p0", { exact: true });
  await evidence.focus();
  await page.keyboard.press("Enter");
  await expect(
    panel.getByText("input-hash", { exact: true }).first(),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  expect(state.posts).toEqual([]);
});

test("a pending parity execution disables duplicate dispatch and history changes", async ({
  page,
}) => {
  const state = await setup(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let posts = 0;
  await page.route("**/api/v1/models/c0/versions/v2/parity", async (route) => {
    posts++;
    await gate;
    await route.fulfill({
      status: 201,
      json: { ...record, id: "new-run", initiator: "human" },
    });
  });
  const panel = page.getByRole("region", { name: "Official parity" });
  try {
    await panel
      .getByRole("button", { name: "Run parity", exact: true })
      .click();
    await expect(
      panel.getByRole("button", { name: "Running parity…", exact: true }),
    ).toBeDisabled();
    await expect(
      panel.getByRole("button", { name: "Refresh parity history" }),
    ).toBeDisabled();
    await expect(panel.getByLabel("Candidate to compare")).toBeDisabled();
    await expect.poll(() => posts).toBe(1);
  } finally {
    release();
  }
  await expect(
    panel.getByRole("button", { name: "Run parity", exact: true }),
  ).toBeEnabled();
  expect(posts).toBe(1);
  expect(state.posts).toHaveLength(0);
});
