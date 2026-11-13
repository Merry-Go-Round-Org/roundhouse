import { test, expect, type Page } from "@playwright/test";
const evidence = {
  candidate_model_id: "candidate", candidate_version: "v2", candidate_bundle_sha256: "a".repeat(64),
  rebuild_run_id: "r1", parity_run_id: "agent-parity", executed_at: "2026-11-11T10:00:00Z",
  passed: true, control: {status:"PASS",exit_code:0,reason:"Unchanged rows scored"},
  checks: ["reject_missing_row_id","reject_duplicated_row_id","reject_target_leakage"].map(check_id => ({check_id,status:"PASS",exit_code:1,reason:"Rejected with exit code 1"})),
};
const review = {id:"q2",model_id:"parent",model_name:"Credit",model_version:"v1",run_id:"r1",gate:2,status:"pending",
  artifact_id:"a1",artifact_version:"1",created_at:"2026-11-11T09:00:00Z",resolution:null,
  requested_by:"operator",candidate_model_id:"candidate",approval_available:true,blockers:[]};
async function setup(page: Page) {
  const state = { evidence: evidence as unknown, candidate:"candidate", writes:[] as string[] };
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() !== "GET") state.writes.push(path);
    if (path.endsWith("/auth/me")) return route.fulfill({json:{id:"reviewer",role:"approver"}});
    const item = {...review,candidate_model_id:state.candidate,input_contract:state.evidence};
    if (path.endsWith("/reviews")) return route.fulfill({json:[item]});
    if (path.endsWith("/reviews/q2")) return route.fulfill({json:item});
    if (path.includes("/models/")) return route.fulfill({json: {id:state.candidate,name:"Candidate",version:"v2",language:"Python",dataset_id:"uci_credit",purpose:"Credit",owner:null,lifecycle_status:"development",created_at:"2026-11-11T09:00:00Z"}});
    return route.fulfill({json:[]});
  });
  return state;
}
async function open(page: Page, chat: boolean) {
  await page.goto("/");
  if (chat) {
    await page.getByRole("button",{name:"Open agent chat",exact:true}).click();
    await page.getByRole("button",{name:"Open review cards",exact:true}).click();
  } else await page.getByRole("button",{name:"Review queue",exact:true}).click();
  await page.getByRole("button",{name:"Inspect q2",exact:true}).click();
}
for (const chat of [false,true]) test(`Gate 2 ${chat ? "chat" : "queue"} reads persisted evidence and reloads without execution`,async({page})=>{
  const state=await setup(page); await open(page,chat);
  const panel=page.getByRole("region",{name:"Rebuild input checks"});
  await expect(panel).toContainText("All input checks passed.");
  await expect(panel).toContainText("Candidate version: v2");
  await panel.getByText("Input-check evidence identifiers",{exact:true}).click();
  await expect(panel).toContainText("Agent parity run: agent-parity");
  await page.getByRole("button",{name:"Reload reviews",exact:true}).click();
  await page.getByRole("button",{name:"Inspect q2",exact:true}).click();
  await expect(panel).toContainText("All input checks passed.");
  await open(page,chat);
  await expect(panel).toContainText("All input checks passed.");
  expect(state.writes).toEqual([]);
});
for (const scenario of ["missing","failed","foreign","malformed","not-run"] as const) test(`Gate 2 ${scenario} input evidence does not change parity eligibility`,async({page})=>{
  const state=await setup(page);
  state.evidence = scenario === "missing" ? null : scenario === "foreign" ? {...evidence,candidate_model_id:"other"}
    : scenario === "malformed" ? {...evidence,checks:[]}
    : scenario === "not-run" ? {...evidence,passed:false,control:{status:"FAIL",exit_code:1,reason:"Control failed"},checks:evidence.checks.map(c=>({...c,status:"NOT_RUN",exit_code:null}))}
    : {...evidence,passed:false,checks:[{...evidence.checks[0],status:"FAIL",exit_code:0},...evidence.checks.slice(1)]};
  await open(page,false);
  const panel=page.getByRole("region",{name:"Rebuild input checks"});
  await expect(panel).toContainText(scenario === "missing" ? "not available in this review yet" : ["foreign","malformed"].includes(scenario) ? "No passing result can be confirmed" : "Input checks did not all pass.");
  if (scenario === "not-run") await expect(panel).toContainText("NOT_RUN");
  await expect(page.getByRole("option",{name:"Accepted",exact:true})).toHaveJSProperty("disabled",false);
  expect(state.writes).toEqual([]);
});
test("changing the review candidate removes previous input evidence",async({page})=>{
  const state=await setup(page); await open(page,false);
  const panel=page.getByRole("region",{name:"Rebuild input checks"});
  await expect(panel).toContainText("All input checks passed.");
  state.candidate="new-candidate";
  await page.getByRole("button",{name:"Reload reviews",exact:true}).click();
  await page.getByRole("button",{name:"Inspect q2",exact:true}).click();
  await expect(panel).not.toContainText("All input checks passed.");
  await expect(panel).toContainText("No passing result can be confirmed");
  expect(state.writes).toEqual([]);
});
