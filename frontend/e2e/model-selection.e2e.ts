import { test, expect } from "@playwright/test";

for (const width of [1280, 390]) {
  test(`model selection is bounded and read-only at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    const models = Array.from({ length: 21 }, (_, i) => ({ id: `model-${i}`, name: `Model ${i}`, version: "1.0",
      language: "R", dataset_id: "uci_credit", purpose: "Example", owner: null, lifecycle_status: "development", created_at: "2026-11-11T12:00:00Z" }));
    const writes: string[] = [];
    await page.route("**/api/v1/**", route => {
      const url = new URL(route.request().url());
      if (route.request().method() !== "GET") writes.push(url.pathname);
      if (url.pathname.endsWith("/auth/me")) return route.fulfill({ json: { id: "operator", role: "admin" } });
      if (url.pathname.endsWith("/models")) {
        const offset = Number(url.searchParams.get("offset") ?? 0);
        return route.fulfill({ json: models.slice(offset, offset + 20) });
      }
      return route.fulfill({ json: models.find(m => url.pathname.endsWith(`/models/${m.id}`)) ?? [] });
    });
    await page.goto("/");
    const selection = page.getByRole("region", { name: "Model selection", exact: true });
    const all = page.getByRole("checkbox", { name: "Select visible models", exact: true });
    await expect(selection).toContainText("0 selected");
    const first = page.getByRole("checkbox", { name: "Select Model 0 (model-0), version 1.0", exact: true });
    await first.focus(); await page.keyboard.press("Space");
    await expect(first).toBeChecked();
    await expect(all).toHaveJSProperty("indeterminate", true);
    await selection.getByRole("button", { name: "Review selection" }).click();
    await expect(page.getByRole("region", { name: "Selected models", exact: true })).toContainText("Model 0 · version 1.0 · model-0");
    await expect(selection.getByRole("button", { name: "Delete selected" })).toBeEnabled();
    await all.check();
    await expect(selection).toContainText("20 selected");
    await expect(page.getByRole("region", { name: "Selected models", exact: true })).toHaveCount(0);
    await page.getByLabel("Search this inventory page").fill("Model 19");
    await expect(selection).toContainText("0 selected");
    await all.check(); await expect(selection).toContainText("1 selected");
    await page.getByRole("button", { name: "Clear filters", exact: true }).click();
    await expect(selection).toContainText("0 selected");
    await all.check();
    await page.getByRole("button", { name: "Next models", exact: true }).click();
    await expect(selection).toContainText("0 selected");
    await all.check(); await expect(selection).toContainText("1 selected");
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(selection).toContainText("0 selected");
    await all.check();
    await selection.getByRole("button", { name: "Clear selection" }).click();
    await expect(all).not.toBeChecked();
    await page.getByRole("button", { name: "Model 20", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Model 20", exact: true, level: 1 })).toBeVisible();
    await page.getByRole("button", { name: "Back to inventory", exact: true }).click();
    await expect(selection).toContainText("0 selected");
    expect(writes).toEqual([]);
  });
}
