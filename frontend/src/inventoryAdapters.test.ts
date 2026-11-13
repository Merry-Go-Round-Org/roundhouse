import { describe, expect, it } from "vitest";
import { demoModels } from "./demo";
import { demoInventoryRow, liveInventoryRow } from "./inventoryAdapters";
import type { ModelRecord } from "./api";

const credit: ModelRecord = {
  id: "real-credit-2026",
  name: "Uploaded credit GLM",
  version: "1.0.0",
  language: "R",
  dataset_id: "uci_credit",
  purpose: "Default probability",
  owner: null,
  lifecycle_status: "active",
  created_at: "2026-11-07T01:00:00Z",
};
const fraud: ModelRecord = {
  ...credit,
  id: "real-fraud-2026",
  name: "Uploaded fraud XGBoost",
  language: "Python",
  dataset_id: "ulb_fraud",
  owner: "Fraud team",
  lifecycle_status: "development",
};

describe("shared inventory presentation adapters", () => {
  it("keeps demo workflow stages explicitly illustrative", () => {
    const demo = demoModels[0];
    const row = demoInventoryRow(demo);
    expect(row.id).toBe(demo.id);
    expect(row.datasetLabel).toBe("UCI");
    expect(row.category).toBe("Credit");
    expect(row.statusLabel).toBe("In review (demo)");
    expect(row.statusTone).toBe("amber");
  });

  it("uses actual Backend lifecycle metadata without inventing approval or metrics", () => {
    const row = liveInventoryRow(credit);
    expect(row).toEqual({
      id: "real-credit-2026",
      name: "Uploaded credit GLM",
      version: "1.0.0",
      language: "R",
      datasetLabel: "UCI",
      category: "Credit",
      statusLabel: "active",
      statusTone: undefined,
      owner: null,
    });
    expect("review" in row).toBe(false);
    expect("parity" in row).toBe(false);
  });

  it("maps ULB Backend models to fraud without importing demo fixtures", () => {
    const row = liveInventoryRow(fraud);
    expect(row.category).toBe("Fraud");
    expect(row.datasetLabel).toBe("ULB");
    expect(row.statusLabel).toBe("development");
    expect(row.owner).toBe("Fraud team");
  });
});
