export interface PackageRead {
  id: string;
  model_id: string;
  name: string;
  version: string;
  language: string;
  dataset_id: string;
  sha256: string;
  input_sha256: string;
  n_rows: number;
}
export interface ScoringResult {
  scope: "development_fixture_scoring";
  package_id: string;
  package_sha256: string;
  input_sha256: string;
  predictions_sha256: string;
  prediction_reference: string;
  runtime_image: string;
  n_rows: number;
  limitation: string;
}
export interface ScoringRead {
  id: string;
  package_id: string;
  status: "queued" | "running" | "completed" | "failed";
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  result: ScoringResult | null;
}
const text = (v: unknown): v is string =>
  typeof v === "string" && v.trim().length > 0;
const hash = (v: unknown) => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const count = (v: unknown) => Number.isInteger(v) && Number(v) >= 0;
const date = (v: unknown) => text(v) && Number.isFinite(Date.parse(v));
function object(v: unknown): Record<string, unknown> {
  if (!v || typeof v !== "object" || Array.isArray(v))
    throw new Error("Invalid scoring API response.");
  return v as Record<string, unknown>;
}
export function readPackage(value: unknown, modelId?: string): PackageRead {
  const v = object(value);
  if (
    !["id", "model_id", "name", "version", "language", "dataset_id"].every(
      (k) => text(v[k]),
    ) ||
    !hash(v.sha256) ||
    !hash(v.input_sha256) ||
    !count(v.n_rows) ||
    (modelId !== undefined && v.model_id !== modelId)
  )
    throw new Error("Invalid package response or model mismatch.");
  return value as PackageRead;
}
export function readScoring(
  value: unknown,
  packageId: string,
  executionId?: string,
): ScoringRead {
  const v = object(value);
  if (
    !text(v.id) ||
    v.package_id !== packageId ||
    (executionId !== undefined && v.id !== executionId) ||
    !["queued", "running", "completed", "failed"].includes(String(v.status)) ||
    !date(v.created_at) ||
    ![v.started_at, v.finished_at].every((x) => x === null || date(x)) ||
    !(v.error === null || typeof v.error === "string")
  )
    throw new Error("Invalid scoring response or identity mismatch.");
  if (v.result !== null) {
    const r = object(v.result);
    if (
      r.scope !== "development_fixture_scoring" ||
      r.package_id !== packageId ||
      ![r.package_sha256, r.input_sha256, r.predictions_sha256].every(hash) ||
      !count(r.n_rows) ||
      ![r.prediction_reference, r.runtime_image, r.limitation].every(text)
    )
      throw new Error("Invalid scoring result evidence.");
  }
  return value as ScoringRead;
}
