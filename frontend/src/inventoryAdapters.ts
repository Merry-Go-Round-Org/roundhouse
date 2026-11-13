import type { Model } from "./domain";
import type { ModelRecord } from "./api";
import type { InventoryRow } from "./InventoryTable";
import { inventoryOwner } from "./inventoryProvenance";

/**
 * Keep presentation concerns separate from the two different model schemas.
 * The demo's simulated workflow stage is NOT the Backend lifecycle status,
 * and neither should be presented as a recorded approval.
 */
export function demoInventoryRow(model: Model): InventoryRow {
  return {
    id: model.id,
    name: model.name,
    version: model.version,
    language: model.language,
    datasetLabel: model.dataset,
    category: model.type,
    statusLabel: model.stage === "Validation" ? "In review (demo)" : model.stage,
    statusTone: model.stage === "Validation" ? "amber" : undefined,
    owner: model.owner,
  };
}

export function liveInventoryRow(model: ModelRecord): InventoryRow {
  const synthetic = model.dataset_id === null || model.inventory?.synthetic_demo === true;
  return {
    id: model.id,
    name: model.name,
    version: model.version,
    language: model.language,
    datasetLabel: synthetic ? "Synthetic / no dataset" :
      model.dataset_id === "uci_credit" ? "UCI" : "ULB",
    category: synthetic ? "Survival" : model.dataset_id === "uci_credit" ? "Credit" : "Fraud",
    // Preserve the detailed upload-derived stage independently of the legacy public enum.
    statusLabel: model.inventory?.lifecycle_stage ?? model.lifecycle_status,
    // Lifecycle is not validation or human approval.
    statusTone: undefined,
    owner: inventoryOwner(model).value,
  };
}
