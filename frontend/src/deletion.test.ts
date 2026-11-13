import { describe, expect, it } from "vitest";
import { readDeletionPreview, readDeletionReceipt, selectionIds, createDeletionClient, type DeletionRequest } from "./deletion";
const token = "a".repeat(64);
const preview = { model_ids: ["one"], models: [{ id: "one", name: "Model one", version: "1", related_counts: { runs: 0 }, blockers: [] }], preview_token: token, can_delete: true, effect: "Permanent deletion", atomic: true, max_selection: 20 };
const pending: DeletionRequest = { kind: "models", ids: ["one"], token, requestId: "5d6bf99d-0ebd-4257-b897-34b836133e45" };
describe("deletion contract", () => {
  it("rejects empty, duplicate and oversized selections", () => {
    for (const ids of [[], ["one", "one"], [" "], Array.from({ length: 21 }, (_, i) => String(i))]) expect(() => selectionIds(ids)).toThrow();
  });
  it("binds preview identities and requires complete atomic eligibility", () => {
    expect(readDeletionPreview(preview, "models", ["one"]).canDelete).toBe(true);
    for (const change of [{ model_ids: ["other"] }, { atomic: false }, { models: [] }, { preview_token: "x" }, { models: [{ ...preview.models[0], blockers: ["runs: 1"] }] }]) {
      expect(() => readDeletionPreview({ ...preview, ...change }, "models", ["one"])).toThrow();
    }
    // Soft deletion keeps evidence, so retained record counts do not block on their own.
    const retained = readDeletionPreview({ ...preview, models: [{ ...preview.models[0], related_counts: { runs: 1 } }] }, "models", ["one"]);
    expect(retained).toMatchObject({ canDelete: true, items: [{ counts: { runs: 1 }, blockers: [] }] });
  });
  it("keeps dataset version identity distinct from the logical dataset ID", () => {
    const value = { ...preview, dataset_version_ids: ["version-1"], datasets: [{ version_id: "version-1", id: "uci_credit", name: null, version: "v1", blockers: { registered_models: ["one"] } }], can_delete: false };
    expect(readDeletionPreview(value, "datasets", ["version-1"]).items[0]).toMatchObject({ id: "version-1", name: "uci_credit", blockers: ["registered models: one"] });
    expect(() => readDeletionPreview(value, "datasets", ["uci_credit"])).toThrow();
  });
  it("never calls partial or foreign receipts success and preserves pending cleanup", () => {
    const receipt = { request_id: pending.requestId, status: "deleted", deleted: ["one"], blocked: [], failed: [], retained: ["audit_history"], cleanup_pending: ["uploads/one"] };
    expect(readDeletionReceipt(receipt, pending).cleanup_pending).toEqual(["uploads/one"]);
    for (const change of [{ request_id: "another" }, { deleted: [] }, { deleted: ["other"] }, { failed: ["one"] }, { blocked: ["one"] }]) expect(() => readDeletionReceipt({ ...receipt, ...change }, pending)).toThrow();
  });
  it("uses exact IDs/token/request ID for explicit idempotent retries", async () => {
    const bodies: unknown[] = [];
    const client = createDeletionClient(async (_path, init) => {
      bodies.push(JSON.parse(String(init?.body)));
      return { request_id: pending.requestId, status: "deleted", deleted: ["one"], blocked: [], failed: [], retained: ["audit_history"] };
    });
    await client.remove(pending); await client.remove(pending);
    expect(bodies).toEqual(Array(2).fill({ model_ids: ["one"], preview_token: token, request_id: pending.requestId }));
  });
});
