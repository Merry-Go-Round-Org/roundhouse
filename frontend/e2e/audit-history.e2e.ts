import {test,expect} from "@playwright/test";
const model={id:"m1",name:"Credit",version:"v1",language:"R",dataset_id:"uci_credit",purpose:"Credit",owner:null,lifecycle_status:"development",created_at:"2026-11-07T10:00:00Z"};
test("public model event history paginates, filters and recovers from read errors without writes",async({page})=>{
  let mode="normal";const methods:string[]=[];
  await page.route("**/api/v1/**",route=>{
    const url=new URL(route.request().url());methods.push(route.request().method());
    if(url.pathname.endsWith("/auth/me"))return route.fulfill({json:{id:"viewer",role:"developer"}});
    if(url.pathname.endsWith("/models/m1"))return route.fulfill({json:model});
    if(url.pathname.endsWith("/history"))return route.fulfill(mode==="error"?{status:503,json:{detail:"Events unavailable"}}:{json:url.searchParams.get("offset")==="0"?Array.from({length:20},(_,i)=>({id:`e${i}`,resource_id:"r1",action:i===0?"human_review_decision":"review_opened",created_at:"2026-11-07T10:00:00Z",payload:{decision:"accepted",reasoning:"DO_NOT_SHOW",prompt:"PRIVATE_PROMPT"}})):[]});
    return route.fulfill({json:[]});
  });
  await page.goto("/?model=m1");await page.getByRole("tab",{name:"History",exact:true}).click();
  await page.getByRole("button",{name:"View model events",exact:true}).click();
  const timeline=page.getByRole("region",{name:"Model audit events"});
  await expect(timeline.getByRole("heading",{name:"human_review_decision",exact:true})).toBeVisible();
  await timeline.getByLabel("Filter this event page",{exact:true}).selectOption("human_review_decision");
  await expect(timeline.getByRole("listitem")).toHaveCount(1);
  await timeline.getByText("Public event details",{exact:true}).click();
  await expect(timeline).toContainText("accepted");
  await expect(timeline).not.toContainText("DO_NOT_SHOW");await expect(timeline).not.toContainText("PRIVATE_PROMPT");
  await page.setViewportSize({width:390,height:844});expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await timeline.getByRole("button",{name:"Next events"}).click();await expect(timeline).toContainText("No events returned");
  await expect(timeline.getByRole("button",{name:"Next events"})).toBeDisabled();
  await timeline.getByRole("button",{name:"Previous events"}).click();
  mode="error";await timeline.getByRole("button",{name:"Refresh model events"}).click();
  await expect(timeline.getByRole("alert")).toContainText("Events unavailable");await expect(timeline.getByRole("listitem")).toHaveCount(0);
  mode="normal";await timeline.getByRole("button",{name:"Refresh model events"}).click();await expect(timeline.getByRole("listitem")).toHaveCount(20);
  expect(methods.every(m=>m==="GET")).toBe(true);
});
