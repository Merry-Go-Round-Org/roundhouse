import {test,expect,type Page} from "@playwright/test";
const admin={id:"admin1",username:"administrator",role:"admin",active:true};
const other={id:"u1",username:"reviewer",role:"validator",active:true};
async function setup(page:Page,role="admin"){
  const state={rows:[{...admin,role},structuredClone(other)],posts:[] as string[],createError:false,refreshError:false};
  await page.route("**/api/v1/**",route=>{
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith("/auth/me"))return route.fulfill({json:{...admin,role}});
    if(path.endsWith("/auth/users")&&route.request().method()==="GET")return route.fulfill(state.refreshError&&state.posts.length ? {status:503,json:{detail:"Read unavailable"}} : {json:state.rows});
    if(route.request().method()==="POST"){
      state.posts.push(path);
      if(path.endsWith("/disable")) {state.rows[1].active=false;return route.fulfill({json:state.rows[1]});}
      const body=route.request().postDataJSON();
      if(state.createError)return route.fulfill({status:503,json:{detail:"Do not display fixture-password-only"}});
      const created={id:"created1",username:body.username.toLowerCase(),role:body.role,active:true};state.rows.push(created);return route.fulfill({status:201,json:created});
    }
    return route.fulfill({json:[]});
  });
  await page.goto("/");return state;
}
test("admin confirms create and disable while self-disable stays blocked",async({page})=>{
  const state=await setup(page);await page.getByRole("button",{name:"Accounts",exact:true}).click(); await page.locator(".account-directory > summary").click();
  await expect(page.getByRole("button",{name:"Review disabling administrator",exact:true})).toBeDisabled();
  await page.getByLabel("New username",{exact:true}).fill("NewReviewer");
  await page.getByLabel("New account password",{exact:true}).fill("fixture-password-only");
  await page.getByLabel("New account role",{exact:true}).selectOption("approver");
  await expect(page.getByRole("button",{name:"Create account",exact:true})).toBeDisabled();
  await page.getByLabel("I confirm creating this account with the selected role.",{exact:true}).check();
  await page.getByRole("button",{name:"Create account",exact:true}).click();
  await expect(page.getByLabel("New account password",{exact:true})).toHaveValue("");
  await expect(page.getByRole("region",{name:"Account administration"})).toContainText("newreviewer: created");
  await expect(page.getByRole("region",{name:"Account administration"})).toContainText("Account directory updated.");
  await expect(page.locator(".account-list li").filter({hasText:"newreviewer"})).toHaveCount(1);
  await expect(page.getByLabel("New username",{exact:true})).toHaveValue("");
  await page.getByRole("button",{name:"Review disabling reviewer",exact:true}).click();
  await page.getByLabel("I confirm disabling this exact account.",{exact:true}).check();
  await page.getByRole("button",{name:"Disable account",exact:true}).click();
  await expect(page.getByRole("region",{name:"Account administration"})).toContainText("sessions revoked");
  await expect(page.getByRole("region",{name:"Account administration"})).toContainText("Account directory updated.");
  await expect(page.locator(".account-list li").filter({has:page.getByText("reviewer",{exact:true})})).toContainText("Disabled");
  await expect(page.getByLabel("New username",{exact:true})).toBeEnabled();
  expect(state.posts).toEqual(["/api/v1/auth/users","/api/v1/auth/users/u1/disable"]);
});
test("uncertain creation clears credentials and locks writes until reconciliation",async({page})=>{
  const state=await setup(page);state.createError=true;
  await page.getByRole("button",{name:"Accounts",exact:true}).click(); await page.locator(".account-directory > summary").click();
  await page.getByLabel("New username",{exact:true}).fill("new");
  await page.getByLabel("New account password",{exact:true}).fill("fixture-password-only");
  await page.getByLabel("I confirm creating this account with the selected role.",{exact:true}).check();
  await page.getByRole("button",{name:"Create account",exact:true}).click();
  await expect(page.getByRole("alert")).toContainText("No automatic retry");
  await expect(page.getByRole("alert")).not.toContainText("fixture-password-only");
  await expect(page.getByLabel("New account password",{exact:true})).toHaveValue("");
  await expect(page.getByRole("button",{name:"Create account",exact:true})).toBeDisabled();
  expect(state.posts).toHaveLength(1);
  await expect(page.locator(".account-list li")).toHaveCount(2);
  const stored=await page.evaluate(()=>JSON.stringify({local:{...localStorage},session:{...sessionStorage}}));expect(stored).not.toContain("fixture-password-only");
});

