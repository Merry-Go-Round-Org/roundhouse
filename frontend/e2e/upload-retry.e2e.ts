import { test, expect } from "@playwright/test";
for (const outcome of ["failed", "rejected", "uncertain"] as const) {
  test(`model upload recovery: ${outcome}`, async ({ page }) => {
    test.skip(process.env.TEST_PROPOSED_UPLOADS !== "true", "Requires upload-enabled test mode");
    let writes = 0;
    await page.route("**/api/v1/**", route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/auth/me")) return route.fulfill({ json: { id: "dev1", username: "developer", role: "developer", active: true } });
      if (path.endsWith("/models/uploads") && route.request().method() === "POST") {
        writes++;
        if (outcome === "uncertain") return route.abort("failed");
        if (outcome === "rejected") return route.fulfill({ status: 422, json: { detail: "Invalid bundle" } });
        return route.fulfill({ json: { id: "u1", status: "failed", files: [], checks: [], model_id: null, error: "Missing required file: schema.json" } });
      }
      return route.fulfill({ json: [] });
    });
    await page.goto("/");
    await page.getByRole("button", { name: "Upload model", exact: true }).click();
    const input = page.getByLabel("Model ZIP bundle");
    await input.setInputFiles({ name: "model.zip", mimeType: "application/zip", buffer: Buffer.from("test") });
    await page.getByRole("button", { name: "Upload and check model", exact: true }).click();
    await expect(page.getByRole("alert")).toBeVisible();
    if (outcome === "failed") await expect(page.getByRole("region", { name: "Upload problems", exact: true })).toContainText("Missing required file: schema.json");
    const retry = page.getByRole("button", { name: "Choose another model ZIP", exact: true });
    if (outcome === "uncertain") {
      await expect(retry).toHaveCount(0);
      await expect(input).toBeDisabled();
    } else {
      await retry.click();
      await expect(input).toBeEnabled();
      expect(await input.evaluate(el => (el as HTMLInputElement).files?.length)).toBe(0);
      expect(writes).toBe(1);
      await page.getByRole("button", { name: "Upload and check model", exact: true }).click();
      expect(writes).toBe(1);
      await input.setInputFiles({ name: "corrected.zip", mimeType: "application/zip", buffer: Buffer.from("corrected") });
      await page.getByRole("button", { name: "Upload and check model", exact: true }).click();
      await expect(retry).toBeVisible();
      expect(writes).toBe(2);
    }
  });
}
