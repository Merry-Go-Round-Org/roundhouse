import type { Model } from "./domain";
import type { ModelRecord } from "./api";
import type { ModelFact } from "./ModelRecordCard";

/** Demo-only illustrative fields; never reuse these values as live evidence. */
export function demoModelFacts(model: Model): ModelFact[] {
  return [
    { label: "Model type", value: model.type + " risk" },
    { label: "Implementation", value: model.language + " · " + model.version },
    { label: "Owner", value: model.owner ?? "Not assigned" },
    { label: "Risk tier", value: model.tier + " · illustrative" },
    { label: "Positive outcome", value: model.type === "Credit" ? "Default = 1 (demo)" : "Fraud = 1 (demo)" },
    { label: "Dataset version", value: "Checksum awaiting evidence" },
  ];
}

/** Backend metadata only. Absent governance and lineage fields remain absent. */
export function liveModelFacts(model: ModelRecord): ModelFact[] {
  return [
    { label: "Model ID", value: model.id },
    { label: "Name", value: model.name },
    { label: "Version", value: model.version },
    { label: "Language", value: model.language },
    { label: "Lifecycle (not approval)", value: model.inventory?.lifecycle_stage ?? model.lifecycle_status },
    { label: "Created", value: model.created_at },
    { label: "Domain / task / algorithm", value: [
      model.inventory?.manifest?.domain,
      model.inventory?.manifest?.task,
      model.inventory?.manifest?.algorithm,
    ].filter((x): x is string => typeof x === "string" && !!x.trim()).join(" / ") || undefined },
  ];
}