for(const mode of ["filtered","later page"]){
 test(`creation preserves the ${mode} directory view`,async({page})=>{
  const state=await setup(page);
  state.rows.push(...Array.from({length:23},(_,i)=>({id:`extra-${i}`,username:`person-${i}`,role:"developer",active:true})));
  await page.getByRole("button",{name:"Accounts",exact:true}).click();
  await page.locator(".account-directory > summary").click();
  if(mode==="filtered")await page.getByLabel("Search accounts",{exact:true}).fill("reviewer");
  else await page.getByRole("button",{name:"Next accounts",exact:true}).click();
  await page.getByLabel("New username",{exact:true}).fill("newuser");
  await page.getByLabel("New account password",{exact:true}).fill("fixture-password-only");
  await page.getByLabel("I confirm creating this account with the selected role.",{exact:true}).check();
  await page.getByRole("button",{name:"Create account",exact:true}).click();
  await expect(page.getByRole("region",{name:"Account administration"})).toContainText("Account newuser: created. Account directory updated.");
  await expect(page.getByText("Account directory (26)",{exact:true})).toBeVisible();
  await expect(page.getByLabel("New username",{exact:true})).toHaveValue("");
  if(mode==="filtered"){
   await expect(page.getByLabel("Search accounts",{exact:true})).toHaveValue("reviewer");
   await expect(page.locator(".account-list li")).toHaveCount(1);
   await expect(page.locator(".account-list li")).toContainText("reviewer");
   await page.getByLabel("Search accounts",{exact:true}).fill("newuser");
  }else{
   await expect(page.getByText("Page 2 of 3",{exact:true})).toBeVisible();
   await expect(page.locator(".account-list li")).toHaveCount(10);
   await page.getByRole("button",{name:"Next accounts",exact:true}).click();
   await expect(page.locator(".account-list li")).toHaveCount(6);
  }
  await expect(page.locator(".account-list li").filter({hasText:"newuser"})).toHaveCount(1);
  expect(state.posts).toEqual(["/api/v1/auth/users"]);
 });
}

for(const role of ["admin","developer","validator","approver"]){
 for(const width of [1280,390]){
  test(`live navigation excludes demo for ${role} at ${width}px`,async({page})=>{
   await page.setViewportSize({width,height:844});
   await setup(page,role);
   await expect(page.getByRole("button",{name:"Inventory",exact:true})).toBeVisible();
   await expect(page.locator('a[href*="/demo"]')).toHaveCount(0);
   await expect(page.getByRole("link",{name:/demo/i})).toHaveCount(0);
   await page.getByRole("button",{name:"Review queue",exact:true}).click();
   await expect(page.getByRole("heading",{name:"Review queue",exact:true})).toBeVisible();
   await expect(page.locator('a[href*="/demo"]')).toHaveCount(0);
  });
 }
}
test("saved creation with failed automatic refresh stays visible and never repeats the write",async({page})=>{
  const state=await setup(page);state.refreshError=true;
  await page.getByRole("button",{name:"Accounts",exact:true}).click();await page.locator(".account-directory > summary").click();
  await page.getByLabel("New username",{exact:true}).fill("newuser");
  await page.getByLabel("New account password",{exact:true}).fill("fixture-password-only");
  await page.getByLabel("I confirm creating this account with the selected role.",{exact:true}).check();
  await page.getByRole("button",{name:"Create account",exact:true}).click();
  await expect(page.getByRole("alert")).toContainText("The change was saved");
  await expect(page.locator(".account-list li").filter({hasText:"newuser"})).toHaveCount(1);
  await expect(page.getByLabel("New username",{exact:true})).toBeDisabled();
  await expect(page.getByLabel("New account password",{exact:true})).toHaveValue("");
  state.refreshError=false;
  await page.getByRole("button",{name:"Refresh accounts",exact:true}).click();
  await expect(page.getByLabel("New username",{exact:true})).toBeEnabled();
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(state.posts).toEqual(["/api/v1/auth/users"]);
});
test("disable updates the row immediately and waits for directory reconciliation before more edits",async({page})=>{
  const state=await setup(page);
  await page.getByRole("button",{name:"Accounts",exact:true}).click();await page.locator(".account-directory > summary").click();
  await page.getByRole("button",{name:"Review disabling reviewer",exact:true}).click();
  await page.getByLabel("I confirm disabling this exact account.",{exact:true}).check();
  let release!:()=>void;
  const gate=new Promise<void>(resolve=>{release=resolve;});
  await page.route("**/api/v1/auth/users",async route=>{
    if(!state.posts.length)return route.fallback();
    await gate;return route.fulfill({json:state.rows});
  });
  try{
    await page.getByRole("button",{name:"Disable account",exact:true}).click();
    await expect(page.locator(".account-list li").filter({has:page.getByText("reviewer",{exact:true})})).toContainText("Disabled");
    await expect(page.getByRole("region",{name:"Account administration"})).toContainText("Updating account directory");
    await expect(page.getByLabel("New username",{exact:true})).toBeDisabled();
    await expect(page.getByRole("button",{name:"Refresh accounts",exact:true})).toBeDisabled();
    await expect(page.getByRole("region",{name:"Disable account confirmation"})).toHaveCount(0);
  }finally{release();}
  await expect(page.getByLabel("New username",{exact:true})).toBeEnabled();
  await expect(page.getByRole("region",{name:"Account administration"})).toContainText("Account directory updated");
  expect(state.posts).toEqual(["/api/v1/auth/users/u1/disable"]);
});
test("non-admin has no accounts navigation",async({page})=>{
  await setup(page,"developer");await expect(page.getByRole("button",{name:"Inventory",exact:true})).toBeVisible();
  await expect(page.getByRole("button",{name:"Accounts",exact:true})).toHaveCount(0);
});

