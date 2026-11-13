import {test,expect,type Page} from "@playwright/test";
const model={id:"m1",name:"Credit",version:"v1",language:"R",dataset_id:"uci_credit",purpose:"Credit",owner:null,lifecycle_status:"development",created_at:"2026-11-07T10:00:00Z"};
const finding={id:"f1",model_id:"m1",report_id:"report1",check_id:"fairness",severity:"review",status:"open",summary:"Review fairness",owner_id:null as string|null,due_date:null as string|null,comments:[] as {actor_id:string;comment:string;at:string}[]};
async function setup(page:Page,role="validator") {
  const state={finding:structuredClone(finding),patches:[] as Record<string,unknown>[],error:false};
  await page.route("**/api/v1/**",route=>{
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith("/auth/me"))return route.fulfill({json:{id:"reviewer1",role}});
    if(path.endsWith("/models/m1"))return route.fulfill({json:model});
    if(path.endsWith("/findings/owners"))return route.fulfill({json:[{id:"owner1",username:"Analyst",role:"developer"}]});
    if(path.endsWith("/findings/f1")) {
      const patch=route.request().postDataJSON();state.patches.push(patch);
      if(state.error)return route.fulfill({status:503,json:{detail:"Update outcome uncertain"}});
      state.finding={...state.finding,...patch,comments:[...state.finding.comments,{actor_id:"reviewer1",comment:patch.comment,at:"2026-11-07T12:00:00Z"}]};
      return route.fulfill({json:state.finding});
    }
    if(path.endsWith("/findings"))return route.fulfill({json:[state.finding]});
    return route.fulfill({json:[]});
  });
  await page.goto("/?model=m1");await page.getByRole("tab",{name:"Validation",exact:true}).click();
  await page.getByRole("button",{name:"View persisted findings"}).click();
  return state;
}
test("reviewer confirms a minimal finding patch and recorded comment",async({page})=>{
  const state=await setup(page);const card=page.getByRole("article",{name:"Finding f1"});
  await card.getByLabel("Status",{exact:true}).selectOption("in_progress");
  await card.getByLabel("Owner",{exact:true}).selectOption("owner1");
  await card.getByLabel("Due date (UTC)",{exact:true}).fill("2026-11-08T10:30");
  await card.getByLabel("Update comment",{exact:true}).fill("Assign follow-up");
  await expect(card.getByRole("button",{name:"Save finding update"})).toBeDisabled();
  await card.getByRole("checkbox").check();
  await card.getByRole("button",{name:"Save finding update"}).click();
  await expect(card.getByRole("status")).toContainText("update saved");
  expect(state.patches).toEqual([{comment:"Assign follow-up",status:"in_progress",owner_id:"owner1",due_date:"2026-11-08T10:30:00.000Z"}]);
  await page.getByRole("button",{name:"Refresh findings"}).click();
  await expect(card).toContainText("in_progress");
});
test("non-reviewer reads findings without edit controls",async({page})=>{
  const state=await setup(page,"developer");
  await expect(page.getByRole("region",{name:"Persisted findings"})).toContainText("Read-only");
  await expect(page.getByRole("button",{name:"Save finding update"})).toHaveCount(0);
  expect(state.patches).toHaveLength(0);
});
test("stale snapshot blocks patch and uncertain write locks until refresh",async({page})=>{
  const state=await setup(page);const card=page.getByRole("article",{name:"Finding f1"});
  await card.getByLabel("Update comment",{exact:true}).fill("Inspect");
  await card.getByRole("checkbox").check();state.finding.status="in_progress";
  await card.getByRole("button",{name:"Save finding update"}).click();
  await expect(card.getByRole("alert")).toContainText("changed");expect(state.patches).toHaveLength(0);
  await page.getByRole("button",{name:"Refresh findings"}).click();
  await card.getByLabel("Update comment",{exact:true}).fill("Inspect again");
  await card.getByRole("checkbox").check();state.error=true;
  await card.getByRole("button",{name:"Save finding update"}).click();
  await expect(card.getByRole("alert")).toContainText("No automatic retry");
  await expect(card.getByRole("button",{name:"Save finding update"})).toBeDisabled();expect(state.patches).toHaveLength(1);
});

test("refresh hides editing until a delayed snapshot arrives and preserves the next edit",async({page})=>{
  const state=await setup(page);
  const card=page.getByRole("article",{name:"Finding f1"});
  await card.getByLabel("Update comment",{exact:true}).fill("Old draft");
  state.finding.status="in_progress";
  let release!:()=>void;
  const responseGate=new Promise<void>(resolve=>{release=resolve;});
  await page.route("**/api/v1/findings?*",async route=>{
    await responseGate;
    await route.fulfill({json:[state.finding]});
  },{times:1});
  try {
    await page.getByRole("button",{name:"Refresh findings"}).click();
    await expect(page.getByRole("status")).toContainText("Loading findings");
    await expect(page.getByRole("button",{name:"Refresh findings"})).toBeDisabled();
    await expect(card).toHaveCount(0);
    expect(state.patches).toHaveLength(0);
  } finally {release();}
  await expect(card.getByLabel("Status",{exact:true})).toHaveValue("in_progress");
  await expect(card.getByLabel("Update comment",{exact:true})).toHaveValue("");
  await card.getByLabel("Update comment",{exact:true}).fill("Inspect again");
  await card.getByRole("checkbox").check();
  await card.getByRole("button",{name:"Save finding update"}).click();
  await expect(card.getByRole("status")).toContainText("update saved");
  expect(state.patches).toEqual([{comment:"Inspect again"}]);
});
