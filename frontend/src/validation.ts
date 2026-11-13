export const checkStatuses = [
  "PASS",
  "FAIL",
  "REVIEW",
  "NOT_ASSESSABLE",
  "NOT_APPLICABLE",
] as const;
export type CheckStatus = (typeof checkStatuses)[number];
export type MetricStatus =
  CheckStatus | "green" | "amber" | "red" | "not_assessable" | "not_run" | "not_applicable";
export function isMetricStatus(value: unknown): value is MetricStatus {
  return (
    typeof value === "string" &&
    [
      ...checkStatuses,
      "green",
      "amber",
      "red",
      "not_assessable",
      "not_run",
      "not_applicable",
    ].includes(value)
  );
}
export function statusDisplay(status: MetricStatus) {
  const canonical =
    (
      {
        green: "PASS",
        amber: "REVIEW",
        red: "FAIL",
        not_assessable: "NOT_ASSESSABLE",
        not_run: "NOT_RUN",
        not_applicable: "NOT_APPLICABLE",
      } as Record<string, string>
    )[status] ?? status;
  const tones: Record<string, string> = {
    PASS: "pass",
    FAIL: "fail",
    REVIEW: "review",
    NOT_ASSESSABLE: "unavailable",
    NOT_APPLICABLE: "neutral",
    NOT_RUN: "unavailable",
  };
  return {
    label: canonical === "NOT_APPLICABLE" ? "NOT_APPLICABLE (N/A)" : canonical,
    tone: tones[canonical],
  };
}
export interface ValidationCheck {
  check_id: string;
  status: CheckStatus;
  required: boolean;
  summary: string;
  evidence_kind: string;
  details: Record<string, unknown>;
}
export interface ValidationEvidence {
  schema_version: "1.0.0";
  report_kind: "legacy_model_validation";
  model_id: string;
  domain: string;
  task: string;
  model_kind: string;
  artifact_sha256: string | null;
  checks: ValidationCheck[];
  summary: {
    overall_status:
      "VALIDATION_FAILED" | "VALIDATION_INCOMPLETE" | "REVIEW_REQUIRED";
    required_checks_passed: boolean;
    blocking_check_ids: string[];
    review_check_ids: string[];
    human_approval_granted: false;
  };
  saved_metrics: Record<string, unknown>;
  limitations: string[];
  generated_at_utc: string;
  policy_version: "1.0.0";
  execution_requested: boolean;
  agent_review?: unknown;
  challenger?: unknown;
}
function object(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
const strings = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every((v) => typeof v === "string");
export function readValidationEvidence(value: unknown): ValidationEvidence {
  const invalid = () => {
    throw new Error(
      "Invalid canonical validation report. Evidence was not displayed.",
    );
  };
  if (!object(value)) return invalid();
  const s = value.summary;
  if (
    value.schema_version !== "1.0.0" ||
    value.report_kind !== "legacy_model_validation" ||
    value.policy_version !== "1.0.0" ||
    typeof value.model_id !== "string" ||
    !value.model_id.trim() ||
    !["uci", "ulb", "survival_synthetic"].includes(String(value.domain)) ||
    !["binary_classification", "survival_analysis"].includes(
      String(value.task),
    ) ||
    !["real_dataset", "synthetic_demo"].includes(String(value.model_kind)) ||
    !(
      value.artifact_sha256 === null ||
      typeof value.artifact_sha256 === "string"
    ) ||
    typeof value.generated_at_utc !== "string" ||
    !Number.isFinite(Date.parse(value.generated_at_utc)) ||
    typeof value.execution_requested !== "boolean" ||
    !object(value.saved_metrics) ||
    !strings(value.limitations) ||
    !Array.isArray(value.checks) ||
    !value.checks.length ||
    !object(s) ||
    !["VALIDATION_FAILED", "VALIDATION_INCOMPLETE", "REVIEW_REQUIRED"].includes(
      String(s.overall_status),
    ) ||
    typeof s.required_checks_passed !== "boolean" ||
    s.human_approval_granted !== false ||
    !strings(s.blocking_check_ids) ||
    !strings(s.review_check_ids)
  )
    return invalid();
  const ids = new Set<string>();
  for (const c of value.checks) {
    if (
      !object(c) ||
      typeof c.check_id !== "string" ||
      !c.check_id.trim() ||
      ids.has(c.check_id) ||
      !checkStatuses.includes(c.status as CheckStatus) ||
      typeof c.required !== "boolean" ||
      typeof c.summary !== "string" ||
      typeof c.evidence_kind !== "string" ||
      !object(c.details)
    )
      return invalid();
    ids.add(c.check_id);
  }
  if (
    [...s.blocking_check_ids, ...s.review_check_ids].some((id) => !ids.has(id))
  )
    return invalid();
  // This reader checks display safety and identity references. Backend ingestion must
  // enforce the full JSON Schema, policy, model/version mapping and evidence integrity.
  return value as unknown as ValidationEvidence;
}
