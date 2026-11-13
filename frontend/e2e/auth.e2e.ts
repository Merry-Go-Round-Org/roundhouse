import { expect, test } from "@playwright/test";

const admin = {
  id: "admin-id",
  username: "fixture-admin",
  role: "admin",
  active: true,
};

test("sign in, restore session, and revoke it without an AI call", async ({ page }) => {
  let signedIn = false;
  const paths: string[] = [];
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    paths.push(path);
    if (path.endsWith("/auth/me")) return route.fulfill(
      signedIn ? { json: admin } : { status: 401, json: { detail: "Sign in required" } },
    );
    if (path.endsWith("/auth/login")) {
      signedIn = true;
      return route.fulfill({ json: admin });
    }
    if (path.endsWith("/auth/logout")) {
      signedIn = false;
      return route.fulfill({ json: { status: "signed_out" } });
    }
    if (path.endsWith("/models")) return route.fulfill({ json: [] });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in to the workspace" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Upload model" })).toHaveCount(0);
  await page.getByLabel("Username").fill("fixture-admin");
  await page.getByLabel("Password").fill("private-test-value");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByText("Role:")).toContainText("admin");
  await expect(page.getByRole("button", { name: "Upload model" })).toBeVisible();
  await page.reload();
  await expect(page.getByText("Signed in as")).toContainText("fixture-admin");
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Sign in to the workspace" })).toBeVisible();
  expect(paths.some((path) => path.endsWith("/auth/login"))).toBe(true);
  expect(paths.some((path) => path.endsWith("/auth/logout"))).toBe(true);
  expect(paths.some((path) => path.endsWith("/conversations"))).toBe(false);
});

test("401 on live data expires the session, while 403 does not", async ({ page }) => {
  let code = 403;
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/me")) return route.fulfill({ json: admin });
    if (path.endsWith("/models")) return route.fulfill({
      status: code, json: { detail: code === 401 ? "Sign in required" : "Not permitted" },
    });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("Not permitted");
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  code = 401;
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect(page.getByRole("heading", { name: "Sign in to the workspace" })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("session expired");
});

test("validator can inspect but cannot start developer upload", async ({ page }) => {
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/me")) return route.fulfill({
      json: { id: "validator-id", username: "validator", role: "validator", active: true },
    });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await expect(page.getByText("Role:")).toContainText("validator");
  await expect(page.getByRole("button", { name: "Upload model" })).toBeDisabled();
  await page.getByRole("button", { name: "Datasets" }).click();
  await expect(page.getByRole("button", { name: "Upload dataset" })).toBeDisabled();
});


test("signed-in live workspace shares demo visual shell without loading demo inventory", async ({ page }) => {
  const account = { id: "live-user", username: "live-user", role: "developer", active: true };
  let fetchedModels = false;
  await page.route("**/api/v1/**", (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v1/auth/me") return route.fulfill({ json: account });
    if (url.pathname === "/api/v1/models") fetchedModels = true;
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await expect(page.locator(".app-shell .sidebar .brand")).toContainText("roundhouse");
  await expect(page.locator(".app-shell .topbar")).toContainText("Live workspace");
  await expect(page.locator(".live-main-shell")).toBeVisible();
  await expect(page.getByRole("button", {name:"Open agent chat", exact:true})).toBeVisible();
  await expect(page.getByRole("link", { name: "View labelled demo" })).toHaveAttribute("href", "/demo");
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  expect(fetchedModels).toBe(true);
  await page.goto("/demo");
  await expect(page.locator(".app-shell .sidebar .brand")).toContainText("roundhouse");
  await expect(page.locator(".demo-pill")).toContainText("Demo workspace");
  await expect(page.getByRole("button", { name: "Sign out" })).toHaveCount(0);
});
