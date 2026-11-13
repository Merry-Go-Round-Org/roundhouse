import { test, expect } from "@playwright/test";
for (const width of [1280, 390]) test(`workspace overview and history navigation work at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 844 });
  const posts: string[] = [];
  const model = { id: "m1", name: "Credit", version: "1", language: "R", dataset_id: "uci_credit", purpose: "Example", owner: null, lifecycle_status: "development", created_at: "2026-11-08T12:00:00Z" };
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "POST") posts.push(path);
    return route.fulfill({ json: path.endsWith("/auth/me") ? { id: "u1", username: "operator", role: "developer", active: true }
      : path === "/api/v1/models" ? [model] : path === "/api/v1/models/m1" ? model : [] });
  });
  await page.goto("/?model=m1");
  await page.getByRole("tab", { name: "History", exact: true }).click();
  await expect(page.getByRole("tab", { name: "History", exact: true })).toHaveAttribute("aria-selected", "true");
  const home = page.getByRole("button", { name: "Merry Go Round", exact: true });
  await home.focus(); await home.press("Enter");
  await expect(page.getByRole("heading", { name: "Workspaces", exact: true })).toBeVisible();
  await page.goBack();
  await expect(page.getByRole("tab", { name: "History", exact: true })).toHaveAttribute("aria-selected", "true");
  await page.goForward();
  await expect(page.getByRole("region", { name: "Available workspaces" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Workspaces", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Open Merry Go Round workspace" }).click();
  await expect(page.getByRole("tab", { name: "History", exact: true })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("button", { name: "Datasets", exact: true }).click();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Datasets", exact: true })).toBeVisible();
  await page.getByRole("button", { name: width < 600 ? "Merry Go Round" : "Open workspace overview", exact: true }).click();
  await expect(page.getByRole("region", { name: "Available workspaces" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.screenshot({ path: `test-results/workspace-overview-${width}.png` });
  expect(posts).toEqual([]);
});
test("workspace navigation preserves a chat draft and does not admit a non-admin accounts route", async ({ page }) => {
  await page.route("**/api/v1/**", route => route.fulfill({ json: new URL(route.request().url()).pathname.endsWith("/auth/me") ? { id: "u1", username: "operator", role: "developer", active: true } : [] }));
  await page.goto("/?page=Accounts");
  await expect(page.getByRole("heading", { name: "Inventory", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Open agent chat", exact: true }).click();
  await page.getByLabel("Message the agent").fill("Keep this draft");
  await page.getByRole("button", { name: "Minimize chat", exact: true }).click();
  await page.getByRole("button", { name: "Merry Go Round", exact: true }).click();
  await page.getByRole("button", { name: "Open Merry Go Round workspace" }).click();
  await page.getByRole("button", { name: "Open agent chat", exact: true }).click();
  await expect(page.getByLabel("Message the agent")).toHaveValue("Keep this draft");
});
