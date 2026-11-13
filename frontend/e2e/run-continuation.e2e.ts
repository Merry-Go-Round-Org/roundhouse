import { test, expect, type Page } from "@playwright/test";
const model = {id:"m1",name:"Credit",version:"v1",language:"R",dataset_id:"uci_credit",purpose:"Credit",owner:null,lifecycle_status:"development",created_at:"2026-11-07T10:00:00Z"};
const run = {id:"r1",model_id:"m1",model_version:"v1",dataset_id:"uci_credit",operation:"rebuild",status:"awaiting_review",stage:"awaiting_review",created_at:"2026-11-07T10:00:00Z",started_at:null,finished_at:null,error:null};
const review = {id:"review1",run_id:"r1",model_id:"m1",model_name:"Credit",model_version:"v1",gate:2,status:"decided",artifact_id:"a1",artifact_version:"v1",created_at:"2026-11-07T10:00:00Z",requested_by:"operator",resolution:{artifact_id:"a1",artifact_version:"v1",decision:"changes_requested",rationale:"Revise candidate",conditions:[],reviewer_id:"reviewer",decided_at:"2026-11-07T11:00:00Z"}};
async function setup(page: Page) {
  const state = {actor:"operator",task:"t1",status:202,posts:[] as unknown[],run:structuredClone(run)};
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/me")) return route.fulfill({json:{id:state.actor,role:"developer"}});
    if (path.endsWith("/resume")) {
      state.posts.push(route.request().postDataJSON()); state.run.status = "queued"; state.run.stage = "resuming";
      return route.fulfill(state.status === 202 ? {status:202,json:state.run} : {status:state.status,json:{detail:"Continuation saved; recovery will retry."}});
    }
    if (path.endsWith("/result")) return route.fulfill({json:{run_id:"r1",result:{status:"awaiting_review",task_id:state.task,checkpoint_id:"review1"}}});
    if (path.endsWith("/reviews/review1")) return route.fulfill({json:review});
    if (path.endsWith("/models/m1")) return route.fulfill({json:model});
    if (path.endsWith("/runs/r1")) return route.fulfill({json:state.run});
    if (path.endsWith("/runs")) return route.fulfill({json:[state.run]});
    return route.fulfill({json:[]});
  });
  return state;
}
async function open(page: Page) {
  await page.goto("/?model=m1");
  await page.getByRole("tab",{name:"History",exact:true}).click();
  await page.getByRole("button",{name:"Inspect r1",exact:true}).click();
  await page.getByRole("button",{name:"Read saved continuation state"}).click();
}
test("initiator explicitly resumes a recorded rejection once", async ({page}) => {
  const state = await setup(page); await open(page);
  const panel = page.getByRole("region",{name:"Run continuation"});
  await expect(panel).toContainText("changes_requested");
  await expect(page.getByRole("button",{name:"Resume run",exact:true})).toBeDisabled();
  await panel.getByRole("checkbox").check();
  await page.getByRole("button",{name:"Resume run",exact:true}).click();
  await expect(panel).toContainText("Continuation acknowledged");
  expect(state.posts).toEqual([{review_id:"review1"}]);
  await page.getByRole("button",{name:"Refresh run details"}).click();
  await expect(panel).toHaveCount(0);
});
test("stale checkpoint needs fresh confirmation and cannot write", async ({page}) => {
  const state = await setup(page); await open(page);
  const panel = page.getByRole("region",{name:"Run continuation"});
  await panel.getByRole("checkbox").check(); state.task = "t2";
  await page.getByRole("button",{name:"Resume run",exact:true}).click();
  await expect(panel.getByRole("alert")).toContainText("changed");
  expect(state.posts).toHaveLength(0);
  await expect(panel.getByRole("checkbox")).toHaveCount(0);
});
test("dispatch uncertainty reconciles saved state without duplicate continuation", async ({page}) => {
  const state = await setup(page); state.status = 503; await open(page);
  const panel = page.getByRole("region",{name:"Run continuation"});
  await panel.getByRole("checkbox").check();
  await page.getByRole("button",{name:"Resume run",exact:true}).click();
  await expect(panel.getByRole("alert")).toContainText("No automatic retry");
  await page.getByRole("button",{name:"Read saved continuation state"}).click();
  await expect(panel.getByRole("alert")).toContainText("No paused agent run");
  expect(state.posts).toHaveLength(1);
});
test("another operator cannot prepare continuation", async ({page}) => {
  const state = await setup(page); state.actor = "someone"; await open(page);
  await expect(page.getByRole("alert")).toContainText("initiating operator");
  await expect(page.getByRole("button",{name:"Resume run",exact:true})).toHaveCount(0);
  expect(state.posts).toHaveLength(0);
});

test("refresh hides continuation controls until fresh run details arrive",async({page})=>{
  const state=await setup(page);await open(page);
  const panel=page.getByRole("region",{name:"Run continuation"});
  await panel.getByRole("checkbox").check();
  let release!:()=>void;
  const gate=new Promise<void>(resolve=>{release=resolve;});
  await page.route("**/api/v1/runs/r1",async route=>{
    await gate;await route.fulfill({json:state.run});
  },{times:1});
  try {
    await page.getByRole("button",{name:"Refresh run details"}).click();
    await expect(page.getByRole("region",{name:"Selected run"}).getByRole("status")).toContainText("Loading run details");
    await expect(panel).toHaveCount(0);
    expect(state.posts).toHaveLength(0);
  } finally {release();}
  await panel.getByRole("button",{name:"Read saved continuation state"}).click();
  await expect(panel.getByRole("checkbox")).not.toBeChecked();
  await expect(panel.getByRole("button",{name:"Resume run",exact:true})).toBeDisabled();
  await panel.getByRole("checkbox").check();
  await panel.getByRole("button",{name:"Resume run",exact:true}).click();
  await expect(panel).toContainText("Continuation acknowledged");
  expect(state.posts).toEqual([{review_id:"review1"}]);
});
