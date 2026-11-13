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
for (const outcome of ["accepted_with_conditions", "changes_requested"] as const)
test(`Chat card: approver explicitly submits ${outcome} with correct run confirmation`, async ({ page }) => {
  const posts: { path: string; body: unknown }[] = [];
  const accepted = {
    ...pending,
    status: "decided",
    resolution: {
      artifact_id: pending.artifact_id, artifact_version: pending.artifact_version,
      decision: outcome, rationale: "Limited acceptance",
      conditions: outcome === "changes_requested" ? [] : ["Review quarterly"], reviewer_id: "approver-1",
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
      return route.fulfill({ json: { id: "approver-1", username: "approver", role: "approver", active: true } });
    if (path === "/api/v1/models") return route.fulfill({ json: [] });
    if (path === "/api/v1/reviews")
      return route.fulfill({ json: url.searchParams.get("status") === "decided" ? (saved ? [accepted] : []) : (saved ? [] : [pending]) });
    if (path === "/api/v1/reviews/q1" && method === "GET")
      return route.fulfill({ json: saved ? accepted : pending });
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
        operation: "document", status: outcome === "changes_requested" ? "failed" : "completed", created_at: "2026-11-07T12:00:00Z",
        started_at: null, finished_at: "2026-11-07T13:00:00Z",
        stage: outcome, error: null,
      } });
    return route.fulfill({ json: [] });
  });
  await page.goto("/"); await page.getByRole("button", {name:"Open agent chat",exact:true}).click();
  await page.getByRole("button", { name: "Open review cards", exact: true }).click();
  await page.getByRole("button", { name: "Inspect q1" }).click();
  await page.getByLabel("Decision", { exact: true }).selectOption(outcome);
  await page.getByLabel("Rationale", { exact: true }).fill("Limited acceptance");
  if (outcome === "accepted_with_conditions") await page.getByLabel("Conditions (one per line)").fill("Review quarterly");
  await expect(page.getByRole("button", { name: "Submit decision" })).toBeDisabled();
  await page.getByRole("button", { name: "Open review for audit" }).click();
  await expect(page.getByText(/Backend recorded review open at/)).toBeVisible();
  await page.getByRole("checkbox", { name: /I inspected this artifact/ }).check();
  await page.getByRole("button", { name: "Submit decision" }).click();
  await expect(page.getByText(`Backend recorded ${outcome} for artifact a1 version 2.`, { exact: false })).toBeVisible();
  await expect(page.getByRole("button", {name:"Decided",exact:true})).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("region", { name: "Recorded decision" })).toContainText("approver-1");
  await expect(page.getByRole("region", { name: "Recorded decision" })).toContainText("Limited acceptance");
  if (outcome === "changes_requested") {
    await expect(page.getByText(/Backend recorded changes_requested/)).toContainText("Changes requested — this reviewed run is closed for revision");
    await expect(page.getByText(/Backend recorded changes_requested/)).toContainText("Check History for follow-up work");
    await expect(page.getByText(/Run r1 status: failed/)).toHaveCount(0);
  } else await expect(page.getByText(/Run r1 status: completed/)).toBeVisible();
  expect(posts.filter((p) => p.path === "/api/v1/reviews/q1/open")).toHaveLength(1);
  const decisions = posts.filter((p) => p.path === "/api/v1/reviews/q1/decision");
  expect(decisions).toHaveLength(1);
  expect(decisions[0].body).toEqual({
    artifact_id: "a1", artifact_version: "2",
    decision: outcome, rationale: "Limited acceptance",
    conditions: outcome === "changes_requested" ? [] : ["Review quarterly"],
  });
});

test("Chat card: changed checkpoint blocks submission before a decision POST", async ({ page }) => {
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
  await page.goto("/"); await page.getByRole("button", {name:"Open agent chat",exact:true}).click();
  await page.getByRole("button", { name: "Open review cards", exact: true }).click();
  await page.getByRole("button", { name: "Inspect q1" }).click();
  await page.getByRole("button", { name: "Open review for audit" }).click();
  await page.getByLabel("Decision", { exact: true }).selectOption("accepted");
  await page.getByLabel("Rationale", { exact: true }).fill("Reviewed");
  await page.getByRole("checkbox", { name: /I inspected this artifact/ }).check();
  await page.getByRole("button", { name: "Submit decision" }).click();
  await expect(page.getByRole("alert")).toContainText("version/gate changed");
  expect(writes).toEqual(["/api/v1/reviews/q1/open"]);
});

test("Chat card: independent reviewer and validation signoff cannot be bypassed by role", async ({ page }) => {
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
  await page.goto("/"); await page.getByRole("button", {name:"Open agent chat",exact:true}).click();
  await page.getByRole("button", { name: "Open review cards", exact: true }).click();
  await page.getByRole("button", { name: "Inspect q1" }).click();
  await expect(page.getByRole("button", { name: "Open review for audit" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Submit decision" })).toBeDisabled();
  await expect(page.getByRole("option", { name: "Accepted", exact: true })).toHaveAttribute("disabled", "");
});

test("chat review cards show only the selected model and discard selection on model change",async({page})=>{
  const model={id:"m1",name:"Credit",version:"1",language:"R",dataset_id:"uci_credit",purpose:"Credit",owner:null,lifecycle_status:"development",created_at:"2026-11-07T10:00:00Z"};
  await page.route("**/api/v1/**",route=>{
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith("/auth/me"))return route.fulfill({json:{id:"reviewer",role:"approver"}});
    if(path.endsWith("/models/m1"))return route.fulfill({json:model});
    if(path.endsWith("/models/m2"))return route.fulfill({json:{...model,id:"m2",name:"Fraud"}});
    if(path.endsWith("/reviews"))return route.fulfill({json:[pending,{...pending,id:"other",model_id:"m2",model_name:"Fraud"}]});
    return route.fulfill({json:[]});
  });
  await page.goto("/?model=m1"); await page.getByRole("button", {name:"Open agent chat",exact:true}).click();await page.getByRole("button",{name:"Open review cards",exact:true}).click();
  const cards=page.getByRole("region",{name:"Chat review cards",exact:true});
  await expect(cards.getByRole("button",{name:"Inspect q1",exact:true})).toBeVisible();
  await expect(cards.getByRole("button",{name:"Inspect other",exact:true})).toHaveCount(0);
  await cards.getByRole("button",{name:"Inspect q1",exact:true}).click();
  await page.setViewportSize({width:390,height:844});expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.goto("/?model=m2"); await page.getByRole("button", {name:"Open agent chat",exact:true}).click();await page.getByRole("button",{name:"Open review cards",exact:true}).click();
  await expect(cards.getByRole("button",{name:"Inspect other",exact:true})).toBeVisible();
  await expect(cards.getByRole("button",{name:"Inspect q1",exact:true})).toHaveCount(0);
  await expect(cards.getByRole("region",{name:"Selected review",exact:true})).toHaveCount(0);
});
