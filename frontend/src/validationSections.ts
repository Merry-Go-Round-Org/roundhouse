import type { ValidationCheck } from "./validation";
export const validationGroups = [
  {
    id: "performance",
    title: "Saved performance",
    checkIds: ["saved_metric_consistency"],
  },
  {
    id: "calibration",
    title: "Calibration and uncertainty",
    checkIds: ["uncertainty_calibration"],
  },
  { id: "stability", title: "Stability", checkIds: ["out_of_time_stability"] },
  { id: "fairness", title: "Fairness", checkIds: ["demographic_fairness"] },
  {
    id: "migration",
    title: "Migration parity",
    checkIds: ["migration_parity"],
  },
  {
    id: "execution",
    title: "Executable checks",
    checkIds: [
      "fixture_scoring",
      "reject_missing_row_id",
      "reject_duplicated_row_id",
      "reject_target_leakage",
    ],
  },
  {
    id: "integrity",
    title: "Integrity and provenance",
    checkIds: [
      "artifact_integrity",
      "schema_contract",
      "split_provenance",
      "historical_verification_lineage",
      "historical_fit_time_attestation",
      "cross_platform_refit",
      "cross_platform_saved_artifact_inference",
      "policy_required_check_coverage",
    ],
  },
  { id: "other", title: "Other reported checks", checkIds: [] },
] as const;
export function groupChecks(checks: ValidationCheck[]) {
  const known = new Set<string>(
    validationGroups.flatMap((g) => [...g.checkIds]),
  );
  return validationGroups.map((group) => ({
    ...group,
    checks: checks.filter((c) =>
      group.id === "other"
        ? !known.has(c.check_id)
        : (group.checkIds as readonly string[]).includes(c.check_id),
    ),
  }));
}
export function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
export function numericEvidence(value: unknown): string {
  if (value == null) return "Not supplied";
  return typeof value === "number" && Number.isFinite(value)
    ? String(value)
    : "Invalid supplied value";
}
export function intervalEvidence(value: unknown): string {
  if (value == null) return "Not supplied";
  return Array.isArray(value) &&
    value.length === 2 &&
    value.every((v) => typeof v === "number" && Number.isFinite(v)) &&
    value[0] <= value[1]
    ? `[${value[0]}, ${value[1]}]`
    : "Invalid supplied interval";
}
