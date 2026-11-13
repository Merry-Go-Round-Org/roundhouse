import { expect, it } from "vitest";
import type { ModelRecord } from "./api";
import { intakeField, intakeSource, inventoryOwner } from "./inventoryProvenance";
import { liveInventoryRow } from "./inventoryAdapters";
const model: ModelRecord = {
  id: "m1", name: "Credit", version: "1", language: "R", dataset_id: "uci_credit",
  purpose: "Credit", owner: null, lifecycle_status: "development", created_at: "2026-11-11T00:00:00Z",
  inventory: { manifest: { ai_native: true, provenance: {
    owner: { value: "Risk team", source: "proposed", confirmed: true, confirmed_by: "reviewer", reason: "Supplied by human reviewer at Gate 0" },
  } } },
};
it("reads confirmed intake ownership from saved manifest evidence without fabricating current model identity", () => {
  expect(inventoryOwner(model).value).toBe("Risk team");
  expect(inventoryOwner(model).source).toContain("Top-level model owner is not supplied");
  expect(liveInventoryRow(model).owner).toBe("Risk team");
  expect(intakeSource(model, "owner")).toContain("Confirmed at Gate 0");
  expect(model.owner).toBeNull();
  expect(inventoryOwner({ ...model, owner: "New owner" })).toMatchObject({ value: "New owner", conflict: expect.stringContaining("Risk team") });
});
it("never promotes inferred, malformed or unconfirmed metadata to a confirmed owner", () => {
  for (const field of [null, "Risk team", {value:"Risk team",source:"proposed"},
    {value:"Risk team",source:"proposed",confirmed:true},
    {value:"Risk team",source:"unknown-source",confirmed:true,confirmed_by:"reviewer"},
    {value:{name:"Risk team"},source:"proposed",confirmed:true,confirmed_by:"reviewer"}]) {
    const changed = {...model,inventory:{manifest:{ai_native:true,provenance:{owner:field}}}};
    expect(inventoryOwner(changed).value).toBeNull();
  }
  expect(intakeField({...model,inventory:{manifest:{provenance:model.inventory!.manifest!.provenance}}}, "owner")).toBeNull();
  expect(inventoryOwner({...model, inventory: undefined, owner:"Team"}).value).toBe("Team");
});
