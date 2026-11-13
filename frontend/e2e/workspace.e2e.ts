import { test, expect, type Page } from "@playwright/test";
const enabled = process.env.TEST_PROPOSED_UPLOADS === "true";
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
const dataset = {
  id: "uci_credit",
  name: "UCI credit",
  filename: "credit.xls",
  target_description: "Next month default",
  limitations: ["Historical population"],
};
const passed = {
  name: "Manifest",
  status: "passed",
  blocking: true,
  reason: "Valid",
};
async function setup(page: Page, broken = false) {
  let posts = 0;
  await page.route("**/api/v1/**", async (route) => {
    if (new URL(route.request().url()).pathname === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "mock-user", username: "fixture-user", role: "admin", active: true } });
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const reply = (json: unknown, status = 200) =>
      route.fulfill({ status, json });
    if (path === "/models") return reply([model]);
    if (path === "/models/m1") return reply(model);
    if (path === "/datasets") {
      if (route.request().method() === "POST") {
        posts++;
        return reply(
          { id: "uci_credit", status: "completed", checks: [passed] },
          201,
        );
      }
      return reply([dataset]);
    }
    if (path === "/runs") return reply([]);
    if (path === "/models/uploads") {
      posts++;
      return reply(
        {
          id: "u1",
          status: "queued",
          checks: [],
          files: [],
          model_id: null,
          error: null,
        },
        202,
      );
    }
    if (path === "/models/uploads/u1")
      return reply({
        id: "u1",
        status: "completed",
        checks: [{ ...passed, status: broken ? "failed" : "passed" }],
        files: ["manifest.json", "score.R"],
        model_id: "m1",
        error: null,
      });
    return reply({ detail: "Not implemented" }, 501);
  });
  return { posts: () => posts };
}
test("live shell removes register forms and preserves refresh selection", async ({
  page,
}) => {
  await setup(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Credit model", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Inventory record" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Open agent chat", exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Register model", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Register package", exact: true }),
  ).toHaveCount(0);
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Inventory record" }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Rebuild & parity" }).click();
  await expect(
    page.getByRole("button", { name: "Run parity", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("heading", { name: "Official parity", exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/No candidate available on this history page/)).toBeVisible();
});
test("dataset descriptors stay labelled and live alias shows the same shell", async ({
  page,
}) => {
  await setup(page);
  await page.goto("/live");
  await page.getByRole("button", { name: "Datasets", exact: true }).click();
  await expect(page.getByRole("heading", { name: "UCI credit" })).toBeVisible();
  await expect(page.getByText(/Upload datasets and inspect their versions and checks/)).toBeVisible();
  await page
    .getByRole("button", { name: "Upload dataset", exact: true })
    .click();
  await expect(
    page.getByLabel("Source dataset", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Split File (Optional)", { exact: true })).toBeVisible();
  const policyLink = page.getByRole("link", { name: /Read F1 Split Policy/ });
  await expect(policyLink).toHaveAttribute("href", "/f1-split-policy.html");
  await expect(policyLink).toHaveAttribute("target", "_blank");
  const [policyPage] = await Promise.all([
    page.waitForEvent("popup"),
    policyLink.click(),
  ]);
  await expect(policyPage.getByRole("heading", { name: "F1 Split Policy", exact: true })).toBeVisible();
  await expect(policyPage.getByRole("heading", { name: "New datasets" })).toBeVisible();
  await policyPage.close();

});
test("unagreed uploads are disabled by default", async ({ page }) => {
  test.skip(enabled, "Tests default contract gate");
  const state = await setup(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Upload model", exact: true }).click();
  await page
    .getByLabel("Model ZIP bundle")
    .setInputFiles({
      name: "model.zip",
      mimeType: "application/zip",
      buffer: Buffer.from("fixture"),
    });
  await expect(
    page.getByRole("button", { name: "Upload and check model" }),
  ).toBeDisabled();
  expect(state.posts()).toBe(0);
});
test("mobile workspace has no page overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page);
  await page.goto("/?model=m1");
  await expect(
    page.getByRole("heading", { name: "Inventory record" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
test("proposed model upload polls checks before opening inventory", async ({
  page,
}) => {
  test.skip(!enabled, "Needs explicit proposed contract test mode");
  const state = await setup(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Upload model", exact: true }).click();
  await page
    .getByLabel("Model ZIP bundle")
    .setInputFiles({
      name: "model.zip",
      mimeType: "application/zip",
      buffer: Buffer.from("fixture"),
    });
  await page.getByRole("button", { name: "Upload and check model" }).click();
  await expect(
    page.getByRole("heading", { name: "Upload u1: queued" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Open in inventory" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Open in inventory" }).click();
  await expect(
    page.getByRole("heading", { name: "Inventory record" }),
  ).toBeVisible();
  expect(state.posts()).toBe(1);
});
test("blocking failures never offer an inventory link", async ({ page }) => {
  test.skip(!enabled, "Needs explicit proposed contract test mode");
  await setup(page, true);
  await page.goto("/");
  await page.getByRole("button", { name: "Upload model", exact: true }).click();
  await page
    .getByLabel("Model ZIP bundle")
    .setInputFiles({
      name: "model.zip",
      mimeType: "application/zip",
      buffer: Buffer.from("fixture"),
    });
  await page.getByRole("button", { name: "Upload and check model" }).click();
  await expect(page.getByText(/Upload completion lacks/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Open in inventory" }),
  ).toHaveCount(0);
});
test("dataset upload preserves an explicit split and requires completed checks", async ({
  page,
}) => {
  test.skip(!enabled, "Needs explicit proposed contract test mode");
  const state = await setup(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Datasets", exact: true }).click();
  await page
    .getByRole("button", { name: "Upload dataset", exact: true })
    .click();
  for (const [label, name] of [
    ["Source dataset", "source.csv"],
    ["Dataset descriptor (dataset.json)", "dataset.json"],
    ["Split File (Optional)", "split.csv"],
  ]) {
    await page
      .getByLabel(label, { exact: true })
      .setInputFiles({
        name,
        mimeType: "text/plain",
        buffer: Buffer.from("fixture"),
      });
  }
  await page.getByRole("button", { name: "Add dataset", exact: true }).click();
  await expect(
    page.getByText(/Dataset uci_credit uploaded/),
  ).toBeVisible();
  await page.getByText("Upload checks (1)", {exact:true}).click();
  await expect(page.getByText("Manifest: passed · Valid")).toBeVisible();
  await page.getByRole("button",{name:"Refresh datasets",exact:true}).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("button",{name:"Upload dataset",exact:true})).toBeEnabled();
  await page.getByRole("button",{name:"Upload dataset",exact:true}).click();
  await expect(page.getByLabel("Source dataset",{exact:true})).toBeEnabled();
  await expect(page.getByRole("button",{name:"Add dataset",exact:true})).toBeEnabled();

  expect(state.posts()).toBe(1);
});

test("optional split omits multipart field and local validation keeps files editable", async ({ page }) => {
  test.skip(!enabled, "Needs upload-enabled workspace");
  const state = await setup(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Datasets", exact: true }).click();
  await page.getByRole("button", { name: "Upload dataset", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Add dataset", exact: true }).click();
  expect(state.posts()).toBe(0);
  for (const [label, name] of [["Source dataset", "source.csv"], ["Dataset descriptor (dataset.json)", "dataset.json"]]) {
    await dialog.getByLabel(label, { exact: true }).setInputFiles({ name, mimeType: "text/plain", buffer: Buffer.from("fixture") });
  }
  const split = dialog.getByLabel("Split File (Optional)", { exact: true });
  await split.setInputFiles({ name: "empty.csv", mimeType: "text/csv", buffer: Buffer.from("") });
  await dialog.getByRole("button", { name: "Add dataset", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("selected split file is empty");
  await expect(split).toBeEnabled();
  expect(state.posts()).toBe(0);
  await split.setInputFiles([]);
  const request = page.waitForRequest(r => r.url().endsWith("/api/v1/datasets") && r.method() === "POST");
  await dialog.getByRole("button", { name: "Add dataset", exact: true }).click();
  expect((await request).postData()).not.toContain('name="split"');
  await expect(dialog.getByText(/Dataset uci_credit uploaded/)).toBeVisible();
  expect(state.posts()).toBe(1);
});

test("older server split rejection allows correction but uncertain uploads are not resent", async ({ page }) => {
  test.skip(!enabled, "Needs upload-enabled workspace");
  await setup(page);
  let posts = 0;
  await page.route("**/api/v1/datasets", async route => {
    if (route.request().method() !== "POST") return route.fallback();
    posts++;
    if (posts === 1) return route.fulfill({ status: 422, json: { detail: "Split file required by this server" } });
    return route.fulfill({ status: 503, json: { detail: "Processing outcome unavailable" } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Datasets", exact: true }).click();
  await page.getByRole("button", { name: "Upload dataset", exact: true }).click();
  const dialog = page.getByRole("dialog");
  async function fill(manual: boolean) {
    for (const [label, name] of [["Source dataset", "source.csv"], ["Dataset descriptor (dataset.json)", "dataset.json"], ...(manual ? [["Split File (Optional)", "split.csv"]] : [])]) {
      await dialog.getByLabel(label, { exact: true }).setInputFiles({ name, mimeType: "text/plain", buffer: Buffer.from("fixture") });
    }
    await dialog.getByRole("button", { name: "Add dataset", exact: true }).click();
  }
  await fill(false);
  await expect(dialog.getByRole("alert")).toContainText("Split file required by this server");
  await dialog.getByRole("button", { name: "Choose corrected dataset files" }).click();
  await fill(true);
  await expect(dialog.getByRole("alert")).toContainText("Inspect inventory/catalogue");
  await expect(dialog.getByRole("button", { name: "Add dataset", exact: true })).toBeDisabled();
  await expect(dialog.getByRole("button", { name: "Choose corrected dataset files" })).toHaveCount(0);
  expect(posts).toBe(2);
});
