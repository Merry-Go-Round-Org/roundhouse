import { test, expect } from "@playwright/test";

test("cancelling a dataset file picker preserves the upload dialog and selected files", async ({ page }) => {
  const writes: string[] = [];
  await page.route("**/api/v1/**", route => {
    if (route.request().method() !== "GET") writes.push(route.request().url());
    if (new URL(route.request().url()).pathname.endsWith("/auth/me"))
      return route.fulfill({ json: { id: "actor1", role: "developer" } });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Datasets", exact: true }).click();
  await page.getByRole("button", { name: "Upload dataset", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Upload dataset" });
  const source = dialog.getByLabel("Source dataset");
  await source.setInputFiles({ name: "source.csv", mimeType: "text/csv", buffer: Buffer.from("id,value\n1,2") });
  // Native OS picker UI is outside Playwright. Reproduce its bubbling cancel event.
  await dialog.getByLabel("Dataset descriptor (dataset.json)").dispatchEvent("cancel", { bubbles: true });
  await expect(dialog).toBeVisible();
  expect(await source.evaluate(el => (el as HTMLInputElement).files?.[0]?.name)).toBe("source.csv");
  await dialog.getByLabel("Split File (Optional)").dispatchEvent("cancel", { bubbles: true });
  await expect(dialog).toBeVisible();
  await source.focus();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  expect(writes).toEqual([]);
});
