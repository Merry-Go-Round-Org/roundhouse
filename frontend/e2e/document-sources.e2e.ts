import { test, expect, type Page } from "@playwright/test";
const model = {id:"m1",name:"Credit",version:"v1",language:"R",dataset_id:"uci_credit",purpose:"Credit",owner:null,lifecycle_status:"development",created_at:"2026-11-07T10:00:00Z"};
const report = {id:"doc1",model_id:"m1",run_id:"r1",kind:"documentation",artifact_id:"a1",model_version:"v1",artifact_version:"1",artifact_sha256:"b".repeat(64),metrics:[]};
async function setup(page: Page) {
  const state = { hash:"a".repeat(64), version:"1", denied:false, fileReads:0, methods:[] as string[] };
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname; state.methods.push(route.request().method());
    if (path.endsWith("/auth/me")) return route.fulfill({json:{id:"actor",role:"developer"}});
    if (path.endsWith("/models/m1")) return route.fulfill({json:model});
    if (path.endsWith("/reports")) return route.fulfill({json:[{...report,artifact_version:state.version}]});
    if (path.endsWith("/files")) return route.fulfill({json:[{path:"documentation/notes.md",sha256:state.hash,download_url:"https://must-not-follow.invalid/notes.md"}]});
    if (path.endsWith("/files/documentation/notes.md")) {
      state.fileReads++;
      return route.fulfill(state.denied ? {status:403,json:{detail:"Source access denied"}} : {contentType:"text/plain",body:"# Legacy notes\nOld description <script>doNotRun()</script>"});
    }
    if (path.endsWith("/download")) return route.fulfill({contentType:"text/markdown",body:"# Generated document\n[Notes](/inputs/model/documentation/notes.md#L4) [Relative notes](./documentation/notes.md) [Absent](absent.py)\nRevised description"});
    return route.fulfill({json:[]});
  });
  await page.goto("/?model=m1"); await page.getByRole("tab",{name:"Documentation",exact:true}).click();
  return state;
}
test("compares retained source and generated version with provenance and no writes", async ({page}) => {
  const state = await setup(page);
  await page.getByRole("button",{name:"Browse uploaded sources and reports"}).click();
  await page.getByLabel("First document",{exact:true}).selectOption("file:documentation/notes.md");
  await page.getByLabel("Second document (optional)",{exact:true}).selectOption("report:doc1");
  await page.getByRole("button",{name:"Read selected documents"}).click();
  await expect(page.getByRole("region",{name:"Comparison document 1",exact:true})).toContainText("Old description");
  await expect(page.getByRole("region",{name:"Comparison document 2",exact:true})).toContainText("Revised description");
  await expect(page.getByRole("region",{name:"Comparison document 1",exact:true})).toContainText("report-version association is not supplied");
  await expect(page.locator(".document-comparison script")).toHaveCount(0);
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(state.methods.every(method => method === "GET")).toBe(true);
});
test("changed source hashes and report versions block stale comparison", async ({page}) => {
  const state = await setup(page);
  await page.getByRole("button",{name:"Browse uploaded sources and reports"}).click();
  await page.getByLabel("First document",{exact:true}).selectOption("file:documentation/notes.md");
  state.hash = "c".repeat(64);
  await page.getByRole("button",{name:"Read selected documents"}).click();
  await expect(page.getByRole("alert")).toContainText("changed"); expect(state.fileReads).toBe(0);
  await page.getByLabel("First document",{exact:true}).selectOption("report:doc1"); state.version = "2";
  await page.getByRole("button",{name:"Read selected documents"}).click();
  await expect(page.getByRole("alert")).toContainText("Report identity/version changed");
  await expect(page.locator(".document-comparison")).toHaveCount(0);
});
test("citations use the allowlist and surface denied access without following supplied URLs", async ({page}) => {
  const state = await setup(page);
  await page.getByRole("button",{name:"View available documents and reports"}).click();
  await page.getByRole("button",{name:"Read documentation",exact:true}).click();
  await page.getByRole("button",{name:"Notes (open source)",exact:true}).click();
  await expect(page.locator(".source-citation-preview")).toContainText("Old description");
  await page.getByRole("button",{name:"Close source preview",exact:true}).click();
  await page.getByRole("button",{name:"Relative notes (open source)",exact:true}).click();
  await expect(page.locator(".source-citation-preview")).toContainText("Old description");
  await page.getByRole("button",{name:"Close source preview",exact:true}).click();
  await page.getByRole("button",{name:"Absent (open source)",exact:true}).click();
  await expect(page.getByRole("alert")).toContainText("not in the authorized");
  expect(state.fileReads).toBe(2);
  state.denied = true;
  await page.getByRole("button",{name:"Notes (open source)",exact:true}).click();
  await expect(page.locator(".source-citation").filter({has:page.getByRole("button",{name:"Notes (open source)",exact:true})})).toContainText("Source access denied");
  await expect(page.locator(".source-citation-preview")).toHaveCount(0);
});
