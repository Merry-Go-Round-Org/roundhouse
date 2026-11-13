import { test, expect } from "@playwright/test";
const model = {
  id: "candidate",
  name: "Rebuilt Credit",
  version: "v2",
  language: "Python",
  dataset_id: "uci_credit",
  purpose: "Credit",
  owner: null,
  lifecycle_status: "development",
  created_at: "2026-11-07T10:00:00Z",
};
const review = {
  id: "q2",
  model_id: "parent",
  model_name: "Credit",
  model_version: "v1",
  run_id: "r1",
  gate: 2,
  status: "pending",
  artifact_id: "a1",
  artifact_version: "1",
  created_at: "2026-11-07T10:00:00Z",
  resolution: null,
  requested_by: "operator",
  candidate_model_id: "candidate",
  approval_available: false,
  blockers: ["official_parity_not_passed"],
};
const parity = {
  id: "parity",
  candidate_model_id: "candidate",
  candidate_version: "v2",
  candidate_bundle_sha256: "a".repeat(64),
  parent_model_id: "parent",
  parent_version: "v1",
  rebuild_run_id: "r1",
  initiator: "agent",
  actor_id: "operator",
  status: "completed",
  passed: false,
  error: null,
  created_at: "2026-11-07T11:00:00Z",
  result: { n_rows: 100, n_above_tolerance: 1, max_absolute_difference: 0.2 },
};
for (const scenario of [
  "failed parity",
  "foreign passing result",
  "missing eligibility",
]) {
  test(`Gate 2 refuses acceptance with ${scenario}`, async ({ page }) => {
    const writes: string[] = [];
    await page.route("**/api/v1/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      if (route.request().method() === "POST") writes.push(path);
      if (path.endsWith("/auth/me"))
        return route.fulfill({ json: { id: "reviewer", role: "approver" } });
      if (path.endsWith("/reviews"))
        return route.fulfill({
          json: [
            scenario === "missing eligibility"
              ? { ...review, approval_available: undefined, blockers: [] }
              : review,
          ],
        });
      if (path.endsWith("/models/candidate"))
        return route.fulfill({ json: model });
      if (path.endsWith("/parity-runs"))
        return route.fulfill({
          json: [
            scenario === "foreign passing result"
              ? { ...parity, passed: true, candidate_model_id: "unrelated" }
              : parity,
          ],
        });
      return route.fulfill({ json: [] });
    });
    await page.goto("/");
    await page
      .getByRole("button", { name: "Review queue", exact: true })
      .click();
    await page.getByRole("button", { name: "Inspect q2", exact: true }).click();
    const panel = page.getByRole("region", { name: "Official parity" });
    if (scenario === "foreign passing result")
      await expect(panel.getByRole("alert")).toContainText("another model");
    else await expect(panel.getByRole("article")).toContainText("FAIL");
    await expect(
      page.getByRole("option", { name: "Accepted", exact: true }),
    ).toHaveJSProperty("disabled", true);
    await expect(
      page.getByRole("option", { name: "Changes requested", exact: true }),
    ).toHaveJSProperty("disabled", false);
    await expect(
      page.getByRole("button", { name: "Submit decision", exact: true }),
    ).toBeDisabled();
    expect(writes).toEqual([]);
  });
}
