/** Backend PSI and dashboard wire contracts. Never infer risk approval from drift bands. */
export type PsiBand = "green" | "amber" | "red" | "not_assessable";
export type PsiStatus = "PASS" | "REVIEW" | "NOT_ASSESSABLE";
export interface PsiMetric {
  status: PsiStatus;
  band: PsiBand;
  psi: number;
  reference_rows: number;
  current_rows: number;
  reason: string;
}
export interface PsiWindow {
  schema_version: string;
  kind: "model_stability_window";
  model_id: string; // Declared package name, NOT necessarily the Backend model UUID.
  artifact_sha256: string;
  baseline_sha256: string;
  policy_version: string;
  reference_scope: "development";
  reference_rows: number;
  current_rows: number;
  status: PsiStatus;
  band: PsiBand;
  score_band: PsiBand;
  feature_band: PsiBand;
  revalidation_required: boolean;
  revalidation_run_id?: string;
  analysis_run_id?: string;
  trigger_band?: PsiBand;
  red_features: string[];
  score: PsiMetric;
  features: Record<string, PsiMetric>;
  limitations: string[];
  simulated: boolean;
  shift: { feature: string; factor: number } | null;
  dataset_version_id: string;
  package_sha256: string;
  runtime_image: string;
  input_scope: "validation_partition_simulation" | "test_partition_simulation" | "uploaded_batch";
  baseline_partition: "train";
  window_label: string;
}
export interface MonitoringEvent {
  id: string;
  model_id: string;
  run_id: string;
  created_at: string;
  payload: PsiWindow;
}
export interface PortfolioMonitoringRow {
  model_id: string;
  model_name: string;
  lifecycle_status: string;
  latest: PsiWindow | null;
  last_run: string | null;
}
export interface DurationSummary {
  count: number;
  mean: number | null;
  total: number;
}
export interface DashboardSummary {
  models: number;
  packages: number;
  reports: number;
  monitoring_events: number;
  runs: Record<string, number>;
  scoring_runs: Record<string, number>;
  reviews: Record<string, number>;
  findings: Record<string, number>;
  lifecycle: Record<string, number>;
  stage_elapsed_seconds: Record<string, DurationSummary>;
  review_elapsed_seconds: DurationSummary;
  llm_cost_usd: number;
  red_alerts: number;
  measurement_limitations: string[];
  source: "persisted_records";
}
const isObject = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === "string" && v.trim().length > 0;
const integer = (v: unknown): v is number => Number.isSafeInteger(v) && (v as number) >= 0;
const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const date = (v: unknown): v is string => text(v) && Number.isFinite(Date.parse(v));
const hash = (v: unknown): v is string => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const bands: readonly string[] = ["green", "amber", "red", "not_assessable"];
const statuses: readonly string[] = ["PASS", "REVIEW", "NOT_ASSESSABLE"];
const isBand = (v: unknown) => bands.includes(String(v));
function invalid(message: string): never { throw new Error("Invalid monitoring API response: " + message); }

function psiMetric(v: unknown): v is PsiMetric {
  return isObject(v) && statuses.includes(String(v.status)) &&
    isBand(v.band) && finite(v.psi) && v.psi >= 0 &&
    integer(v.reference_rows) && integer(v.current_rows) &&
    typeof v.reason === "string" &&
    (v.band !== "not_assessable" || v.status === "NOT_ASSESSABLE");
}
export function readPsiWindow(v: unknown): PsiWindow {
  if (!isObject(v) || v.kind !== "model_stability_window" ||
    !text(v.schema_version) || !text(v.model_id) ||
    !hash(v.artifact_sha256) || !hash(v.baseline_sha256) ||
    !text(v.policy_version) || v.reference_scope !== "development" ||
    !integer(v.reference_rows) || !integer(v.current_rows) ||
    !statuses.includes(String(v.status)) || !isBand(v.band) ||
    !isBand(v.score_band) || !isBand(v.feature_band) ||
    typeof v.revalidation_required !== "boolean" ||
    !Array.isArray(v.red_features) || !v.red_features.every(text) ||
    !psiMetric(v.score) || !isObject(v.features) ||
    !Object.values(v.features).every(psiMetric) ||
    !Array.isArray(v.limitations) || !v.limitations.every(text) ||
    typeof v.simulated !== "boolean" ||
    !text(v.dataset_version_id) || !hash(v.package_sha256) ||
    !text(v.runtime_image) || !text(v.window_label) ||
    !["uploaded_batch", "validation_partition_simulation", "test_partition_simulation"].includes(String(v.input_scope)) ||
    v.baseline_partition !== "train" ||
    (v.analysis_run_id !== undefined && !text(v.analysis_run_id)) ||
    (v.trigger_band !== undefined && !isBand(v.trigger_band)) ||
    (v.revalidation_run_id !== undefined && !text(v.revalidation_run_id)) ||
    v.reference_rows !== v.score.reference_rows ||
    v.current_rows !== v.score.current_rows ||
    Object.values(v.features as Record<string, PsiMetric>).some((metric) =>
      metric.reference_rows !== v.reference_rows || metric.current_rows !== v.current_rows) ||
    (v.simulated !== (v.input_scope !== "uploaded_batch")) ||
    (v.simulated ? !isObject(v.shift) || !text(v.shift.feature) ||
       !finite(v.shift.factor) || v.shift.factor <= 0 || v.shift.factor > 100
       : v.shift !== null) ||
    (v.status === "NOT_ASSESSABLE" && v.band !== "not_assessable") ||
    (v.revalidation_required && v.band === "not_assessable") ||
    v.red_features.some((key) => !isObject((v.features as Record<string, unknown>)[key]) ||
      (v.features as Record<string, PsiMetric>)[key].band !== "red"))
    invalid("PSI evidence/provenance is missing or inconsistent.");
  return v as unknown as PsiWindow;
}

