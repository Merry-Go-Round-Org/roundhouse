import { expect, test } from "@playwright/test";

test.describe("Real FastAPI cookie-session integration (provider disabled)", () => {
  test.skip(process.env.AUTH_LIVE_E2E !== "true", "Requires isolated API and seeded test users");

  async function login(page: import("@playwright/test").Page, name: string) {
    await page.goto("/");
    await page.getByLabel("Username").fill(name);
    await page.getByLabel("Password").fill(process.env.AUTH_E2E_PASSWORD!);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  }

  test("developer signs in, restores real HTTP-only cookie, and signs out", async ({ page }) => {
    await login(page, "ci-developer");
    await expect(page.getByText("Role:")).toContainText("developer");
    await expect(page.getByRole("button", { name: "Upload model" })).toBeVisible();
    const user = await page.evaluate(async () => {
      const response = await fetch("/api/v1/auth/me", { credentials: "include" });
      return { status: response.status, data: await response.json() };
    });
    expect(user.status).toBe(200);
    expect(user.data.username).toBe("ci-developer");
    await page.reload();
    await expect(page.getByText("Signed in as")).toContainText("ci-developer");
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page.getByRole("heading", { name: "Sign in to the workspace" })).toBeVisible();
    const anonymousStatus = await page.evaluate(async () =>
      (await fetch("/api/v1/auth/me", { credentials: "include" })).status,
    );
    expect(anonymousStatus).toBe(401);
  });

  test("developer cannot approve a review; server revocation forces sign-in", async ({ page }) => {
    await login(page, "ci-developer");
    const status = await page.evaluate(async () => {
      const res = await fetch("/api/v1/reviews/no-such-review/decision", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ artifact_id: "a", artifact_version: "1", decision: "accepted", rationale: "test" }),
      });
      return res.status;
    });
    expect(status).toBe(403);
    await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
    await page.evaluate(async () => {
      await fetch("/api/v1/auth/logout", { method: "POST", credentials: "include" });
    });
    await page.getByRole("button", { name: "Refresh" }).click();
    await expect(page.getByRole("heading", { name: "Sign in to the workspace" })).toBeVisible();
    await expect(page.getByRole("status")).toContainText("session expired");
  });

  test("P1 real approver records a version-bound Gate 3 decision, run transition and audit history", async ({ page }) => {
    await login(page, "ci-approver");
    const who = await page.evaluate(async () =>
      (await (await fetch("/api/v1/auth/me", { credentials: "include" })).json()).id as string,
    );
    await page.getByRole("button", { name: "Review queue", exact: true }).click();
    await page.getByRole("button", { name: "Inspect ci-review-gate3" }).click();
    await expect(page.getByText(/Artifact:/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Submit decision" })).toBeDisabled();
    await page.getByRole("button", { name: "Open review for audit" }).click();
    await expect(page.getByText(/Backend recorded review open at/)).toBeVisible();
    await page.getByLabel("Decision", { exact: true }).selectOption("accepted_with_conditions");
    await page.getByLabel("Rationale", { exact: true }).fill("Versioned evidence inspected by CI approver");
    await page.getByLabel("Conditions (one per line)").fill("Reassess quarterly");
    await expect(page.getByRole("button", { name: "Submit decision" })).toBeDisabled();
    await page.getByRole("checkbox", { name: /I inspected this artifact/ }).check();
    await page.getByRole("button", { name: "Submit decision" }).click();
    await expect(page.getByRole("button", {name:"Decided",exact:true})).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByRole("region", { name: "Recorded decision" }))
      .toContainText("Reassess quarterly");
    await expect(page.getByText(/Run ci-review-run status: completed/)).toBeVisible();
    const evidence = await page.evaluate(async () => {
      const urls = [
        "/api/v1/reviews/ci-review-gate3",
        "/api/v1/runs/ci-review-run",
        "/api/v1/models/ci-review-model/history",
      ];
      const responses = await Promise.all(urls.map((url) => fetch(url, {
        credentials: "include", cache: "no-store",
      })));
      return {
        statuses: responses.map((r) => r.status),
        review: await responses[0].json(),
        run: await responses[1].json(),
        events: await responses[2].json(),
      };
    });
    expect(evidence.statuses).toEqual([200, 200, 200]);
    expect(evidence.review.status).toBe("decided");
    expect(evidence.review.resolution.reviewer_id).toBe(who);
    expect(evidence.review.resolution.artifact_id).toBe(evidence.review.artifact_id);
    expect(evidence.review.resolution.artifact_version).toBe(evidence.review.artifact_version);
    expect(evidence.review.resolution.conditions).toEqual(["Reassess quarterly"]);
    expect(evidence.run.status).toBe("completed");
    expect(evidence.events.some((event: { resource_id: string; action: string; payload: { decision?: string } }) =>
      event.resource_id === "ci-review-gate3" &&
      event.action === "human_review_decision" &&
      event.payload.decision === "accepted_with_conditions")).toBe(true);
    expect(evidence.events.some((event: { resource_id: string; action: string }) =>
      event.resource_id === "ci-review-gate3" && event.action === "review_opened")).toBe(true);
    const repeat = await page.evaluate(async () => {
      const response = await fetch("/api/v1/reviews/ci-review-gate3/decision", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          artifact_id: "never-resubmit", artifact_version: "wrong",
          decision: "accepted", rationale: "Blocked replay",
        }),
      });
      return response.status;
    });
    expect(repeat).toBe(409);
  });

  test("P2 real FastAPI dashboard, persisted PSI evidence and guarded monitoring submission", async ({ page }) => {
    await login(page, "ci-validator");
    await page.getByRole("button", { name: "Dashboard", exact: true }).click();
    const dash = page.getByRole("region", { name: "Live dashboard" });
    await expect(dash).toBeVisible();
    await expect(dash.getByText("PSI events", { exact: true })).toBeVisible();
    await expect(dash.getByText("Revalidation due", { exact: true })).toBeVisible();
    const persisted = await page.evaluate(async () => {
      const response = await fetch("/api/v1/dashboard", {
        cache: "no-store", credentials: "include",
      });
      return { code: response.status, body: await response.json() };
    });
    expect(persisted.code).toBe(200);
    expect(persisted.body.source).toBe("persisted_records");
    expect(persisted.body.monitoring_events).toBe(1);
    expect(persisted.body.red_alerts).toBeGreaterThanOrEqual(1);
    await page.getByRole("button", { name: "Inspect PSI monitoring" }).click();
    const portfolio = page.getByRole("region", { name: "Live monitoring" });
    await expect(portfolio).toBeVisible();
    const rawPortfolio = await page.evaluate(async () => {
      const response = await fetch("/api/v1/monitoring?offset=0&limit=20", {
        credentials: "include", cache: "no-store",
      });
      return { code: response.status, json: await response.json() };
    });
    expect(rawPortfolio.code).toBe(200);
    expect(rawPortfolio.json.map((row: { model_id: string }) => row.model_id)).toContain("ci-psi-model");
    await expect.poll(async () => {
      const errors = await portfolio.getByRole("alert").allTextContents();
      if (errors.length) return "error: " + errors.join("; ");
      return await portfolio.getByRole("table", { name: "Monitoring portfolio records" }).count()
        ? "loaded" : "loading";
    }).toBe("loaded");
    await expect(portfolio.getByRole("cell", { name: /CI monitored model/ })).toBeVisible();
    await portfolio.getByRole("button", { name: "Inspect latest PSI" }).click();
    await expect(portfolio.getByRole("heading", { name: "CI declared simulated shift" })).toBeVisible();
    await expect(portfolio.getByText(/Declared shift:/)).toContainText("AGE");
    await expect(portfolio.getByText(/Red feature PSI:/)).toContainText("AGE");
    await expect(portfolio.getByText(/artificial shift of the frozen test partition/)).toBeVisible();
    await portfolio.getByRole("button", { name: "Open model monitoring" })
      .filter({ hasNotText: "CI Review validation fixture" }).first().click();
    await expect(page.getByRole("tab", { name: "Monitoring" })).toHaveAttribute("aria-selected", "true");
    const detail = page.getByRole("region", { name: "Model PSI monitoring" });
    await expect(detail.getByRole("article", { name: "PSI window CI declared simulated shift" })).toBeVisible();
    await expect(detail.getByText(/uploaded\s+binary model, registered package, verified dataset version and accepted\s+Gate 3/)).toBeVisible();
    const attempts = await page.evaluate(async () => {
      const before = await (await fetch("/api/v1/runs?model_id=ci-psi-model", { credentials: "include" })).json();
      const response = await fetch("/api/v1/models/ci-psi-model/monitoring/simulate", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ feature: "AGE", factor: 2.0, window_label: "Rejected test" }),
      });
      const after = await (await fetch("/api/v1/runs?model_id=ci-psi-model", { credentials: "include" })).json();
      return { code: response.status, before: before.length, after: after.length };
    });
    expect(attempts).toEqual({ code: 409, before: 1, after: 1 });
  });

  test("validator role cannot upload and retains legitimate read access", async ({ page }) => {
    await login(page, "ci-validator");
    await expect(page.getByText("Role:")).toContainText("validator");
    await expect(page.getByRole("button", { name: "Upload model" })).toBeDisabled();
    await page.getByRole("button", { name: "Datasets" }).click();
    await expect(page.getByRole("button", { name: "Upload dataset" })).toBeDisabled();
    await page.getByRole("button", { name: "Sign out" }).click();
  });
});
