import { test, expect, type Page } from "@playwright/test";

const review = {
  id: "q3", model_id: "m1", model_name: "Credit", model_version: "1",
  run_id: "r1", gate: 3, status: "pending", artifact_id: "a1", artifact_version: "v1",
  created_at: "2026-11-10T12:00:00Z", resolution: null, requested_by: "validator1",
  approval_available: false, hard_blockers: [],
  blockers: ["split_provenance", "saved_metric_consistency"],
  limitations: ["split_provenance", "saved_metric_consistency"],
  allowed_decisions: ["changes_requested", "accepted_with_conditions"],
};
async function setup(page: Page) {
  const state = { fresh: { ...review }, posts: [] as Record<string, unknown>[], decided: false, reject: false };
  const saved = () => state.decided ? { ...state.fresh, status: "decided", resolution: {
    ...state.posts[0], reviewer_id: "approver1", decided_at: "2026-11-10T13:00:00Z",
  } } : state.fresh;
  await page.route("**/api/v1/**", route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { id: "approver1", username: "Reviewer", role: "approver", active: true } });
    if (path.endsWith("/reviews")) return route.fulfill({ json: [saved()] });
    if (path.endsWith("/reviews/q3")) return route.fulfill({ json: saved() });
    if (path.endsWith("/reviews/q3/open")) return route.fulfill({ json: { review_id: "q3", opened_at: "2026-11-10T12:30:00Z", measurement: "Elapsed since open" } });
    if (path.endsWith("/reviews/q3/decision")) {
      state.posts.push(request.postDataJSON());
      if (state.reject) return route.fulfill({ status: 422, json: { detail: { code: "limitation_acknowledgements_required", message: "Refresh limitations" } } });
      state.decided = true;
      return route.fulfill({ json: saved() });
    }
    return route.fulfill({ json: [] });
  });
  return state;
}
async function open(page: Page, chat = false) {
  await page.goto("/");
  if (chat) {
    await page.getByRole("button", { name: "Open agent chat", exact: true }).click();
    await page.getByRole("button", { name: "Open review cards", exact: true }).click();
  } else await page.getByRole("button", { name: "Review queue", exact: true }).click();
  await page.getByRole("button", { name: "Inspect q3", exact: true }).click();
  await page.getByRole("button", { name: "Open review for audit", exact: true }).click();
  await expect(page.getByRole("button", { name: "Review opened in Backend" })).toBeVisible();
  await page.getByLabel("Decision", { exact: true }).selectOption("accepted_with_conditions");
  await page.getByLabel("Rationale", { exact: true }).fill("Reviewed limitations");
}
async function acknowledge(page: Page) {
  await page.getByLabel("Acknowledgement for split_provenance", { exact: true }).fill("split_provenance: Restrict use until provenance is established");
  await page.getByLabel("Acknowledgement for saved_metric_consistency", { exact: true }).fill("Review metrics quarterly");
  await page.getByRole("checkbox", { name: /I inspected this artifact/ }).check();
}
for (const chat of [false, true]) test(`Gate 3 ${chat ? "chat" : "queue"} requires every acknowledgement and restores saved conditions`, async ({ page }) => {
  const state = await setup(page);
  await open(page, chat);
  await expect(page.getByRole("option", { name: "Accepted", exact: true })).toHaveJSProperty("disabled", true);
  await page.getByLabel("Acknowledgement for split_provenance", { exact: true }).fill("Restrict use");
  await expect(page.getByRole("checkbox", { name: /I inspected this artifact/ })).toBeDisabled();
  await page.getByLabel("Acknowledgement for saved_metric_consistency", { exact: true }).fill("saved_metric_consistency:");
  await expect(page.getByRole("checkbox", { name: /I inspected this artifact/ })).toBeDisabled();
  await acknowledge(page);
  await page.getByLabel("Acknowledgement for saved_metric_consistency", { exact: true }).fill("Review metrics annually");
  await expect(page.getByRole("checkbox", { name: /I inspected this artifact/ })).not.toBeChecked();
  await page.getByRole("checkbox", { name: /I inspected this artifact/ }).check();
  await page.getByRole("button", { name: "Submit decision", exact: true }).click();
  await expect(page.getByRole("region", { name: "Recorded decision", exact: true })).toContainText("saved_metric_consistency: Review metrics annually");
  expect(state.posts).toEqual([{ artifact_id: "a1", artifact_version: "v1", decision: "accepted_with_conditions",
    rationale: "Reviewed limitations", conditions: ["split_provenance: Restrict use until provenance is established", "saved_metric_consistency: Review metrics annually"] }]);
  await page.reload();
  if (chat) {
    await page.getByRole("button", { name: "Open agent chat", exact: true }).click();
    await page.getByRole("button", { name: "Open review cards", exact: true }).click();
  } else await page.getByRole("button", { name: "Review queue", exact: true }).click();
  await page.getByRole("button", { name: "Decided", exact: true }).click();
  await page.getByRole("button", { name: "Inspect q3", exact: true }).click();
  await expect(page.getByRole("region", { name: "Recorded decision", exact: true })).toContainText("split_provenance: Restrict use until provenance is established");
  expect(state.posts).toHaveLength(1);
});
for (const changed of ["limitations", "allowed_decisions"] as const) test(`Gate 3 preflight refuses changed ${changed} without posting`, async ({ page }) => {
  const state = await setup(page);
  await open(page);
  await acknowledge(page);
  state.fresh = { ...state.fresh, [changed]: changed === "limitations" ? ["split_provenance"] : ["changes_requested"] };
  await page.getByRole("button", { name: "Submit decision", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("No decision was submitted");
  await expect(page.getByRole("checkbox", { name: /I inspected this artifact/ })).not.toBeChecked();
  expect(state.posts).toHaveLength(0);
});
test("Gate 3 server rejection requires reconciliation and never retries the decision", async ({ page }) => {
  const state = await setup(page);
  state.reject = true;
  await open(page);
  await acknowledge(page);
  await page.getByRole("button", { name: "Submit decision", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("No automatic retry");
  await expect(page.getByRole("button", { name: "Submit decision", exact: true })).toBeDisabled();
  expect(state.posts).toHaveLength(1);
});
