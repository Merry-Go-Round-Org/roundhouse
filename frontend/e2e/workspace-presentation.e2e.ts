import {test,expect} from "@playwright/test";
test("empty inventory gives role-appropriate guidance and styled accounts fit mobile",async({page})=>{
 let role="admin";
 const writes:string[]=[];
 await page.route("**/api/v1/**",route=>{
  const path=new URL(route.request().url()).pathname;
  if(route.request().method()==="POST")writes.push(path);
  if(path.endsWith("/auth/me"))return route.fulfill({json:{id:"u1",username:"operator",role,active:true}});
  if(path.endsWith("/auth/users"))return route.fulfill({json:[{id:"u1",username:"operator",role,active:true},{id:"u2",username:"reviewer",role:"approver",active:true}]});
  return route.fulfill({json:[]});
 });
 await page.setViewportSize({width:1440,height:1000});await page.goto("/");
 await expect(page.getByRole("heading",{name:"No models available yet."})).toBeVisible();
 await expect(page.getByRole("button",{name:"Upload your first model"})).toBeVisible();
 await expect(page.getByRole("heading",{name:"No models match your filters."})).toHaveCount(0);
 await page.screenshot({path:"test-results/workspace-desktop.png",fullPage:true});
 await page.getByRole("button",{name:"Accounts",exact:true}).click();
 await expect(page.getByLabel("New username",{exact:true})).toBeEnabled();
 await page.screenshot({path:"test-results/accounts-desktop.png",fullPage:true});
 await page.setViewportSize({width:390,height:844});
 await page.getByLabel("New username",{exact:true}).scrollIntoViewIfNeeded();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
 await page.getByRole("region",{name:"Account administration"}).screenshot({path:"test-results/accounts-mobile.png"});
 role="validator";await page.goto("/");
 await expect(page.getByRole("heading",{name:"No models available yet."})).toBeVisible();
 await expect(page.getByText("Ask a model developer to upload a model, then refresh this page.")).toBeVisible();
 await expect(page.getByRole("button",{name:"Upload your first model"})).toHaveCount(0);
 expect(writes).toEqual([]);
});
