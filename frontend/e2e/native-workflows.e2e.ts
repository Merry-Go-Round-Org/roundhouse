import { test, expect, type Page } from "@playwright/test";
const hash = "a".repeat(64),
  date = "2026-11-08T12:00:00Z";
const model = {
  id: "m1",
  name: "Credit",
  version: "1",
  language: "R",
  dataset_id: "uci_credit",
  purpose: "Demo",
  owner: null,
  lifecycle_status: "development",
  created_at: date,
};
const candidate = {
  candidate_id: "c1",
  fitted_model: "saved_model",
  executable: true,
  execution_error: null,
  manifest: {
    name: { value: "Credit", source: "file", evidence: "model.rds" },
    version: { value: "1", source: "file", evidence: "model.rds" },
    language: { value: "R", source: "artifact", evidence: "inspection" },
    owner: {
      value: null,
      source: "unknown",
      reason: "Needs business confirmation",
    },
  },
  dataset_mapping: null,
  scoring: {
    entry_point: "score.py",
    wrapper: true,
    wrapper_sha256: hash,
    self_check: "sample scored",
  },
  files: ["model.rds"],
  problems: [],
};
const archive = {
  id: "archive1",
  actor_id: "u1",
  sha256: hash,
  status: "awaiting_review",
  files: [
    { path: "model.rds", sha256: hash },
    { path: "notes.txt", sha256: hash },
  ],
  plan: {
    candidates: [
      candidate,
      {
        ...candidate,
        candidate_id: "c2",
        fitted_model: "training_code_only",
        executable: false,
        scoring: { entry_point: null, wrapper: false },
        files: ["notes.txt"],
      },
    ],
    unassigned_files: [],
  },
  plan_sha256: hash,
  decision: null,
  error: null,
  created_at: date,
  models: [],
};
const proposal = {
  id: "p1",
  source_run_id: "analysis1",
  source_task_id: "task1",
  conversation_id: null,
  model_id: "m1",
  type: "open_finding",
  args: {
    title: "Missing rationale",
    severity: "medium",
    impact: "Review gap",
    remediation: "Explain preprocessing",
  },
  rationale: "Review the missing rationale",
  evidence: [{ kind: "check", ref: "DOC-01" }],
  required_role: "validator",
  preconditions: { model_version: "1", bundle_sha256: hash },
  status: "pending",
  decision: null,
  result: null,
  created_at: date,
};
async function setup(page: Page, role = "developer") {
  const s = {
    archive: JSON.parse(JSON.stringify(archive)),
    proposal: JSON.parse(JSON.stringify(proposal)),
    uploads: 0,
    upload503: false,
    decisionLost: false,
    posts: [] as { path: string; body: any }[],
  };
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname,
      post = req.method() === "POST";
    if (post)
      s.posts.push({
        path,
        body: req.headers()["content-type"]?.includes("application/json")
          ? (req.postData() ? req.postDataJSON() : null)
          : req.postData(),
      });
    if (path.endsWith("/auth/me"))
      return route.fulfill({
        json: { id: "u1", username: "operator", role, active: true },
      });
    if (path === "/api/v1/models") return route.fulfill({ json: [model] });
    if (path === "/api/v1/models/m1") return route.fulfill({ json: model });
    if (path === "/api/v1/intake/capabilities")
      return route.fulfill({
        json: {
          harness_profiles: { survey: true, intake: true },
          gate_0_role: "developer",
        },
      });
    if (path === "/api/v1/intake/archives" && post) {
      s.uploads++;
      if (s.upload503) {
        s.archive.status = "failed";
        return route.fulfill({
          status: 503,
          json: {
            detail: {
              archive_id: "archive1",
              message: "Archive dispatch unavailable",
            },
          },
        });
      }
      return route.fulfill({ status: 202, json: s.archive });
    }
    if (path === "/api/v1/intake/archives")
      return route.fulfill({ json: s.uploads ? [s.archive] : [] });
    if (path === "/api/v1/intake/archives/archive1")
      return route.fulfill({ json: s.archive });
    if (path.endsWith("/archive1/file"))
      return route.fulfill({
        contentType: "application/octet-stream",
        body: "sample source",
      });
    if (path.endsWith("/archive1/decision")) {
      s.archive.status = "confirmed";
      s.archive.decision = (req.postData() ? req.postDataJSON() : null);
      return route.fulfill({ json: s.archive });
    }
    if (path === "/api/v1/proposals/actions")
      return route.fulfill({
        json: {
          open_finding: {
            required_role: "validator",
            execution_available: true,
            args_schema: {},
          },
          start_revalidation: {
            required_role: "validator",
            execution_available: true,
            args_schema: {},
          },
        },
      });
    if (path === "/api/v1/proposals")
      return route.fulfill({ json: [s.proposal] });
    if (path === "/api/v1/proposals/p1")
      return route.fulfill({ json: s.proposal });
    if (path === "/api/v1/proposals/p1/decision") {
      s.proposal.status = (req.postData() ? req.postDataJSON() : null).decision;
      s.proposal.decision = (req.postData() ? req.postDataJSON() : null);
      if (s.decisionLost) return route.abort("failed");
      return route.fulfill({ json: s.proposal });
    }
    if (path === "/api/v1/proposals/p1/execute") {
      s.proposal.status = "executed";
      s.proposal.result = { finding_id: "finding1" };
      return route.fulfill({ json: s.proposal });
    }
    if (path === "/api/v1/findings/owners")
      return route.fulfill({
        json: [{ id: "owner1", username: "Reviewer", role: "validator" }],
      });
    if (path === "/api/v1/runs/analysis1")
      return route.fulfill({
        json: {
          id: "analysis1",
          model_id: "m1",
          model_version: "1",
          operation: "monitor",
          dataset_id: "uci_credit",
          status: "completed",
          created_at: date,
          started_at: date,
          finished_at: date,
          stage: null,
          error: null,
        },
      });
    if (path === "/api/v1/runs/analysis1/analysis")
      return route.fulfill({
        json: {
          run_id: "analysis1",
          kind: "investigation",
          inputs: {},
          result: {
            ai_native_run_id: "native1",
            outcome: { outcome: "problem" },
            exports: {
              "investigation_report.md":
                "## Investigation result\n\n**Review** the changed units.",
            },
            artifact_ids: ["a1"],
            proposal_ids: [],
          },
        },
      });
    return route.fulfill({ json: [] });
  });
  return s;
}
async function upload(page: Page) {
  await page.goto("/");
  await page
    .getByRole("button", { name: "Archive intake", exact: true })
    .click();
  await page
    .getByLabel("Model archive (ZIP)")
    .setInputFiles({
      name: "models.zip",
      mimeType: "application/zip",
      buffer: Buffer.from("fixture archive"),
    });
  await page
    .getByRole("button", { name: "Upload archive", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Saved archive" }),
  ).toBeVisible();
}
async function preparePlan(page: Page) {
  await page
    .getByRole("combobox", { name: "Include candidate c1", exact: true })
    .selectOption("accept");
  await page
    .getByRole("combobox", { name: "Include candidate c2", exact: true })
    .selectOption("exclude");
  await page.getByLabel("c1 owner", { exact: true }).fill("Risk team");
  await page
    .getByLabel("Gate 0 rationale")
    .fill("Reviewed source and scoring wrapper");
  await page
    .getByLabel(
      "I reviewed the candidate files, metadata and any agent-written wrapper for this exact plan",
    )
    .check();
}
async function openProposal(page: Page) {
  await page.goto("/");
  await page
    .getByRole("button", { name: "Agent proposals", exact: true })
    .click();
  await page.getByRole("button", { name: /Open a validation finding/ }).click();
  await expect(
    page.getByRole("region", { name: "Saved proposal" }),
  ).toBeVisible();
}
async function prepareProposal(page: Page) {
  await page
    .getByRole("combobox", { name: "Proposal decision", exact: true })
    .selectOption("approved");
  await page.getByLabel("Decision rationale").fill("Evidence reviewed");
  await page.getByLabel("Finding owner").selectOption("owner1");
  await page.getByLabel("Due date (UTC)").fill("2026-12-01T12:00");
  await page
    .getByLabel(
      "I inspected the evidence and confirm this exact proposal and decision",
    )
    .check();
}

test("live archive upload sends multipart archive and Gate 0 binds all candidates to the exact plan", async ({
  page,
}) => {
  const s = await setup(page);
  await upload(page);
  await expect(
    page.getByLabel("Include candidate c2").locator('option[value="accept"]'),
  ).toHaveAttribute("disabled", "");
  await preparePlan(page);
  await page.getByRole("button", { name: "Confirm Gate 0 plan" }).click();
  await expect(
    page.getByRole("region", { name: "Saved archive" }),
  ).toContainText("confirmed");
  expect(s.posts[0].body).toContain('name="archive"');
  const decisions = s.posts.filter((p) => p.path.endsWith("/decision"));
  expect(decisions).toHaveLength(1);
  expect(decisions[0].body).toEqual({
    plan_sha256: hash,
    rationale: "Reviewed source and scoring wrapper",
    candidates: [
      { candidate_id: "c1", accept: true, manifest: { owner: "Risk team" } },
      { candidate_id: "c2", accept: false, manifest: {} },
    ],
  });
});
test("changed plan blocks Gate 0 before POST and reload requires a new confirmation", async ({
  page,
}) => {
  const s = await setup(page);
  await upload(page);
  await preparePlan(page);
  s.archive.plan_sha256 = "b".repeat(64);
  await page.getByRole("button", { name: "Confirm Gate 0 plan" }).click();
  await expect(page.getByRole("alert")).toContainText("plan changed");
  expect(s.posts.filter((p) => p.path.endsWith("/decision"))).toHaveLength(0);
  await page.getByRole("button", { name: "Reload saved archive" }).click();
  await expect(page.getByLabel("Gate 0 rationale")).toHaveValue("");
  await expect(
    page.getByRole("button", { name: "Confirm Gate 0 plan" }),
  ).toBeDisabled();
});
test("dispatch failure retains archive ID and does not repeat an upload", async ({
  page,
}) => {
  const s = await setup(page);
  s.upload503 = true;
  await upload(page);
  await expect(
    page.getByRole("region", { name: "Saved archive" }),
  ).toContainText("failed");
  await expect(
    page.getByRole("button", { name: "Upload archive", exact: true }),
  ).toBeDisabled();
  expect(s.uploads).toBe(1);
  await page.reload();
  await page
    .getByRole("button", { name: "Archive intake", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Upload archive", exact: true }),
  ).toBeDisabled();
  expect(s.uploads).toBe(1);
});
test("finding approval includes owner and UTC due date; execution is a separate confirmed request", async ({
  page,
}) => {
  const s = await setup(page, "validator");
  await openProposal(page);
  await prepareProposal(page);
  await page.getByRole("button", { name: "Submit proposal decision" }).click();
  await expect(
    page.getByRole("button", { name: "Execute approved action", exact: true }),
  ).toBeDisabled();
  expect(s.posts.filter((p) => p.path.endsWith("/execute"))).toHaveLength(0);
  expect(s.posts[0].body).toEqual({
    decision: "approved",
    rationale: "Evidence reviewed",
    owner_id: "owner1",
    due_date: "2026-12-01T12:00:00.000Z",
  });
  await page
    .getByLabel("I confirm execution of this exact approved action")
    .check();
  await page
    .getByRole("button", { name: "Execute approved action", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Proposal outcome" }),
  ).toContainText("finding1");
  expect(s.posts.filter((p) => p.path.endsWith("/execute"))).toHaveLength(1);
  expect(s.posts.at(-1)?.body).toBeNull();
});
test("stale proposal prevents POST and lost decision response requires reconciliation", async ({
  page,
}) => {
  const s = await setup(page, "validator");
  await openProposal(page);
  await prepareProposal(page);
  s.proposal.preconditions.model_version = "2";
  await page.getByRole("button", { name: "Submit proposal decision" }).click();
  await expect(page.getByRole("alert")).toContainText("Proposal changed");
  expect(s.posts).toHaveLength(0);
  await page.getByRole("button", { name: "Reload saved proposal" }).click();
  await prepareProposal(page);
  s.decisionLost = true;
  await page.getByRole("button", { name: "Submit proposal decision" }).click();
  await expect(page.getByRole("alert")).toContainText("no write was retried");
  await expect(
    page.getByRole("button", { name: "Submit proposal decision" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Reload saved proposal" }).click();
  await expect(
    page.getByRole("button", { name: "Execute approved action", exact: true }),
  ).toBeDisabled();
  expect(s.posts).toHaveLength(1);
});
test("approver cannot substitute for developer or validator roles", async ({
  page,
}) => {
  const s = await setup(page, "approver");
  await page.goto("/");
  await page
    .getByRole("button", { name: "Archive intake", exact: true })
    .click();
  await expect(page.getByLabel("Model archive (ZIP)")).toBeDisabled();
  await page
    .getByRole("button", { name: "Agent proposals", exact: true })
    .click();
  await page.getByRole("button", { name: /Open a validation finding/ }).click();
  await expect(
    page.getByRole("region", { name: "Saved proposal" }),
  ).toContainText("requires the validator role");
  await expect(
    page.getByRole("button", { name: "Submit proposal decision" }),
  ).toHaveCount(0);
  expect(s.posts).toHaveLength(0);
});
test("analysis report is retrieved and rendered as Markdown with no action writes", async ({
  page,
}) => {
  const s = await setup(page, "validator");
  await openProposal(page);
  await expect(
    page.getByRole("heading", { name: "Investigation result" }),
  ).toBeVisible();
  await page.getByText("Analysis evidence and export details", { exact: true }).click();
  const references = page.locator("details").filter({ has: page.getByText("Analysis evidence and export details", { exact: true }) });
  await expect(references).toContainText("investigation_report.md");
  await expect(references).toContainText("native1");
  await expect(references).toContainText("analysis1");
  await expect(references.locator("pre")).toHaveCount(0);
  await expect(references).not.toContainText("changed units");
  expect(s.posts).toHaveLength(0);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/native-proposal-mobile.png",
    fullPage: true,
  });
});

test("admin can upload and confirm Gate 0 under the updated contract", async ({ page }) => {
  const s = await setup(page, "admin");
  await upload(page);
  await preparePlan(page);
  await page.getByRole("button", { name: "Confirm Gate 0 plan" }).click();
  await expect(page.getByRole("region", { name: "Saved archive" })).toContainText("confirmed");
  expect(s.posts.filter(p => p.path.endsWith("/decision"))).toHaveLength(1);
});

test("admin can explicitly approve and execute a proposal", async ({ page }) => {
  const s = await setup(page, "admin");
  await openProposal(page);
  await prepareProposal(page);
  await page.getByRole("button", { name: "Submit proposal decision" }).click();
  await expect(page.getByRole("button", { name: "Execute approved action", exact: true })).toBeDisabled();
  await page.getByLabel("I confirm execution of this exact approved action").check();
  await page.getByRole("button", { name: "Execute approved action", exact: true }).click();
  await expect(page.getByRole("region", { name: "Proposal outcome" })).toContainText("finding1");
  expect(s.posts.filter(p => p.path.endsWith("/decision"))).toHaveLength(1);
  expect(s.posts.filter(p => p.path.endsWith("/execute"))).toHaveLength(1);
});

test("archive candidate gaps remain visible after reload without treating absent manifest as fatal", async ({ page }) => {
  const state = await setup(page);
  state.archive.plan.candidates[0].problems = ["Missing scoring script: scoring/predict.R"];
  state.archive.plan.unassigned_files = ["notes.txt"];
  await upload(page);
  const problems = page.getByRole("region", { name: "Candidate c1 problems", exact: true });
  await expect(problems).toContainText("scoring/predict.R");
  await expect(page.getByLabel("Include candidate c1").locator('option[value="accept"]')).toHaveAttribute("disabled", "");
  await expect(page.getByText(/do not require an existing manifest/)).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Archive intake", exact: true }).click();
  await page.getByRole("button", { name: /archive1/ }).first().click();
  await expect(problems).toContainText("scoring/predict.R");
  expect(state.posts.filter(p => p.path.endsWith("/decision"))).toHaveLength(0);
});


test("Gate 0 readiness polling leads to one confirmed decision and visible registered model", async ({ page }) => {
  const s = await setup(page);
  s.archive.status = "running";
  s.archive.plan = null;
  s.archive.plan_sha256 = null;
  await upload(page);
  await expect(page.getByText("The agent is inspecting your archive. The intake plan will appear here when ready.")).toBeVisible();
  s.archive = JSON.parse(JSON.stringify(archive));
  await expect(page.getByRole("region", { name: "Gate 0 intake plan" })).toBeVisible({ timeout: 10000 });
  await expect(page.getByText(/Ready for Gate 0/)).toBeVisible();
  await preparePlan(page);
  await page.getByRole("button", { name: "Confirm Gate 0 plan" }).click();
  await expect(page.getByText(/Gate 0 is saved/)).toBeVisible();
  s.archive.status = "completed";
  s.archive.models = [{ model_id: "m1", name: "Credit", model_version: "1", document_run_id: "doc1", reproduction: null }];
  await expect(page.getByRole("button", { name: "Credit · 1", exact: true })).toBeVisible({ timeout: 10000 });
  await page.getByRole("button", { name: "Reload saved archive" }).click();
  await expect(page.getByText("Intake is complete. Open a registered model below to follow its documentation and later reviews.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Confirm Gate 0 plan" })).toHaveCount(0);
  expect(s.posts.filter(p => p.path.endsWith("/decision"))).toHaveLength(1);
});

test("Gate 0 missing metadata can be filled and prerequisites explain disabled confirmation", async ({ page }) => {
  const s = await setup(page);
  delete s.archive.plan.candidates[0].manifest.name;
  await upload(page);
  await expect(page.getByLabel("Gate 0 requirements")).toContainText("Choose Accept into inventory or Leave out for c1");
  await preparePlan(page);
  await expect(page.getByLabel("Gate 0 requirements")).toContainText("enter a model name");
  await expect(page.getByRole("button", { name: "Confirm Gate 0 plan" })).toBeDisabled();
  await page.getByLabel("c1 name", { exact: true }).fill("Recovered_model");
  await expect(page.getByLabel("Gate 0 requirements")).toContainText("Confirm that you reviewed this exact plan");
  await page.getByLabel("I reviewed the candidate files, metadata and any agent-written wrapper for this exact plan").check();
  await page.getByRole("button", { name: "Confirm Gate 0 plan" }).click();
  await expect(page.getByText(/Gate 0 is saved/)).toBeVisible();
  expect(s.posts.filter(p => p.path.endsWith("/decision"))[0].body.candidates[0].manifest.name).toBe("Recovered_model");
});

for (const role of ["validator", "approver"]) {
  test(`Gate 0 explains ${role} role restriction without allowing a decision`, async ({ page }) => {
    const s = await setup(page, role);
    s.uploads = 1;
    await page.goto("/");
    await page.getByRole("button", { name: "Review queue", exact: true }).click();
    await page.getByRole("button", { name: "Review archive intake plans" }).click();
    await page.getByRole("button", { name: /Inspect archive archive1/ }).click();
    await expect(page.getByText(/Your current role can inspect this plan but cannot confirm it/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Confirm Gate 0 plan" })).toBeDisabled();
    await expect(page.getByRole("combobox", { name: "Include candidate c1", exact: true })).toBeDisabled();
    expect(s.posts).toHaveLength(0);
  });
}

test("Gate 0 empty candidate plan explains why confirmation cannot proceed", async ({ page }) => {
  const s = await setup(page);
  s.archive.plan.candidates = [];
  await upload(page);
  await page.getByLabel("Gate 0 rationale").fill("No fitted model found");
  await page.getByLabel("I reviewed the candidate files, metadata and any agent-written wrapper for this exact plan").check();
  await expect(page.getByLabel("Gate 0 requirements")).toContainText("No model candidates were found");
  await expect(page.getByRole("button", { name: "Confirm Gate 0 plan" })).toBeDisabled();
  expect(s.posts.filter(p => p.path.endsWith("/decision"))).toHaveLength(0);
});


async function analysisScenario(page: Page) {
  const s = await setup(page, "validator");
  const state = { status: "running", error: null as string | null, missing: true,
    broken: false, foreign: false, reads: 0, analysisReads: 0, text: "## Saved analysis\n\nMeasured evidence remains separate." };
  await page.route("**/api/v1/runs/analysis1", async route => {
    state.reads++;
    await route.fulfill({ json: { id: "analysis1", model_id: state.foreign ? "other" : "m1",
      model_version: "1", operation: "validate", dataset_id: "uci_credit", status: state.status,
      created_at: date, started_at: date, finished_at: null, stage: "agent_analysis", error: state.error } });
  });
  await page.route("**/api/v1/runs/analysis1/analysis", async route => {
    state.analysisReads++;
    if (state.broken) return route.fulfill({ status: 503, json: { detail: "Analysis storage unavailable" } });
    if (state.missing) return route.fulfill({ status: 404, json: { detail: "AI analysis not found" } });
    await route.fulfill({ json: { run_id: "analysis1", kind: "validation", result: {
      exports: { "validation_narrative.md": state.text }, artifact_ids: ["a1"], proposal_ids: [] } } });
  });
  return { s, state };
}

test("analysis automatically follows pending registration and failure, with read-only recovery", async ({ page }) => {
  const { s, state } = await analysisScenario(page);
  await openProposal(page);
  const panel = page.getByRole("region", { name: "Agent analysis", exact: true });
  await expect(panel).toContainText("No saved analysis report yet");
  state.status = "failed"; state.error = "AI validation run failed: turn_limit_exceeded";
  await expect(panel).toContainText("turn_limit_exceeded", { timeout: 8000 });
  await expect(panel).toContainText("This is not a passed analysis");
  await expect(panel.getByRole("link", { name: "Open model validation" })).toHaveAttribute("href", "/?model=m1&tab=Validation");
  const stopped = state.reads;
  await page.waitForTimeout(3300);
  expect(state.reads).toBe(stopped);
  state.missing = false; state.status = "awaiting_review"; state.error = null;
  await panel.getByRole("button", { name: "Refresh agent analysis" }).click();
  await expect(panel.getByRole("heading", { name: "Saved analysis", exact: true })).toBeVisible();
  await expect(panel).toContainText("waiting for human review");
  expect(s.posts).toHaveLength(0);
});

test("analysis background reads preserve the proposal draft and pause on error until explicit recovery", async ({ page }) => {
  const { s, state } = await analysisScenario(page); state.missing = false;
  await openProposal(page);
  const panel = page.getByRole("region", { name: "Agent analysis", exact: true });
  await expect(panel.getByRole("heading", { name: "Saved analysis", exact: true })).toBeVisible();
  await page.getByRole("textbox", { name: "Decision rationale", exact: true }).fill("Keep these review notes");
  state.text = "## Updated analysis\n\nNo approval is implied.";
  await expect(panel.getByRole("heading", { name: "Updated analysis", exact: true })).toBeVisible({ timeout: 8000 });
  await expect(page.getByRole("textbox", { name: "Decision rationale", exact: true })).toHaveValue("Keep these review notes");
  state.broken = true;
  await expect(panel).toContainText("Automatic updates stopped", { timeout: 8000 });
  await expect(panel.getByRole("heading", { name: "Updated analysis", exact: true })).toHaveCount(0);
  const stopped = state.reads; await page.waitForTimeout(3300); expect(state.reads).toBe(stopped);
  state.broken = false; state.status = "completed";
  await panel.getByRole("button", { name: "Refresh agent analysis" }).click();
  await expect(panel.getByRole("heading", { name: "Updated analysis", exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Decision rationale", exact: true })).toHaveValue("Keep these review notes");
  expect(s.posts).toHaveLength(0);
});

test("analysis rejects a foreign run before requesting its evidence", async ({ page }) => {
  const { s, state } = await analysisScenario(page); state.foreign = true;
  await openProposal(page);
  await expect(page.getByRole("region", { name: "Agent analysis", exact: true })).toContainText("another model or run");
  expect(state.analysisReads).toBe(0); expect(s.posts).toHaveLength(0);
});

test("analysis polling pauses in hidden panels and stops after navigation", async ({ page }) => {
  const { s, state } = await analysisScenario(page);
  await openProposal(page);
  const panel = page.getByRole("region", { name: "Agent analysis", exact: true });
  await expect(panel).toContainText("No saved analysis report yet");
  await panel.evaluate(node => { (node as HTMLElement).hidden = true; });
  const hiddenReads = state.reads;
  await page.waitForTimeout(3300); expect(state.reads).toBe(hiddenReads);
  state.missing = false;
  await page.locator(".native-analysis").evaluate(node => { (node as HTMLElement).hidden = false; });
  await expect(panel.getByRole("heading", { name: "Saved analysis", exact: true })).toBeVisible({ timeout: 8000 });
  await page.getByRole("button", { name: "Inventory", exact: true }).click();
  await expect(panel).toHaveCount(0);
  const leftReads = state.reads; await page.waitForTimeout(3300); expect(state.reads).toBe(leftReads);
  expect(s.posts).toHaveLength(0);
});

test("analysis revision request does not claim an execution failure", async ({ page }) => {
  const s = await setup(page, "validator");
  await page.route("**/api/v1/runs/analysis1", route => route.fulfill({json:{
    id:"analysis1",model_id:"m1",model_version:"1",dataset_id:"uci_credit",operation:"validate",
    status:"failed",stage:"changes_requested",error:null,created_at:"2026-11-08T12:00:00Z",started_at:null,finished_at:null
  }}));
  await openProposal(page);
  const panel=page.getByRole("region",{name:"Agent analysis",exact:true});
  await expect(panel).toContainText("Changes requested");
  await expect(panel).not.toContainText("The operation failed");
  await expect(panel).not.toContainText("requesting a new attempt");
  expect(s.posts).toHaveLength(0);
});
