import type { ModelRecord } from "./api";

const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === "string" && !!v.trim();
const sources = ["file", "artifact", "code", "document", "proposed", "unknown"];

/** Saved intake provenance is nested in the API's manifest, not its upload summary. */
export function intakeField(model: ModelRecord, key: string) {
  const manifest = model.inventory?.manifest;
  if (manifest?.ai_native !== true || !object(manifest.provenance)) return null;
  const field = manifest.provenance[key];
  if (!object(field) || !sources.includes(String(field.source))) return null;
  return {
    value: field.value,
    confirmed: field.confirmed === true && text(field.confirmed_by),
    source: String(field.source),
    evidence: text(field.evidence) ? field.evidence : null,
    reason: text(field.reason) ? field.reason : null,
  };
}

export function intakeSource(model: ModelRecord, key: string): string | undefined {
  const field = intakeField(model, key);
  if (!field) return undefined;
  return [
    `Source: ${field.source} · ${field.confirmed ? "Confirmed at Gate 0" : "Gate 0 confirmation not supplied"}`,
    field.evidence, field.reason,
  ].filter(Boolean).join(" · ");
}

export function inventoryOwner(model: ModelRecord) {
  const saved = intakeField(model, "owner");
  const confirmed = saved?.confirmed && text(saved.value) ? saved.value.trim() : null;
  const current = text(model.owner) ? model.owner.trim() : null;
  return {
    value: current ?? confirmed,
    source: current
      ? current === confirmed ? intakeSource(model, "owner") : "Source: existing model API"
      : confirmed ? `${intakeSource(model, "owner")}. Top-level model owner is not supplied.` : undefined,
    conflict: current && confirmed && current !== confirmed
      ? `Saved Gate 0 owner: ${confirmed}. Current model owner differs; verify the record with the model owner.` : null,
  };
}
