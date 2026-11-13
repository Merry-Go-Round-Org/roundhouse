import { test, expect, type Page } from "@playwright/test";
const token = "a".repeat(64);
const initialModels = ["one", "two"].map(id => ({ id, name: `Model ${id}`, version: "1", language: "R", dataset_id: "uci_credit", purpose: "Example", owner: null, lifecycle_status: "development", created_at: "2026-11-12T12:00:00Z" }));
const initialDatasets = ["uci_credit", "ulb_fraud"].map((id, i) => ({ id, name: `Dataset ${i}`, filename: "data.csv", target_description: "Outcome", limitations: [], version: "v1", version_id: `version-${i}`, verified_upload: true,
  sha256: token, split_sha256: token, statistics: { row_count: 10, positive_rate: 0.5,
    partition_counts: { train: 6, validation: 2, test: 2 }, positive_rates: { train: 0.5, validation: 0.5, test: 0.5 },
    partition_sha256: { train: token, validation: token, test: token } } }));
async function setup(page: Page, options: { role?: string; blocked?: boolean; uncertain?: boolean; missingReceipt?: boolean; stale?: boolean; cleanup?: boolean; denied?: boolean } = {}) {
  const state = { models: structuredClone(initialModels), datasets: structuredClone(initialDatasets), writes: [] as { path: string; body: Record<string, unknown> }[], receipts: new Map<string, unknown>() };
  await page.route("**/api/v1/**", route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { id: "operator", role: options.role ?? "admin" } });
    if (path.endsWith("/models")) return route.fulfill({ json: state.models });
    if (path.endsWith("/datasets")) return route.fulfill({ json: state.datasets });
    if (path.includes("/deletions")) {
      const kind = path.includes("/models/") ? "models" : "datasets";
      if (request.method() === "GET") {
        const receipt = state.receipts.get(path.split("/").at(-1)!);
        return receipt ? route.fulfill({ json: receipt }) : route.fulfill({ status: 404, json: { detail: "Deletion receipt not found" } });
      }
      const body = request.postDataJSON();
      const ids = body[kind === "models" ? "model_ids" : "dataset_version_ids"] as string[];
      if (path.endsWith("/preview") && options.denied) return route.fulfill({ status: 403, json: { detail: "Only the uploading developer or an admin may delete this model" } });
      if (path.endsWith("/preview")) return route.fulfill({ json: {
        ...body, [kind]: ids.map(id => kind === "models" ? { id, name: `Model ${id}`, version: "1", related_counts: { runs: options.blocked ? 1 : 0 }, blockers: options.blocked ? ["runs: 1"] : [] } :
          { version_id: id, id: "uci_credit", name: `Dataset ${id}`, version: "v1", blockers: options.blocked ? { registered_models: ["one"] } : {} }),
        preview_token: token, atomic: true, max_selection: 20, can_delete: !options.blocked,
        effect: "Remove the selected records from the inventory. Evidence and audit history are kept.",
      } });
      state.writes.push({ path, body });
      if (options.stale) return route.fulfill({ status: 409, json: { detail: { code: "model_delete_blocked", models: [{ id: "one", blockers: ["new evidence: 1"] }] } } });
      if (options.missingReceipt && state.writes.length === 1) return route.abort();
      const result = { request_id: body.request_id, status: "deleted", deleted: ids, blocked: [], failed: [], retained: ["audit_history"], ...(options.cleanup ? { cleanup_pending: ["uploads/example"] } : {}) };
      state.receipts.set(body.request_id, result);
      if (kind === "models") state.models = state.models.filter(m => !ids.includes(m.id));
      else state.datasets = state.datasets.filter(d => !ids.includes(d.version_id));
      if (options.uncertain) return route.abort();
      return route.fulfill({ json: result });
    }
    return route.fulfill({ json: state.models.find(m => path.endsWith(`/models/${m.id}`)) ?? [] });
  });
  await page.goto("/"); return state;
}
async function selectAndPreview(page: Page, kind: "models" | "datasets", count = 2) {
  if (kind === "datasets") await page.getByRole("button", { name: "Datasets", exact: true }).click();
  if (count === 2) await page.getByRole("checkbox", { name: `Select visible ${kind}`, exact: true }).check();
  else await page.getByRole("checkbox", { name: kind === "models" ? "Select Model one (one), version 1" : "Select dataset Dataset 0 (version-0)", exact: true }).check();
  await page.getByRole("button", { name: kind === "models" ? "Delete selected" : "Delete selected datasets", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("Remove the selected records");
}
async function confirm(page: Page, kind: string, count: number) {
  await page.getByRole("checkbox", { name: "I understand the listed records will be removed from the inventory. Evidence and audit history are kept.", exact: true }).check();
  await page.getByRole("button", { name: `Delete ${count} ${kind}`, exact: true }).click();
}
for (const kind of ["models", "datasets"] as const) for (const count of [1, 2]) {
  test(`${kind}: confirms exact selection and refreshes after deleting ${count}`, async ({ page }) => {
    const state = await setup(page); await selectAndPreview(page, kind, count);
    await expect(page.getByRole("button", { name: `Delete ${count} ${kind}`, exact: true })).toBeDisabled();
    expect(state.writes).toHaveLength(0);
    await confirm(page, kind, count);
    await expect(page.getByRole("dialog")).toContainText(`${count} records deleted`);
    expect(state.writes).toHaveLength(1);
    expect(state.writes[0].body[kind === "models" ? "model_ids" : "dataset_version_ids"]).toEqual(kind === "models" ? ["one", "two"].slice(0, count) : ["version-0", "version-1"].slice(0, count));
    await page.getByRole("dialog").getByRole("button", { name: "Close", exact: true }).click();
    await expect(page.getByRole("button", { name: kind === "models" ? "Model one" : "View Dataset 0", exact: true })).toHaveCount(0);
  });
}
for (const kind of ["models", "datasets"] as const) test(`${kind}: dependency blockers prevent the whole batch`, async ({ page }) => {
  const state = await setup(page, { blocked: true }); await selectAndPreview(page, kind);
  await expect(page.getByRole("dialog")).toContainText(kind === "models" ? "runs: 1" : "registered models: one");
  await expect(page.getByRole("button", { name: `Delete 2 ${kind}` })).toBeDisabled();
  await page.keyboard.press("Escape"); await expect(page.getByRole("dialog")).not.toBeVisible(); expect(state.writes).toHaveLength(0);
});
test("lost response recovers via receipt after reload without a second write", async ({ page }) => {
  const state = await setup(page, { uncertain: true, cleanup: true }); await selectAndPreview(page, "models"); await confirm(page, "models", 2);
  await expect(page.getByRole("dialog")).toContainText("Deletion outcome is unknown");
  await page.reload(); await page.getByRole("button", { name: "Recover deletion" }).click();
  await page.getByRole("button", { name: "Check deletion receipt" }).click();
  await expect(page.getByRole("dialog")).toContainText("2 records deleted");
  await expect(page.getByRole("dialog")).toContainText("managed file cleanup is pending");
  expect(state.writes).toHaveLength(1);
});
test("missing receipt permits only an explicitly confirmed identical retry", async ({ page }) => {
  const state = await setup(page, { missingReceipt: true }); await selectAndPreview(page, "models", 1); await confirm(page, "models", 1);
  await page.getByRole("button", { name: "Check deletion receipt" }).click();
  await expect(page.getByRole("dialog")).toContainText("does not prove the first request failed");
  await page.getByRole("checkbox", { name: "I confirm retrying this same deletion request." }).check();
  await page.getByRole("button", { name: "Retry same deletion request" }).click();
  await expect(page.getByRole("dialog")).toContainText("1 records deleted");
  expect(state.writes).toHaveLength(2); expect(state.writes[0]).toEqual(state.writes[1]);
});
test("stale selection shows blockers and requires a fresh selection", async ({ page }) => {
  const state = await setup(page, { stale: true }); await selectAndPreview(page, "models"); await confirm(page, "models", 2);
  await expect(page.getByRole("dialog")).toContainText("new evidence: 1");
  await expect(page.getByRole("button", { name: "Delete 2 models" })).toHaveCount(0);
  expect(state.models).toHaveLength(2); expect(state.writes).toHaveLength(1);
});
test("validator can select but cannot request deletion", async ({ page }) => {
  const state = await setup(page, { role: "validator" });
  await page.getByRole("checkbox", { name: "Select visible models", exact: true }).check();
  await expect(page.getByRole("button", { name: "Delete selected", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Datasets", exact: true }).click();
  await page.getByRole("checkbox", { name: "Select visible datasets", exact: true }).check();
  await expect(page.getByRole("button", { name: "Delete selected datasets", exact: true })).toBeDisabled(); expect(state.writes).toHaveLength(0);
});

test("server ownership denial cannot be bypassed by a developer", async ({ page }) => {
  const state = await setup(page, { role: "developer", denied: true });
  await page.getByRole("checkbox", { name: "Select visible models", exact: true }).check();
  await page.getByRole("button", { name: "Delete selected", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("Only the uploading developer");
  await expect(page.getByRole("button", { name: "Delete 2 models" })).toHaveCount(0); expect(state.writes).toHaveLength(0);
});

test("deleting a model clears the retained floating chat context", async ({ page }) => {
  await setup(page);
  await page.getByRole("button", { name: "Model one", exact: true }).click();
  await page.getByRole("button", { name: "Open agent chat", exact: true }).click();
  await expect(page.getByText("Context: Model one", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Minimize chat", exact: true }).click();
  await page.getByRole("button", { name: "Back to inventory", exact: true }).click();
  await selectAndPreview(page, "models", 1); await confirm(page, "models", 1);
  await expect(page.getByRole("dialog")).toContainText("1 records deleted");
  await page.getByRole("dialog").getByRole("button", { name: "Close", exact: true }).click();
  await page.getByRole("button", { name: "Open agent chat", exact: true }).click();
  await expect(page.getByText("Context: General guidance", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Chat about Model one", exact: true })).toHaveCount(0);
});

test("mobile dataset filters clear selection and confirmation can be cancelled", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await setup(page);
  await page.getByRole("button", { name: "Datasets", exact: true }).click();
  await expect(page.getByRole("button", { name: "Delete selected datasets", exact: true })).toBeDisabled();
  await page.getByRole("checkbox", { name: "Select visible datasets", exact: true }).check();
  await page.getByLabel("Search datasets", { exact: true }).fill("Dataset 0");
  await expect(page.getByRole("region", { name: "Dataset selection", exact: true })).toContainText("0 datasets selected");
  await page.getByRole("checkbox", { name: "Select visible datasets", exact: true }).check();
  await page.getByRole("button", { name: "Review selection", exact: true }).click();
  await expect(page.getByRole("region", { name: "Selected datasets", exact: true })).toContainText("version-0");
  await page.getByRole("button", { name: "Delete selected datasets", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("Dataset version-0");
  const dialogBox = await page.getByRole("dialog").boundingBox(); expect(dialogBox!.width).toBeLessThanOrEqual(390);
  await page.keyboard.press("Escape"); await expect(page.getByRole("dialog")).not.toBeVisible();
  expect(state.writes).toHaveLength(0);
});
