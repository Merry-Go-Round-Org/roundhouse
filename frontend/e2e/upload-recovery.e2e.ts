import { test, expect } from "@playwright/test";
const upload = { id: "u1", status: "running", checks: [], files: [], model_id: null, error: null };
test("saved uploads recover across reload and refresh without writes", async ({ page }) => {
  const methods: string[] = [];
  let completed = false;
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url()); methods.push(route.request().method());
    if (url.pathname.endsWith("/auth/me")) return route.fulfill({json: {id:"actor1", role:"developer"}});
    if (url.pathname.endsWith("/models/uploads")) return route.fulfill({json:
      url.searchParams.get("offset") === "0" ? Array.from({length:20}, (_, i) => ({...upload, id:`u${i+1}`})) : []});
    if (url.pathname.endsWith("/models/uploads/u1")) return route.fulfill({json: completed ? {...upload, status:"completed", model_id:"m1", checks:[{name:"manifest", status:"passed", blocking:true, reason:"Verified"}]} : upload});
    return route.fulfill({json: []});
  });
  await page.goto("/");
  await page.getByRole("button", {name:"View saved uploads",exact:true}).click();
  await page.getByRole("button", {name:"Inspect upload u1",exact:true}).click();
  await expect(page.getByRole("region", {name:"Selected upload",exact:true})).toContainText("Status: running");
  await page.reload();
  await page.getByRole("button", {name:"View saved uploads",exact:true}).click();
  await page.getByRole("button", {name:"Inspect upload u1",exact:true}).click();
  completed = true;
  await page.getByRole("button", {name:"Refresh upload status",exact:true}).click();
  await expect(page.getByRole("button", {name:"Open recovered model"})).toBeVisible();
  await page.getByRole("button", {name:"Next uploads"}).click();
  await expect(page.getByText("No uploads returned on this page.", {exact:false})).toBeVisible();
  await expect(page.getByRole("region", {name:"Selected upload",exact:true})).toHaveCount(0);
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(methods.every(method => method === "GET")).toBe(true);
});
test("failed refresh hides stale evidence and wrong identity cannot open a model", async ({page}) => {
  let mode = "valid";
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/auth/me")) return route.fulfill({json:{id:"actor1",role:"developer"}});
    if (url.pathname.endsWith("/models/uploads")) return route.fulfill(mode === "error" ? {status:503,json:{detail:"Upload history unavailable"}} : {json:[upload]});
    if (url.pathname.endsWith("/models/uploads/u1")) return route.fulfill({json:{...upload,id:"wrong",files:["Must not display"]}});
    return route.fulfill({json:[]});
  });
  await page.goto("/");
  await page.getByRole("button", {name:"View saved uploads",exact:true}).click();
  await page.getByRole("button", {name:"Inspect upload u1",exact:true}).click();
  await expect(page.getByRole("alert")).toContainText("another upload");
  await expect(page.getByText("Must not display")).toHaveCount(0);
  mode = "error";
  await page.getByRole("button", {name:"Refresh saved uploads"}).click();
  await expect(page.getByRole("alert")).toContainText("Upload history unavailable");
  await expect(page.getByRole("button", {name:"Inspect upload u1",exact:true})).toHaveCount(0);
  mode = "valid";
  await page.getByRole("button", {name:"Refresh saved uploads"}).click();
  await expect(page.getByRole("button", {name:"Inspect upload u1",exact:true})).toBeVisible();
});

test("saved missing-file checks remain visible after reload", async ({ page }) => {
  const failed = { ...upload, status: "failed", error: "Missing required file: schema.json", checks: [{ name: "schema", status: "failed", blocking: true, reason: "Missing required file: schema.json" }] };
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { id: "actor1", role: "developer" } });
    if (path.endsWith("/models/uploads/u1")) return route.fulfill({ json: failed });
    if (path.endsWith("/models/uploads")) return route.fulfill({ json: [failed] });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  for (let attempt = 0; attempt < 2; attempt++) {
    await page.getByRole("button", { name: "View saved uploads", exact: true }).click();
    await page.getByRole("button", { name: "Inspect upload u1", exact: true }).click();
    await expect(page.getByRole("region", { name: "Upload problems", exact: true })).toContainText("Missing required file: schema.json");
    await expect(page.getByRole("button", { name: "Open recovered model" })).toHaveCount(0);
    if (!attempt) await page.reload();
  }
});
