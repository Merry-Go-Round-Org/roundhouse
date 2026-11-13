import { test, expect } from "@playwright/test";
const oldReview = { id: "q1", model_id: "m1", model_name: "Credit", model_version: "1", run_id: "r1", gate: 1, status: "pending", artifact_id: "old-artifact", artifact_version: "1", created_at: "2026-11-08T12:00:00Z", resolution: null };
for (const delayedPreview of [false, true]) test(`checkpoint recovery clears ${delayedPreview ? "in-flight" : "visible"} old evidence and requires fresh inspection`, async ({ page }) => {
  let changed = false, refreshing = false;
  let releasePreview!: () => void, releaseRefresh!: () => void;
  const previewGate = new Promise<void>(resolve => { releasePreview = resolve; });
  const refreshGate = new Promise<void>(resolve => { releaseRefresh = resolve; });
  const writes: string[] = [];
  await page.route("**/api/v1/**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (request.method() !== "GET") writes.push(path);
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { id: "reviewer", username: "Reviewer", role: "validator", active: true } });
    if (path.endsWith("/reviews")) return route.fulfill({ json: [oldReview] });
    if (path.endsWith("/reviews/q1")) {
      if (refreshing) await refreshGate;
      return route.fulfill({ json: changed ? { ...oldReview, artifact_id: "new-artifact", artifact_version: "2" } : oldReview });
    }
    if (path.endsWith("/reviews/q1/open")) return route.fulfill({ json: { review_id: "q1", opened_at: "2026-11-08T12:01:00Z", measurement: "Elapsed since opening" } });
    if (path.endsWith("/old-artifact/download")) {
      if (delayedPreview) await previewGate;
      return route.fulfill({ contentType: "text/markdown", body: "# Old evidence\n\nOriginal assumptions" });
    }
    if (path.endsWith("/new-artifact/download")) return route.fulfill({ contentType: "text/markdown", body: "# Updated evidence\n\nRevised assumptions" });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Review queue", exact: true }).click();
  await page.getByRole("button", { name: "Inspect q1", exact: true }).click();
  await page.getByRole("button", { name: "Open review for audit" }).click();
  await page.getByRole("button", { name: "Preview review artifact" }).click();
  if (!delayedPreview) await expect(page.getByRole("heading", { name: "Old evidence" })).toBeVisible();
  await page.getByLabel("Decision", { exact: true }).selectOption("accepted");
  await page.getByLabel("Rationale", { exact: true }).fill("Inspected original version");
  await page.getByRole("checkbox", { name: /I inspected this artifact/ }).check();
  changed = true;
  await page.getByRole("button", { name: "Submit decision", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("version/gate changed");
  await expect(page.getByRole("heading", { name: "Old evidence" })).toHaveCount(0);
  refreshing = true;
  await page.getByRole("button", { name: "Refresh saved review status (read only)" }).click();
  await expect(page.getByRole("button", { name: "Preview review artifact" })).toBeDisabled();
  releaseRefresh();
  await expect(page.getByText("Artifact: new-artifact · artifact version 2", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Preview review artifact" }).click();
  await expect(page.getByRole("heading", { name: "Updated evidence" })).toBeVisible();
  releasePreview();
  await expect(page.getByRole("heading", { name: "Old evidence" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Submit decision", exact: true })).toBeDisabled();
  await expect(page.getByRole("checkbox", { name: /I inspected this artifact/ })).not.toBeChecked();
  await expect(page.getByRole("button", { name: "Open review for audit" })).toBeEnabled();
  expect(writes).toEqual(["/api/v1/reviews/q1/open"]);
});
