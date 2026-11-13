import { test, expect, type Page } from "@playwright/test";
const model = {id:"m1",name:"Credit",version:"1",language:"R",dataset_id:"uci_credit",purpose:"Demo",owner:null,lifecycle_status:"development",created_at:"2026-11-05T12:00:00Z"};
const report = {id:"doc1",model_id:"m1",run_id:"r1",kind:"documentation",artifact_id:"a1",artifact_version:"1",metrics:[]};
const run = {id:"r1",model_id:"m1",model_version:"1",dataset_id:"uci_credit",operation:"validate",status:"running",stage:"Checking",created_at:model.created_at,started_at:null,finished_at:null,error:null};
const review = {id:"q1",model_id:"m1",model_name:"Credit",model_version:"1",run_id:"r1",gate:1,status:"pending",artifact_id:"a1",artifact_version:"2",created_at:model.created_at,resolution:null,approval_available:true};
async function fixture(page:Page, data:(path:string)=>unknown) {
 const writes:string[]=[];
 await page.route("**/api/v1/**",route=>{
  const path=new URL(route.request().url()).pathname;
  if(route.request().method()!=="GET")writes.push(path);
  if(path.endsWith("/auth/me"))return route.fulfill({json:{id:"operator",role:"approver",active:true}});
  if(path.endsWith("/models/m1"))return route.fulfill({json:model});
  if(path.endsWith("/models"))return route.fulfill({json:[model]});
  if(path.endsWith("/download"))return route.fulfill({contentType:"text/markdown",body:"# Readable evidence"});
  return route.fulfill({json:data(path)??[]});
 });
 await page.clock.install();
 return writes;
}
test("F3 documents arrive automatically and unchanged reader survives list updates",async({page})=>{
 let reports=[report];
 const writes=await fixture(page,p=>p.endsWith("/reports")?reports:undefined);
 await page.goto("/?model=m1");await page.getByRole("tab",{name:"Documentation",exact:true}).click();
 await page.getByRole("button",{name:"Read documentation",exact:true}).click();
 const reader=page.getByRole("region",{name:"Document reader",exact:true});
 await expect(reader).toContainText("Readable evidence");
 reports=[report,{...report,id:"doc2",artifact_id:"a2"}];
 await page.clock.fastForward(11000);
 await expect(page.getByRole("button",{name:"Read documentation",exact:true})).toHaveCount(2);
 await expect(reader).toContainText("Readable evidence");
 reports=[{...report,artifact_version:"2"},reports[1]];
 await page.clock.fastForward(11000);await expect(reader).toHaveCount(0);
 expect(writes).toEqual([]);
});
test("F3 selected history run completes automatically without writes",async({page})=>{
 let status="running";
 const writes=await fixture(page,p=>p.endsWith("/runs/r1")?{...run,status}:p.endsWith("/runs")?[{...run,status}]:undefined);
 await page.goto("/?model=m1");await page.getByRole("tab",{name:"History",exact:true}).click();
 await page.getByRole("button",{name:"Inspect r1",exact:true}).click();
 const detail=page.getByRole("region",{name:"Selected run",exact:true});
 await expect(detail).toContainText("Checking");status="completed";
 await page.clock.fastForward(4000);await expect(detail).toContainText("completed");
 await page.clock.fastForward(11000);await expect(page.getByRole("table",{name:"Saved runs"})).toContainText("completed");
 expect(writes).toEqual([]);
});
test("F3 review eligibility changes preserve notes and revoke confirmation",async({page})=>{
 let available=true;
 const writes=await fixture(page,p=>p.endsWith("/reviews/q1")?{...review,approval_available:available}:p.endsWith("/reviews")?[review]:undefined);
 await page.goto("/");await page.getByRole("button",{name:"Review queue",exact:true}).click();
 await page.getByRole("button",{name:"Inspect q1",exact:true}).click();
 await page.getByRole("button",{name:"Preview review artifact"}).click();
 await expect(page.getByRole("heading",{name:"Readable evidence",exact:true})).toBeVisible();
 await page.getByLabel("Decision",{exact:true}).selectOption("accepted");
 await page.getByLabel("Rationale",{exact:true}).fill("Keep my review notes");
 const confirmation=page.getByRole("checkbox",{name:/I inspected this artifact/});
 await confirmation.check();available=false;
 await page.clock.fastForward(11000);
 await expect(page.getByText(/This review changed. Your notes are preserved/)).toBeVisible();
 await expect(page.getByLabel("Rationale",{exact:true})).toHaveValue("Keep my review notes");
 await expect(confirmation).not.toBeChecked();await expect(page.getByRole("button",{name:"Submit decision",exact:true})).toBeDisabled();
 expect(writes).toEqual([]);
});
test("F3 monitoring portfolio picks up new records without navigation",async({page})=>{
 let rows:unknown[]=[];
 const writes=await fixture(page,p=>p.includes("monitoring")?rows:undefined);
 await page.goto("/");await page.getByRole("button",{name:"Monitoring",exact:true}).click();
 await expect(page.getByText("No models returned for this portfolio page.")).toBeVisible();
 rows=[{model_id:"m1",model_name:"New monitored model",lifecycle_status:"active",latest:null,last_run:null}];
 await page.clock.fastForward(11000);
 await expect(page.getByRole("table",{name:"Monitoring portfolio records"})).toContainText("New monitored model");
 expect(writes).toEqual([]);
});

test("F3 failed history observation stops and manual refresh reconnects",async({page})=>{
 let fail=false, reads=0;
 await fixture(page,()=>[]);
 await page.route("**/api/v1/runs?*",route=>{
  reads++;
  return route.fulfill(fail?{status:503,json:{detail:"History temporarily unavailable"}}:{json:[run]});
 });
 await page.goto("/?model=m1");await page.getByRole("tab",{name:"History",exact:true}).click();
 await expect(page.getByRole("table",{name:"Saved runs"})).toBeVisible();fail=true;
 await page.clock.fastForward(11000);
 await expect(page.getByText(/Automatic updates stopped:/)).toContainText("History temporarily unavailable");
 const stopped=reads;await page.clock.fastForward(30000);expect(reads).toBe(stopped);
 fail=false;await page.getByRole("button",{name:"Refresh history",exact:true}).click();
 await expect(page.getByText(/Automatic updates stopped:/)).toHaveCount(0);
 await expect(page.getByRole("table",{name:"Saved runs"})).toBeVisible();
 const resumed=reads;await page.clock.fastForward(11000);await expect.poll(()=>reads).toBeGreaterThan(resumed);
 await page.getByRole("tab",{name:"Overview",exact:true}).click();
 const left=reads;await page.clock.fastForward(30000);expect(reads).toBe(left);
});