test("account refresh blocks editing and clears old disable confirmation",async({page})=>{
  const state=await setup(page);
  await page.getByRole("button",{name:"Accounts",exact:true}).click(); await page.locator(".account-directory > summary").click();
  await page.getByRole("button",{name:"Review disabling reviewer",exact:true}).click();
  await page.getByLabel("I confirm disabling this exact account.",{exact:true}).check();
  let release!:()=>void;
  const gate=new Promise<void>(resolve=>{release=resolve;});
  await page.route("**/api/v1/auth/users",async route=>{
    await gate;await route.fulfill({json:state.rows});
  },{times:1});
  try {
    await page.getByRole("button",{name:"Refresh accounts",exact:true}).click();
    await expect(page.getByLabel("New account password",{exact:true})).toBeDisabled();
    await expect(page.getByRole("region",{name:"Disable account confirmation"})).toHaveCount(0);
    expect(state.posts).toHaveLength(0);
  } finally {release();}
  await page.getByLabel("New account password",{exact:true}).fill("fixture-password-only");
  await expect(page.getByLabel("New account password",{exact:true})).toHaveValue("fixture-password-only");
  await page.getByRole("button",{name:"Review disabling reviewer",exact:true}).click();
  await expect(page.getByLabel("I confirm disabling this exact account.",{exact:true})).not.toBeChecked();
});

test("account directory stays collapsed and renders bounded searchable pages",async({page})=>{
 const state=await setup(page);
 state.rows.push(...Array.from({length:23},(_,i)=>({id:`extra-${i}`,username:`person-${i}`,role:"developer",active:true})));
 await page.getByRole("button",{name:"Accounts",exact:true}).click();
 await expect(page.getByText("Account directory (25)",{exact:true})).toBeVisible();
 await expect(page.locator(".account-list li")).toHaveCount(0);
 await page.locator(".account-directory > summary").click();
 await expect(page.locator(".account-list li")).toHaveCount(10);
 await page.getByRole("button",{name:"Next accounts",exact:true}).click();
 await expect(page.getByText("Page 2 of 3",{exact:true})).toBeVisible();
 await page.getByRole("button",{name:"Next accounts",exact:true}).click();
 await expect(page.locator(".account-list li")).toHaveCount(5);
 await expect(page.getByRole("button",{name:"Next accounts",exact:true})).toBeDisabled();
 await page.getByLabel("Search accounts",{exact:true}).fill("person-22");
 await expect(page.locator(".account-list li")).toHaveCount(1);
 await expect(page.getByText("Page 1 of 1",{exact:true})).toBeVisible();
 await page.locator(".account-directory > summary").click();
 await expect(page.locator(".account-list li")).toHaveCount(0);
 expect(state.posts).toEqual([]);
});