export function readMonitoringEvents(value: unknown, modelId: string): MonitoringEvent[] {
  if (!Array.isArray(value)) invalid("expected events array");
  const ids = new Set<string>();
  return value.map((entry: unknown) => {
    if (!isObject(entry) || !text(entry.id) || ids.has(entry.id) ||
      entry.model_id !== modelId || !text(entry.run_id) || !date(entry.created_at))
      invalid("wrong model, duplicate event or invalid timestamp");
    ids.add(entry.id);
    return { ...entry, payload: readPsiWindow(entry.payload) } as MonitoringEvent;
  });
}
export function readPortfolioMonitoring(value: unknown): PortfolioMonitoringRow[] {
  if (!Array.isArray(value)) invalid("expected portfolio array");
  const ids = new Set<string>();
  return value.map((entry: unknown) => {
    if (!isObject(entry) || !text(entry.model_id) || ids.has(entry.model_id) ||
      !text(entry.model_name) || !text(entry.lifecycle_status) ||
      !(entry.last_run === null || date(entry.last_run)) ||
      !(entry.latest === null || isObject(entry.latest)) ||
      (entry.latest === null) !== (entry.last_run === null))
      invalid("portfolio model or monitoring snapshot is invalid");
    ids.add(entry.model_id);
    return {
      model_id: entry.model_id,
      model_name: entry.model_name,
      lifecycle_status: entry.lifecycle_status,
      last_run: entry.last_run,
      latest: entry.latest === null ? null : readPsiWindow(entry.latest),
    } as PortfolioMonitoringRow;
  });
}
function counts(v: unknown): v is Record<string, number> {
  return isObject(v) && Object.values(v).every(integer);
}
function duration(v: unknown): v is DurationSummary {
  return isObject(v) && integer(v.count) && finite(v.total) && v.total >= 0 &&
    (v.mean === null || (finite(v.mean) && v.mean >= 0)) &&
    (v.count === 0 ? (v.total === 0 && (v.mean === null || v.mean === 0)) : v.mean !== null);
}
export function readDashboard(value: unknown): DashboardSummary {
  if (!isObject(value) || value.source !== "persisted_records" ||
    !["models", "packages", "reports", "monitoring_events", "red_alerts"].every(k => integer(value[k])) ||
    !["runs", "scoring_runs", "reviews", "findings", "lifecycle"].every(k => counts(value[k])) ||
    !isObject(value.stage_elapsed_seconds) ||
    !Object.values(value.stage_elapsed_seconds).every(duration) ||
    !duration(value.review_elapsed_seconds) ||
    !finite(value.llm_cost_usd) || value.llm_cost_usd < 0 ||
    !Array.isArray(value.measurement_limitations) ||
    !value.measurement_limitations.every(text))
    invalid("dashboard counts, stage timing or persisted source metadata");
  return value as unknown as DashboardSummary;
}
/** Backend PSI bands are descriptive, not approval or model-performance signals. */
export function bandLabel(v: PsiBand): string {
  return v === "not_assessable" ? "NOT ASSESSABLE" : v.toUpperCase();
}
