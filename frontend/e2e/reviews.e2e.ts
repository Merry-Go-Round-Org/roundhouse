import { test, expect } from "@playwright/test";
const pending = {
  id: "q1",
  model_id: "m1",
  model_name: "Credit",
  model_version: "1",
  run_id: "r1",
  gate: 1,
  status: "pending",
  artifact_id: "a1",
  artifact_version: "2",
  created_at: "2026-11-05T12:00:00Z",
  resolution: null,
};
test("review artifact and local conditional draft never submit a decision", async ({
  page,
}) => {
  const posts: string[] = [];
  await page.route("**/api/v1/**", (route) => {
    if (new URL(route.request().url()).pathname === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "mock-user", username: "fixture-user", role: "approver", active: true } });
    if (route.request().method() === "POST") posts.push(route.request().url());
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/reviews"))
      return route.fulfill({
        json: url.searchParams.get("status") === "pending" ? [pending] : [],
      });
    if (url.pathname.endsWith("/download"))
      return route.fulfill({
        contentType: "text/markdown",
        body: "# Model draft\n\n## Evidence\nReview this evidence.",
      });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Review queue", exact: true }).click();
  await page.getByRole("button", { name: "Inspect q1", exact: true }).click();
  const initialConfirmation=page.getByLabel("I inspected this artifact and confirm the exact version and decision above",{exact:true});
  await expect(initialConfirmation).toBeVisible();
  await expect(page.getByRole("button", { name: "Prepare draft", exact: true })).toHaveCount(0);
  expect(await initialConfirmation.evaluate((input) =>
    getComputedStyle(input.parentElement!).display)).toBe("flex");
  expect((await initialConfirmation.boundingBox())!.width).toBeLessThan(32);
  await expect(initialConfirmation).toBeDisabled();

  await page.getByRole("button", { name: "Preview review artifact" }).click();
  await expect(
    page.getByRole("heading", { name: "Model draft", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Choose a decision.", { exact: true })).toBeVisible();
  await page
    .getByLabel("Decision", { exact: true })
    .selectOption("accepted_with_conditions");
  await page
    .getByLabel("Rationale", { exact: true })
    .fill("Restrict use until evidence improves");
  await page
    .getByLabel("Conditions (one per line)", { exact: true })
    .fill("Review quarterly\nKeep usage limits");
  await expect(
    page.getByRole("region", { name: "Decision summary", exact: true }),
  ).toContainText('"artifact_version": "2"');
  await expect(
    page.getByRole("button", { name: "Submit decision", exact: true }),
  ).toBeDisabled();
  await initialConfirmation.check();
  await page.getByLabel("Rationale", { exact: true }).fill("Changed rationale");
  await expect(initialConfirmation).not.toBeChecked();
  await expect(
    page.getByRole("region", { name: "Decision summary", exact: true }),
  ).toContainText("Changed rationale");
  await page.getByLabel("Conditions (one per line)", { exact: true }).fill("");
  await expect(initialConfirmation).toBeDisabled();
  await expect(page.getByText("Enter at least one condition.", { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page
    .getByRole("button", {name:"Decided",exact:true}).click();
  await expect(
    page.getByText("No decided reviews."),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Selected review", exact: true }),
  ).toHaveCount(0);
  expect(posts).toEqual([]);
});
test("decided reviews show supplied identity and cannot prepare another decision", async ({
  page,
}) => {
  await page.route("**/api/v1/**", (route) => {
    if (new URL(route.request().url()).pathname === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "mock-user", username: "fixture-user", role: "approver", active: true } });
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/reviews"))
      return route.fulfill({
        json:
          url.searchParams.get("status") === "decided"
            ? [
                {
                  ...pending,
                  status: "decided",
                  resolution: {
                    artifact_id: "a1",
                    artifact_version: "2",
                    decision: "changes_requested",
                    rationale: "Missing citations",
                    conditions: [],
                    reviewer_id: "reviewer-1",
                    decided_at: "2026-11-05T13:00:00Z",
                  },
                },
              ]
            : [],
      });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Review queue", exact: true }).click();
  await page
    .getByRole("button", {name:"Decided",exact:true}).click();
  await page.getByRole("button", { name: "Inspect q1", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Recorded decision", exact: true }),
  ).toContainText("reviewer-1");
  await expect(
    page.getByRole("button", { name: "Prepare draft", exact: true }),
  ).toHaveCount(0);
});
test("unimplemented review listing is an error, not an empty or simulated queue", async ({
  page,
}) => {
  await page.route("**/api/v1/**", (route) =>
    new URL(route.request().url()).pathname === "/api/v1/auth/me"
      ? route.fulfill({ json: { id: "mock-user", username: "fixture-user", role: "approver", active: true } })
      : route.fulfill(
      new URL(route.request().url()).pathname.endsWith("/reviews")
        ? { status: 501, json: { detail: "Review listing not implemented" } }
        : { json: [] },
    ),
  );
  await page.goto("/");
  await page.getByRole("button", { name: "Review queue", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Review listing not implemented",
  );
  await expect(
    page.getByText("No pending reviews returned by the backend."),
  ).toHaveCount(0);
  await expect(
    page.getByRole("table", { name: "Review items", exact: true }),
  ).toHaveCount(0);
});


for (const [role, gate] of [["approver", 1], ["validator", 1], ["validator", 2]] as const)
test(`P1 ${role} Gate ${gate} explicitly opens, confirms version and submits one persisted decision`, async ({ page }) => {
  const eligible = { ...pending, gate, candidate_model_id: gate === 2 ? "candidate-1" : null, requested_by: "developer-1", approval_available: true, blockers: [] };
  const posts: { path: string; body: unknown }[] = [];
  const accepted = {
    ...eligible,
    status: "decided",
    resolution: {
      artifact_id: pending.artifact_id, artifact_version: pending.artifact_version,
      decision: "accepted_with_conditions", rationale: "Limited acceptance",
      conditions: ["Review quarterly"], reviewer_id: `${role}-1`,
      decided_at: "2026-11-07T13:00:00Z", review_elapsed_seconds: 12,
      timing_limitation: "Elapsed since opening, not active effort",
    },
  };
  let saved = false;
  await page.route("**/api/v1/**", (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    if (method === "POST") {
      posts.push({ path, body: route.request().postDataJSON?.() ?? null });
    }
    if (path === "/api/v1/auth/me")
      return route.fulfill({ json: { id: `${role}-1`, username: role, role, active: true } });
    if (path === "/api/v1/models") return route.fulfill({ json: [] });
    if (path === "/api/v1/reviews")
      return route.fulfill({ json: url.searchParams.get("status") === "decided" ? (saved ? [accepted] : []) : (saved ? [] : [eligible]) });
    if (path === "/api/v1/reviews/q1" && method === "GET")
      return route.fulfill({ json: saved ? accepted : eligible });
    if (path === "/api/v1/reviews/q1/open")
      return route.fulfill({ json: { review_id: "q1", opened_at: "2026-11-07T12:59:00Z",
        measurement: "Elapsed since explicit open; not active review effort" } });
    if (path === "/api/v1/reviews/q1/decision") {
      saved = true;
      return route.fulfill({ json: accepted });
    }
    if (path === "/api/v1/runs/r1")
      return route.fulfill({ json: {
        id: "r1", model_id: "m1", model_version: "1", dataset_id: "uci_credit",
        operation: "document", status: "completed", created_at: "2026-11-07T12:00:00Z",
        started_at: null, finished_at: "2026-11-07T13:00:00Z",
        stage: "accepted_with_conditions", error: null,
      } });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Review queue", exact: true }).click();
  await page.getByRole("button", { name: "Inspect q1" }).click();
  await page.getByLabel("Decision", { exact: true }).selectOption("accepted_with_conditions");
  await page.getByLabel("Rationale", { exact: true }).fill("Limited acceptance");
  if (gate === 2) {
    const checks = page.getByRole("region", { name: "Rebuild input checks", exact: true });
    await expect(checks).toContainText("not available in this review yet");
    await expect(checks).not.toContainText("All input checks passed");
  }
  await page.getByLabel("Conditions (one per line)").fill("Review quarterly");
  await expect(page.getByRole("button", { name: "Submit decision" })).toBeDisabled();
  await expect(page.getByLabel("Submission requirements")).toHaveText("Select Open review for audit above before submitting.");
  await page.getByRole("button", { name: "Open review for audit" }).click();
  await expect(page.getByText(/Backend recorded review open at/)).toBeVisible();
  await expect(page.getByLabel("Submission requirements")).toHaveText("Tick the confirmation after inspecting the evidence and decision.");
  await page.getByRole("checkbox", { name: /I inspected this artifact/ }).check();
  await page.getByRole("button", { name: "Submit decision" }).click();
  await expect(page.getByText(/Backend recorded accepted_with_conditions for artifact a1 version 2/)).toBeVisible();
  await expect(page.getByRole("button", {name:"Decided",exact:true})).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("region", { name: "Recorded decision" })).toContainText(`${role}-1`);
  await expect(page.getByRole("region", { name: "Recorded decision" })).toContainText("Limited acceptance");
  await expect(page.getByText(/Run r1 status: completed/)).toBeVisible();
  expect(posts.filter((p) => p.path === "/api/v1/reviews/q1/open")).toHaveLength(1);
  const decisions = posts.filter((p) => p.path === "/api/v1/reviews/q1/decision");
  expect(decisions).toHaveLength(1);
  expect(decisions[0].body).toEqual({
    artifact_id: "a1", artifact_version: "2",
    decision: "accepted_with_conditions", rationale: "Limited acceptance",
    conditions: ["Review quarterly"],
  });
});

test("P1 changed checkpoint blocks submission before a decision POST", async ({ page }) => {
  const writes: string[] = [];
  let reads = 0;
  await page.route("**/api/v1/**", (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname, method = route.request().method();
    if (method === "POST") writes.push(path);
    if (path === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "approver-1", username: "approver", role: "approver", active: true } });
    if (path === "/api/v1/models") return route.fulfill({ json: [] });
    if (path === "/api/v1/reviews") return route.fulfill({ json: [pending] });
    if (path === "/api/v1/reviews/q1" && method === "GET") {
      reads++;
      return route.fulfill({ json: reads > 1 ? { ...pending, artifact_version: "3" } : pending });
    }
    if (path === "/api/v1/reviews/q1/open")
      return route.fulfill({ json: { review_id: "q1", opened_at: "2026-11-07T13:00:00Z",
        measurement: "Elapsed since opening" } });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Review queue", exact: true }).click();
  await page.getByRole("button", { name: "Inspect q1" }).click();
  await page.getByRole("button", { name: "Open review for audit" }).click();
  await page.getByLabel("Decision", { exact: true }).selectOption("accepted");
  await page.getByLabel("Rationale", { exact: true }).fill("Reviewed");
  await page.getByRole("checkbox", { name: /I inspected this artifact/ }).check();
  await page.getByRole("button", { name: "Submit decision" }).click();
  await expect(page.getByRole("alert")).toContainText("version/gate changed");
  expect(writes).toEqual(["/api/v1/reviews/q1/open"]);
});

test("P1 independent reviewer and validation signoff cannot be bypassed by role", async ({ page }) => {
  const gate3 = { ...pending, gate: 3, requested_by: "validator-1" };
  let role = "validator";
  let id = "validator-1";
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/auth/me")
      return route.fulfill({ json: { id, username: "test", role, active: true } });
    if (path === "/api/v1/models") return route.fulfill({ json: [] });
    if (path === "/api/v1/reviews") return route.fulfill({ json: [gate3] });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Review queue", exact: true }).click();
  await page.getByRole("button", { name: "Inspect q1" }).click();
  await expect(page.getByRole("button", { name: "Open review for audit" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Submit decision" })).toBeDisabled();
  await expect(page.getByRole("option", { name: "Accepted", exact: true })).toHaveAttribute("disabled", "");
});

test("long review evidence does not stretch decision controls at desktop or mobile widths", async ({ page }) => {
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { id: "validator1", username: "validator", role: "validator", active: true } });
    if (path.endsWith("/reviews")) return route.fulfill({ json: [pending] });
    if (path.endsWith("/download")) return route.fulfill({ contentType: "text/markdown", body: "# Long evidence\n\n" + "Evidence paragraph with findings and explanations.\n\n".repeat(200) });
    return route.fulfill({ json: [] });
  });
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await page.getByRole("button", { name: "Review queue", exact: true }).click();
  await page.getByRole("button", { name: "Inspect q1", exact: true }).click();
  await page.getByRole("button", { name: "Preview review artifact", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Long evidence", exact: true })).toBeVisible();
  for (const [width, height] of [[1600,900],[1280,747],[390,844]]) {
    await page.setViewportSize({ width, height });
    const form = page.locator(".review-draft");
    expect((await form.boundingBox())!.height).toBeLessThan(width < 500 ? 1400 : 1000);
    const artifact = page.getByRole("region", { name: "Review artifact", exact: true });
    expect((await artifact.boundingBox())!.height).toBeLessThan(height);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.getByLabel("Decision", { exact: true }).selectOption("accepted");
    await page.getByLabel("Rationale", { exact: true }).fill("Reviewed evidence");
    await page.getByLabel("Rationale", { exact: true }).press("Tab");
    await page.getByRole("button", { name: "Submit decision", exact: true }).scrollIntoViewIfNeeded();
    await expect(page.getByRole("button", { name: "Submit decision", exact: true })).toBeVisible();
  }
});
