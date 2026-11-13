import { test, expect } from "@playwright/test";
const model = {
  id: "m1",
  name: "Credit",
  version: "1.0.0",
  language: "R",
  dataset_id: "uci_credit",
  purpose: "Default",
  owner: null,
  lifecycle_status: "development",
  created_at: "2026-11-05T12:00:00Z",
};
test("Markdown export uses the report endpoint and keeps original artifact download separate", async ({ page }) => {
  const reads: string[] = [];
  let denied = false;
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { id: "user", role: "admin" } });
    if (path.endsWith("/models/m1")) return route.fulfill({ json: model });
    if (path.endsWith("/reports")) return route.fulfill({ json: [{ id: "report1", model_id: "m1", run_id: "r1", kind: "validation", artifact_id: "artifact1", metrics: [] }] });
    if (path.endsWith("/markdown")) {
      reads.push(path);
      return route.fulfill(denied ? { status: 403, json: { detail: "Report access denied" } } : { contentType: "text/markdown", body: "# Validation report\n\n## Human decision\nDecision: accepted\nCondition: Review limitations" });
    }
    if (path.endsWith("/download")) { reads.push(path); return route.fulfill({ json: { checks: [] } }); }
    return route.fulfill({ json: [] });
  });
  await page.goto("/?model=m1");
  await expect(page.getByRole("link", { name: "View labelled demo" })).toHaveCount(0);
  await page.getByRole("tab", { name: "Documentation", exact: true }).click();
  const card = page.locator(".model-documents .lifecycle-card").filter({ hasText: "Validation report 1" });
  const exported = page.waitForEvent("download");
  await card.getByRole("button", { name: "Download report as Markdown" }).click();
  const download = await exported;
  expect(download.suggestedFilename()).toBe("report-report1.md");
  const stream = await download.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
  expect(Buffer.concat(chunks).toString()).toContain("Condition: Review limitations");
  const original = page.waitForEvent("download");
  await card.getByRole("button", { name: "Download validation artifact" }).click();
  await original;
  expect(reads).toEqual(["/api/v1/reports/report1/markdown", "/api/v1/artifacts/artifact1/download"]);
  denied = true;
  await card.getByRole("button", { name: "Download report as Markdown" }).click();
  await expect(page.getByRole("alert")).toContainText("Report access denied");
  await expect(card.getByRole("button", { name: "Download report as Markdown" })).toBeEnabled();
});
test("document preview renders tables and sections without loading active content or images", async ({
  page,
}) => {
  let imageRequests = 0;
  page.on("request", (request) => {
    if (request.url().includes("remote-image")) imageRequests++;
  });
  await page.route("**/api/v1/**", (route) => {
    if (new URL(route.request().url()).pathname === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "mock-user", username: "fixture-user", role: "admin", active: true } });
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/models") return route.fulfill({ json: [model] });
    if (path === "/api/v1/models/m1") return route.fulfill({ json: model });
    if (path.endsWith("/reports"))
      return route.fulfill({
        json: [
          {
            id: "doc1",
            model_id: "m1",
            run_id: "r1",
            kind: "documentation",
            artifact_id: "a1",
            metrics: [],
          },
        ],
      });
    if (path.endsWith("/download"))
      return route.fulfill({
        contentType: "text/markdown",
        body: [
          "# Model document",
          "",
          "## 1. Model overview",
          "",
          "A **reviewable** draft.",
          "",
          "### Missing information",
          "",
          "| Item | Reason |",
          "| --- | --- |",
          "| Owner | Not supplied |",
          "",
          "### Conflicts with the old documentation",
          "",
          "Old notes disagree.",
          "",
          "[Manifest](manifest.json) [Website](https://example.org/evidence) [Unsafe](javascript:alert%281%29)",
          "",
          "![external](https://example.org/remote-image.png)",
          "",
          "<script>window.injected = true</script>",
        ].join("\n"),
      });
    return route.fulfill({ status: 501, json: {} });
  });
  await page.goto("/?model=m1");
  await page.getByRole("tab", { name: "Documentation", exact: true }).click();
  await page
    .getByRole("button", { name: "View available documents and reports" })
    .click();
  await page
    .getByRole("button", { name: "Read documentation", exact: true })
    .click();
  const reader = page.getByRole("region", {
    name: "Document reader",
    exact: true,
  });
  await expect(
    reader.getByRole("cell", { name: "Owner", exact: true }),
  ).toBeVisible();
  await reader
    .getByRole("button", { name: "Missing information", exact: true })
    .click();
  await expect(
    reader.getByRole("heading", { name: "Missing information", exact: true }),
  ).toBeFocused();
  await expect(
    reader.getByRole("link", { name: "Website", exact: true }),
  ).toHaveAttribute("rel", "noopener noreferrer");
  await expect(
    reader.getByRole("link", { name: "Manifest", exact: true }),
  ).toHaveCount(0);
  await expect(
    reader.getByRole("link", { name: "Unsafe", exact: true }),
  ).toHaveCount(0);
  await expect(reader.locator("img,script,iframe")).toHaveCount(0);
  expect(imageRequests).toBe(0);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("tab", { name: "Overview", exact: true }).click();
  await expect(reader).toHaveCount(0);
});
test("unsupported document formats stay downloadable without rendering", async ({
  page,
}) => {
  await page.route("**/api/v1/**", (route) => {
    if (new URL(route.request().url()).pathname === "/api/v1/auth/me")
      return route.fulfill({ json: { id: "mock-user", username: "fixture-user", role: "admin", active: true } });
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/models")) return route.fulfill({ json: [model] });
    if (path.endsWith("/models/m1")) return route.fulfill({ json: model });
    if (path.endsWith("/reports"))
      return route.fulfill({
        json: [
          {
            id: "doc1",
            model_id: "m1",
            run_id: "r1",
            kind: "documentation",
            artifact_id: "a1",
            metrics: [],
          },
        ],
      });
    return route.fulfill({ contentType: "application/pdf", body: "%PDF" });
  });
  await page.goto("/?model=m1");
  await page.getByRole("tab", { name: "Documentation", exact: true }).click();
  await page
    .getByRole("button", { name: "View available documents and reports" })
    .click();
  await page
    .getByRole("button", { name: "Read documentation", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "not a supported Markdown/text document",
  );
  await expect(
    page.getByRole("region", { name: "Document reader", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Download documentation artifact" }),
  ).toBeEnabled();
});

test("generated document cards use readable titles and hide identifiers until requested",async({page})=>{
 await page.route("**/api/v1/**",route=>{
  const path=new URL(route.request().url()).pathname;
  if(path.endsWith("/auth/me"))return route.fulfill({json:{id:"operator",role:"developer"}});
  if(path.endsWith("/models/m1"))return route.fulfill({json:model});
  if(path.endsWith("/models"))return route.fulfill({json:[model]});
  if(path.endsWith("/reports"))return route.fulfill({json:[{id:"doc1",model_id:"m1",run_id:"r1",kind:"documentation",artifact_id:"a1",artifact_sha256:"a".repeat(64),metrics:[]}]});
  return route.fulfill({json:[]});
 });
 await page.goto("/?model=m1");await page.getByRole("tab",{name:"Documentation",exact:true}).click();
 await page.getByRole("button",{name:"View available documents and reports"}).click();
 await expect(page.getByRole("heading",{name:"Agent-generated documentation 1",exact:true})).toBeVisible();
 await expect(page.getByText(/SHA-256: a{64}/)).toBeHidden();
 await page.getByText("Report identifiers and checksums",{exact:true}).click();
 await expect(page.getByText(/SHA-256: a{64}/)).toBeVisible();
});
