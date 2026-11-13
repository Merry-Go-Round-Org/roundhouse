import { createNativeClient } from "./nativeApi";
import { createDeletionClient } from "./deletion";
import { createDurableChatClient } from "./durableChat";
import { notifySessionExpired } from "./auth";
import {
  readReviews, sameReviewCheckpoint,
  type ReviewDraft, type ReviewItem, type ReviewStatus,
} from "./reviews";
import { readMarkdownResponse } from "./documentTransport";
import { readModelFiles, modelFileRoute, type ModelFile } from "./modelFiles";
import { readFinding, readFindingOwners, sameFinding, type Finding, type FindingPatch } from "./findings";
import { readAuditEvents } from "./auditHistory";
import { readAccount, readAccounts, accountRoles, type Account } from "./accounts";
import { multipart, readUpload } from "./uploads";
import {
  isMetricStatus,
  readValidationEvidence,
  type MetricStatus,
} from "./validation";
import {
  readPackage,
  readScoring,
  type PackageRead,
  type ScoringRead,
} from "./scoringTypes";
export type { PackageRead, ScoringRead } from "./scoringTypes";
import { consumeChat, type ChatNotice } from "./chatStream";
import {
  readMonitoringEvents, readPortfolioMonitoring, readDashboard,
  type MonitoringEvent, type PortfolioMonitoringRow, type DashboardSummary,
} from "./monitoring";
import type { ChatQuestion } from "./chatChoices";
import { readParityRun, readParityRuns, type ParityRun } from "./parity";

/** Models, runs and dataset versions reflect the current Backend on backend-wit. */
export type DatasetId = "uci_credit" | "ulb_fraud";
export type Operation = "document" | "rebuild" | "validate" | "monitor";
export type RunStatus =
  "queued" | "running" | "awaiting_review" | "completed" | "failed";
export interface RunCreate {
  operation: Operation;
  model_version: string;
  /** Synthetic survival models have no public dataset. */
  dataset_id: DatasetId | null;
}
export interface RunRead extends RunCreate {
  id: string;
  model_id: string;
  status: RunStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  stage: string | null;
  error: string | null;
}
export function readRun(value: unknown): RunRead {
  if (!value || typeof value !== "object")
    throw new ApiError(200, "Invalid run response.");
  const v = value as Record<string, unknown>;
  const date = (x: unknown) =>
    typeof x === "string" && Number.isFinite(Date.parse(x));
  if (
    !["id", "model_id", "model_version"].every(
      (k) => typeof v[k] === "string" && (v[k] as string).trim().length > 0,
    ) ||
    !(v.dataset_id === null || ["uci_credit", "ulb_fraud"].includes(String(v.dataset_id))) ||
    !["document", "rebuild", "validate", "monitor"].includes(
      String(v.operation),
    ) ||
    !["queued", "running", "awaiting_review", "completed", "failed"].includes(
      String(v.status),
    ) ||
    !date(v.created_at) ||
    ![v.started_at, v.finished_at].every((x) => x === null || date(x)) ||
    ![v.stage, v.error].every((x) => x === null || typeof x === "string")
  )
    throw new ApiError(
      200,
      "Invalid run response: expected the issue #5 schema.",
    );
  return value as RunRead;
}
export interface DatasetStatistics {
  row_count: number;
  positive_rate: number;
  partition_counts: Record<"train" | "validation" | "test", number>;
  positive_rates: Record<"train" | "validation" | "test", number | null>;
  partition_sha256: Record<"train" | "validation" | "test", string>;
}
export interface DatasetRead {
  id: DatasetId;
  name: string;
  filename: string;
  target_description: string;
  limitations: string[];
  /** Present for verified uploaded datasets; absent for older catalogue fixtures. */
  version_id?: string;
  version?: string;
  sha256?: string;
  split_sha256?: string;
  verified_upload?: boolean;
  descriptor?: Record<string, unknown>;
  columns?: string[];
  statistics?: DatasetStatistics;
  model_count?: number;
  created_at?: string;
}
export function readDatasets(value: unknown): DatasetRead[] {
  const validHash = (v: unknown) => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
  const object = (v: unknown): v is Record<string, unknown> =>
    !!v && typeof v === "object" && !Array.isArray(v);
  const ids = new Set<string>();
  if (!Array.isArray(value)) throw new ApiError(200, "Invalid dataset catalogue.");
  for (const raw of value) {
    if (!object(raw) ||
      !["uci_credit", "ulb_fraud"].includes(String(raw.id)) ||
      [raw.name, raw.filename, raw.target_description].some((v) => typeof v !== "string" || !v.trim()) ||
      !Array.isArray(raw.limitations) || raw.limitations.some((x: unknown) => typeof x !== "string") ||
      ids.has(String(raw.id)))
      throw new ApiError(200, "Invalid dataset catalogue.");
    ids.add(String(raw.id));
    for (const key of ["version_id", "version"] as const)
      if (raw[key] !== undefined && (typeof raw[key] !== "string" || !raw[key].trim()))
        throw new ApiError(200, "Invalid uploaded dataset version.");
    for (const key of ["sha256", "split_sha256"] as const)
      if (raw[key] !== undefined && !validHash(raw[key]))
        throw new ApiError(200, "Invalid uploaded dataset checksum.");
    if (raw.verified_upload !== undefined && typeof raw.verified_upload !== "boolean")
      throw new ApiError(200, "Invalid dataset verification flag.");
    if (raw.model_count !== undefined &&
      (!Number.isSafeInteger(raw.model_count) || (raw.model_count as number) < 0))
      throw new ApiError(200, "Invalid dataset model count.");
    if (raw.created_at !== undefined &&
      (typeof raw.created_at !== "string" || !Number.isFinite(Date.parse(raw.created_at))))
      throw new ApiError(200, "Invalid uploaded dataset timestamp.");
    if (raw.descriptor !== undefined && !object(raw.descriptor))
      throw new ApiError(200, "Invalid dataset descriptor.");
    if (raw.columns !== undefined &&
      (!Array.isArray(raw.columns) || raw.columns.some((c: unknown) => typeof c !== "string" || !c.trim())))
      throw new ApiError(200, "Invalid dataset column list.");
    if (raw.statistics !== undefined) {
      const stats = raw.statistics;
      if (!object(stats) || !Number.isSafeInteger(stats.row_count) ||
        (stats.row_count as number) <= 0 ||
        typeof stats.positive_rate !== "number" || stats.positive_rate < 0 || stats.positive_rate > 1 ||
        !["partition_counts", "positive_rates", "partition_sha256"].every((k) => object(stats[k])))
        throw new ApiError(200, "Invalid dataset statistics.");
      const counts = stats.partition_counts as Record<string, unknown>;
      const rates = stats.positive_rates as Record<string, unknown>;
      const hashes = stats.partition_sha256 as Record<string, unknown>;
      for (const key of ["train", "validation", "test"]) {
        const rate = rates[key];
        if (!Number.isSafeInteger(counts[key]) || (counts[key] as number) < 0 ||
          !(rate === null || (typeof rate === "number" && Number.isFinite(rate) && rate >= 0 && rate <= 1)) ||
          !validHash(hashes[key]))
          throw new ApiError(200, "Invalid dataset partition evidence.");
      }
      if ((counts.train as number) + (counts.validation as number) + (counts.test as number) !== stats.row_count)
        throw new ApiError(200, "Dataset partition counts do not reconcile.");
    }
    if (raw.verified_upload === true &&
      (!raw.version_id || !raw.version || !raw.sha256 || !raw.split_sha256 || !raw.statistics))
      throw new ApiError(200, "Verified dataset is missing required evidence.");
  }
  return value as DatasetRead[];
}
export interface ReportRead {
  id: string;
  model_id: string;
  run_id: string;
  kind: "documentation" | "parity" | "validation";
  artifact_id: string;
  metrics: {
    name: string;
    value: number | null;
    status: MetricStatus;
    reason: string;
    evidence_ids: string[];
  }[];
  /** Backend report metadata; absent on older read-only fixtures. */
  model_version?: string | null;
  artifact_version?: string | null;
  artifact_sha256?: string | null;
  created_at?: string | null;
  review_status?: "pending" | "decided" | null;
  review_resolution?: { decision: string; rationale: string; conditions: string[]; reviewer_id: string } | null;
  review_id?: string | null;
  evidence?: Record<string, unknown>;
  source_files?: Record<string, unknown>[];
}
export interface ModelCreate {
  name: string;
  version: string;
  language: "R" | "Python";
  dataset_id: DatasetId;
  purpose: string;
  owner: string | null;
}
/** The public lifecycle is deliberately broader than the old 3-state UI. */
export type ModelLifecycle = "development" | "active" | "retired" |
  "uploaded" | "documented" | "rebuilt" | "approved" | "monitored" | "revalidation_due";
export type ModelProvenance = { value: unknown; source: "upload" | "not_provided" };
export interface ModelInventory {
  lifecycle_stage?: string;
  manifest?: Record<string, unknown>;
  provenance?: Record<string, ModelProvenance>;
  upload_id?: string;
  dataset_version_id?: string | null;
  bundle_sha256?: string;
  synthetic_demo?: boolean;
  applicability?: Partial<Record<Operation, "applicable" | "NOT_APPLICABLE">>;
  last_validation_date?: string | null;
  validation_frequency?: string | null;
  next_validation_due?: string | null;
  open_findings?: number;
  approval?: Record<string, unknown> | null;
  monitoring?: Record<string, unknown> | null;
}
export interface ModelRecord extends Omit<ModelCreate, "dataset_id"> {
  id: string;
  /** Null denotes a synthetic survival demonstration, not ULB fraud. */
  dataset_id: DatasetId | null;
  lifecycle_status: ModelLifecycle;
  created_at: string;
  inventory?: ModelInventory;
}
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly runId?: string,
  ) {
    super(message);
  }
}
function isRecord(value: unknown): value is ModelRecord {
  if (!value || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  return (
    ["id", "name", "version", "purpose", "created_at"].every(
      (k) => typeof v[k] === "string" && (v[k] as string).length > 0,
    ) &&
    ["R", "Python"].includes(String(v.language)) &&
    (v.dataset_id === null || ["uci_credit", "ulb_fraud"].includes(String(v.dataset_id))) &&
    ["development", "active", "retired", "uploaded", "documented", "rebuilt", "approved", "monitored", "revalidation_due"].includes(String(v.lifecycle_status)) &&
    (v.owner === null || typeof v.owner === "string") &&
    Number.isFinite(Date.parse(String(v.created_at))) &&
    (v.inventory === undefined ||
      (typeof v.inventory === "object" && v.inventory !== null && !Array.isArray(v.inventory) &&
        (!("lifecycle_stage" in v.inventory) ||
          (typeof v.inventory.lifecycle_stage === "string" && !!v.inventory.lifecycle_stage.trim())) &&
        (!("synthetic_demo" in v.inventory) || typeof v.inventory.synthetic_demo === "boolean") &&
        (!("applicability" in v.inventory) ||
          (typeof v.inventory.applicability === "object" && v.inventory.applicability !== null &&
            !Array.isArray(v.inventory.applicability) &&
            Object.entries(v.inventory.applicability).every(
              ([key, status]) => ["document", "rebuild", "validate", "monitor"].includes(key) &&
                ["applicable", "NOT_APPLICABLE"].includes(String(status)),
            )))))
  );
}
export function createModelClient(
  baseUrl = "/api/v1",
  transport: typeof fetch = fetch,
) {
  const base = baseUrl.replace(/\/$/, "");
  async function responseFor(
    path: string,
    init: RequestInit = {},
  ): Promise<Response> {
    let response: Response;
    try {
      response = await transport(base + path, {
        ...init,
        credentials: "include",
        headers: { ...(init.body instanceof FormData ? {} : { "Content-Type": "application/json" }), ...init.headers },
      });
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") throw error;
      throw new ApiError(
        0,
        "Cannot reach the backend. Check the server and connection.",
      );
    }
    if (!response.ok) {
      if (response.status === 401) notifySessionExpired();
      const messages: Record<number, string> = {
        401: "Sign in to continue.",
        403: "You do not have access to this operation.",
        404: "Requested record not found.",
        409: "This record changed. Reload it before retrying.",
        422: "The backend rejected the supplied fields.",
        503: "Execution is currently unavailable.",
        500: "The backend or development proxy could not complete the request. Check the backend server and retry.",
        502: "The development proxy cannot reach the backend. Check the backend server and retry.",
        504: "The backend did not respond in time. Retry the read request.",
        501: "This feature is not implemented by the backend yet.",
      };
      let runId: string | undefined;
      let archiveId: string | undefined;
      let deletionDetail: unknown;
      let message =
        messages[response.status] ??
        `Backend request failed (${response.status}).`;
      try {
        const body = await response.json();
        const detail = body?.detail;
        if (detail?.code === "model_delete_blocked" || detail?.code === "dataset_delete_blocked") deletionDetail = detail;
        if (typeof detail?.run_id === "string") runId = detail.run_id;
        if (typeof detail?.archive_id === "string") archiveId = detail.archive_id;
        if (typeof detail === "string") message = detail;
        else if (typeof detail?.message === "string") message = detail.message;
        else if (detail && typeof detail.code === "string" &&
          Array.isArray(detail.checks) && detail.checks.every((v: unknown) => typeof v === "string"))
          message = detail.code + ": " + detail.checks.join(", ");
        else if (Array.isArray(detail)) {
          const errors = detail.filter((item) => typeof item?.msg === "string");
          if (errors.length)
            message = errors
              .map(
                (item) =>
                  `${Array.isArray(item.loc) ? item.loc.join(".") + ": " : ""}${item.msg}`,
              )
              .join("; ");
        }
      } catch {
        /* Keep a useful fallback for non-JSON errors. */
      }
      throw Object.assign(new ApiError(response.status, message, runId), { archiveId, deletionDetail });
    }
    return response;
  }
  async function request(
    path: string,
    init: RequestInit = {},
  ): Promise<unknown> {
    const response = await responseFor(path, init);
    try {
      return await response.json();
    } catch {
      throw new ApiError(response.status, "Backend returned invalid JSON.");
    }
  }
  function record(value: unknown): ModelRecord {
    if (!isRecord(value))
      throw new ApiError(
        200,
        "Backend model response does not match the agreed schema.",
      );
    return value;
  }
  return {
    deletion: createDeletionClient(request),
    native: createNativeClient(request, responseFor, (path, form, progress, signal) => multipart(base + path, form, progress, signal)),
    chat: createDurableChatClient(request, responseFor),
    async uploadModel(
      file: File,
      progress: (percent: number | null) => void,
      signal?: AbortSignal,
    ) {
      if (!file.name.toLowerCase().endsWith(".zip") || !file.size)
        throw new Error("Choose a non-empty model ZIP bundle.");
      const form = new FormData();
      form.append("bundle", file);
      return readUpload(
        await multipart(base + "/models/uploads", form, progress, signal),
      );
    },
    async listUploads(offset = 0, limit = 20, signal?: AbortSignal) {
      if (!Number.isInteger(offset) || offset < 0 || !Number.isInteger(limit) || limit < 1 || limit > 100)
        throw new Error("Invalid upload pagination.");
      const value = await request(`/models/uploads?offset=${offset}&limit=${limit}`, { signal });
      if (!Array.isArray(value) || value.length > limit)
        throw new Error("Invalid saved upload list.");
      const rows = value.map(readUpload);
      if (new Set(rows.map(row => row.id)).size !== rows.length)
        throw new Error("Duplicate saved upload IDs.");
      return rows;
    },
    async getUpload(id: string, signal?: AbortSignal) {
      const state = readUpload(
        await request(`/models/uploads/${encodeURIComponent(id)}`, { signal }),
      );
      if (state.id !== id) throw new Error("Backend returned another upload.");
      return state;
    },
    async uploadDataset(
      source: File,
      descriptor: File,
      split: File | null,
      progress: (percent: number | null) => void,
      signal?: AbortSignal,
    ) {
      if (![source, descriptor].every((f) => f instanceof File && f.size > 0))
        throw new Error("Choose a non-empty source dataset and dataset.json.");
      if (split !== null && (!(split instanceof File) || split.size === 0))
        throw new Error("Choose a non-empty split file, or leave it empty for automatic splitting.");
      const form = new FormData();
      form.append("source", source);
      form.append("descriptor", descriptor);
      if (split !== null) form.append("split", split);
      // Proposed synchronous persistence response; any async job needs a separately agreed status endpoint.
      const value = await multipart(base + "/datasets", form, progress, signal);
      if (
        !value ||
        typeof value !== "object" ||
        !("id" in value) ||
        typeof value.id !== "string" ||
        !value.id.trim() ||
        !("status" in value) ||
        value.status !== "completed" ||
        !("checks" in value) ||
        !Array.isArray(value.checks) ||
        !value.checks.length ||
        value.checks.some(
          (c) =>
            !c ||
            typeof c.name !== "string" ||
            typeof c.reason !== "string" ||
            typeof c.blocking !== "boolean" ||
            !["passed", "failed"].includes(c.status) ||
            (c.blocking && c.status === "failed"),
        )
      )
        throw new Error(
          "Dataset checks/completion do not match the proposed contract. Check the catalogue before retrying.",
        );
      return {
        id: value.id,
        checks: value.checks as import("./uploads").UploadCheck[],
      };
    },
    async listDatasets(signal?: AbortSignal): Promise<DatasetRead[]> {
      return readDatasets(await request("/datasets", { signal }));
    },
    async listReports(
      modelId: string,
      signal?: AbortSignal,
    ): Promise<ReportRead[]> {
      const value = await request(
        `/models/${encodeURIComponent(modelId)}/reports`,
        { signal },
      );
      if (
        !Array.isArray(value) ||
        value.some(
          (v) =>
            !v ||
            v.model_id !== modelId ||
            ![v.id, v.run_id, v.artifact_id].every(
              (x) => typeof x === "string" && x.trim(),
            ) ||
            !["documentation", "parity", "validation"].includes(v.kind) ||
            !Array.isArray(v.metrics) ||
            v.metrics.some(
              (m: ReportRead["metrics"][number]) =>
                !m ||
                typeof m.name !== "string" ||
                typeof m.reason !== "string" ||
                !(
                  m.value === null ||
                  (typeof m.value === "number" && Number.isFinite(m.value))
                ) ||
                !isMetricStatus(m.status) ||
                !Array.isArray(m.evidence_ids) ||
                m.evidence_ids.some((x) => typeof x !== "string"),
            ) ||
            ["model_version", "artifact_version", "review_id"].some(
              (key) => v[key] !== undefined && v[key] !== null && (typeof v[key] !== "string" || !v[key].trim()),
            ) ||
            (v.artifact_sha256 !== undefined && v.artifact_sha256 !== null &&
              (typeof v.artifact_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(v.artifact_sha256))) ||
            (v.created_at !== undefined && v.created_at !== null &&
              (typeof v.created_at !== "string" || !Number.isFinite(Date.parse(v.created_at)))) ||
            (v.review_status !== undefined && v.review_status !== null &&
              !["pending", "decided"].includes(v.review_status)) ||
            (v.review_resolution !== undefined && v.review_resolution !== null &&
              (v.review_status !== "decided" || typeof v.review_resolution !== "object" ||
                !["accepted", "accepted_with_conditions", "changes_requested"].includes(v.review_resolution.decision) ||
                typeof v.review_resolution.rationale !== "string" ||
                typeof v.review_resolution.reviewer_id !== "string" ||
                !Array.isArray(v.review_resolution.conditions) ||
                !v.review_resolution.conditions.every((condition: unknown) => typeof condition === "string" && !!condition.trim()))) ||
            (v.evidence !== undefined && (!v.evidence || typeof v.evidence !== "object" || Array.isArray(v.evidence))) ||
            (v.source_files !== undefined && (!Array.isArray(v.source_files) ||
              v.source_files.some((a: unknown) => !a || typeof a !== "object" || Array.isArray(a)))),
        )
      )
        throw new ApiError(200, "Invalid report response or model mismatch.");
      return value;
    },
    async listReviews(status: ReviewStatus, signal?: AbortSignal) {
      if (!["pending", "decided"].includes(status))
        throw new Error("Invalid review filter.");
      return readReviews(
        await request(`/reviews?status=${status}`, { signal }),
        status,
      );
    },
    /** Read the authoritative version-bound checkpoint immediately before a write. */
    async getReview(id: string, signal?: AbortSignal): Promise<ReviewItem> {
      if (!id.trim()) throw new Error("Review ID is required.");
      const value = await request(`/reviews/${encodeURIComponent(id)}`, { signal });
      if (!value || typeof value !== "object" ||
        !("status" in value) || !["pending", "decided"].includes(String(value.status)))
        throw new ApiError(200, "Invalid review response.");
      const review = readReviews([value], value.status as ReviewStatus)[0];
      if (review.id !== id) throw new ApiError(200, "Backend returned another review.");
      return review;
    },
    /** Explicit opening records an auditable review start, not a decision. */
    async openReview(id: string, signal?: AbortSignal): Promise<{ opened_at: string; measurement: string }> {
      if (!id.trim()) throw new Error("Review ID is required.");
      const value = await request(`/reviews/${encodeURIComponent(id)}/open`, {
        method: "POST", signal,
      });
      if (!value || typeof value !== "object" ||
        !("review_id" in value) || value.review_id !== id ||
        !("opened_at" in value) || typeof value.opened_at !== "string" ||
        !Number.isFinite(Date.parse(value.opened_at)) ||
        !("measurement" in value) || typeof value.measurement !== "string")
        throw new ApiError(200, "Invalid review-open acknowledgment; check Backend history.");
      return value as { opened_at: string; measurement: string };
    },
    /** One user-confirmed write; no retry or client-asserted reviewer identity. */
    async decideReview(
      review: ReviewItem,
      draft: ReviewDraft,
      signal?: AbortSignal,
    ): Promise<ReviewItem> {
      if (review.status !== "pending" ||
        draft.artifact_id !== review.artifact_id ||
        draft.artifact_version !== review.artifact_version)
        throw new Error("Decision must target the pending review's exact artifact/version.");
      const value = await request(`/reviews/${encodeURIComponent(review.id)}/decision`, {
        method: "POST", body: JSON.stringify(draft), signal,
      });
      const decided = readReviews([value], "decided")[0];
      if (!sameReviewCheckpoint(review, decided) || decided.resolution === null ||
        decided.resolution.decision !== draft.decision ||
        decided.resolution.rationale !== draft.rationale ||
        decided.resolution.conditions.length !== draft.conditions.length ||
        decided.resolution.conditions.some((condition, i) => condition !== draft.conditions[i]))
        throw new ApiError(200, "Decision response differs from the submitted artifact or payload; reload Backend record.");
      return decided;
    },
    async listModelFiles(modelId: string, signal?: AbortSignal) {
      return readModelFiles(await request(`/models/${encodeURIComponent(modelId)}/files`, { signal }));
    },
    async getModelFile(modelId: string, file: ModelFile, signal?: AbortSignal) {
      const current = readModelFiles(await request(`/models/${encodeURIComponent(modelId)}/files`, { signal }));
      if (!current.some(row => row.path === file.path && row.sha256 === file.sha256))
        throw new Error("Source file changed or is no longer authorized. Reload the file list.");
      return readMarkdownResponse(await responseFor(modelFileRoute(modelId, file.path), {
        signal, headers: { Accept: "text/plain" },
      }));
    },
    async getDocumentText(id: string, signal?: AbortSignal) {
      const response = await responseFor(
        `/artifacts/${encodeURIComponent(id)}/download`,
        {
          signal,
          headers: {
            Accept: "text/markdown, text/plain, application/octet-stream",
          },
        },
      );
      return readMarkdownResponse(response);
    },
    async getValidationEvidence(id: string, signal?: AbortSignal) {
      const response = await responseFor(
        `/artifacts/${encodeURIComponent(id)}/download`,
        {
          signal,
          headers: { Accept: "application/json" },
        },
      );
      return readValidationEvidence(await response.json());
    },
    async downloadReportMarkdown(id: string, signal?: AbortSignal): Promise<Blob> {
      const response = await responseFor(`/reports/${encodeURIComponent(id)}/markdown`, {
        signal, headers: { Accept: "text/markdown" },
      });
      if (response.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "text/markdown")
        throw new Error("The report export is not Markdown. Try again or download the original artifact.");
      return response.blob();
    },
    async downloadArtifact(id: string, signal?: AbortSignal): Promise<Blob> {
      return (
        await responseFor(`/artifacts/${encodeURIComponent(id)}/download`, {
          signal,
        })
      ).blob();
    },
    async listPackages(
      modelId: string,
      signal?: AbortSignal,
    ): Promise<PackageRead[]> {
      const value = await request(
        `/models/${encodeURIComponent(modelId)}/packages`,
        { signal },
      );
      if (!Array.isArray(value))
        throw new ApiError(200, "Expected a package array.");
      return value.map((v) => readPackage(v, modelId));
    },
    async registerPackage(
      modelId: string,
      name: string,
      signal?: AbortSignal,
    ): Promise<PackageRead> {
      if (!name.trim()) throw new Error("Package name is required.");
      return readPackage(
        await request(`/models/${encodeURIComponent(modelId)}/packages`, {
          method: "POST",
          body: JSON.stringify({ package_name: name.trim() }),
          signal,
        }),
        modelId,
      );
    },
    async listScoring(
      packageId: string,
      offset = 0,
      signal?: AbortSignal,
    ): Promise<ScoringRead[]> {
      if (!Number.isInteger(offset) || offset < 0)
        throw new RangeError("Invalid scoring history offset.");
      const value = await request(
        `/packages/${encodeURIComponent(packageId)}/scoring-runs?offset=${offset}&limit=20`,
        { signal },
      );
      if (!Array.isArray(value))
        throw new ApiError(200, "Expected a scoring history array.");
      return value.map((v) => readScoring(v, packageId));
    },
    async startScoring(
      packageId: string,
      signal?: AbortSignal,
    ): Promise<ScoringRead> {
      return readScoring(
        await request(
          `/packages/${encodeURIComponent(packageId)}/scoring-runs`,
          {
            method: "POST",
            signal,
          },
        ),
        packageId,
      );
    },
    async getScoring(
      packageId: string,
      executionId: string,
      signal?: AbortSignal,
    ): Promise<ScoringRead> {
      return readScoring(
        await request(`/scoring-runs/${encodeURIComponent(executionId)}`, {
          signal,
        }),
        packageId,
        executionId,
      );
    },
    async downloadPredictions(
      executionId: string,
      signal?: AbortSignal,
    ): Promise<Blob> {
      const response = await responseFor(
        `/scoring-runs/${encodeURIComponent(executionId)}/predictions`,
        {
          signal,
          headers: { Accept: "text/csv" },
        },
      );
      if (
        !response.headers
          .get("Content-Type")
          ?.toLowerCase()
          .includes("text/csv")
      )
        throw new ApiError(
          response.status,
          "Expected a predictions CSV download.",
        );
      return response.blob();
    },
    async listRuns(
      modelId: string,
      offset = 0,
      limit = 20,
      signal?: AbortSignal,
    ): Promise<RunRead[]> {
      if (!modelId.trim()) throw new Error("Model ID is required.");
      if (
        !Number.isInteger(offset) ||
        offset < 0 ||
        !Number.isInteger(limit) ||
        limit < 1 ||
        limit > 100
      )
        throw new RangeError(
          "Offset must be nonnegative; limit must be 1–100.",
        );
      const query = new URLSearchParams({
        model_id: modelId,
        offset: String(offset),
        limit: String(limit),
      });
      const value = await request(`/runs?${query}`, { signal });
      if (!Array.isArray(value))
        throw new ApiError(200, "Backend run list must be an array.");
      const runs = value.map(readRun);
      if (runs.some((run) => run.model_id !== modelId))
        throw new ApiError(200, "Backend returned runs for another model.");
      return runs;
    },
    async getRun(id: string, signal?: AbortSignal): Promise<RunRead> {
      if (!id.trim()) throw new Error("Run ID is required.");
      const run = readRun(
        await request(`/runs/${encodeURIComponent(id)}`, { signal }),
      );
      if (run.id !== id)
        throw new ApiError(200, "Backend returned another run.");
      return run;
    },
    async getRunCheckpoint(id: string, signal?: AbortSignal) {
      const value = await request(`/runs/${encodeURIComponent(id)}/result`, { signal });
      if (!value || typeof value !== "object" || !("run_id" in value) || value.run_id !== id || !("result" in value))
        throw new Error("Invalid run result identity.");
      const result = value.result;
      if (result === null) return null;
      if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error("Invalid run result.");
      const fields = result as Record<string, unknown>;
      if (fields.status !== "awaiting_review" ||
          typeof fields.checkpoint_id !== "string" || !fields.checkpoint_id.trim() ||
          typeof fields.task_id !== "string" || !fields.task_id.trim()) return null;
      return { reviewId: fields.checkpoint_id, taskId: fields.task_id };
    },
    async resumeRun(run: RunRead, reviewId: string, signal?: AbortSignal): Promise<RunRead> {
      const value = readRun(await request(`/runs/${encodeURIComponent(run.id)}/resume`, {
        method: "POST", body: JSON.stringify({ review_id: reviewId }), signal,
      }));
      if (value.id !== run.id || value.model_id !== run.model_id ||
          value.model_version !== run.model_version || value.operation !== run.operation ||
          value.dataset_id !== run.dataset_id)
        throw new Error("Continuation returned a different run or model version. Refresh saved state.");
      return value;
    },
    /** Official parity history for a candidate, or for every candidate of a parent model. */
    async listParityRuns(modelId: string, signal?: AbortSignal, offset = 0, limit = 20): Promise<ParityRun[]> {
      if (!modelId.trim()) throw new Error("Model ID is required.");
      try {
        return readParityRuns(
          await request(`/models/${encodeURIComponent(modelId)}/parity-runs?offset=${offset}&limit=${limit}`, { signal }),
          modelId,
        );
      } catch (error) {
        if (error instanceof ApiError) throw error;
        throw new ApiError(200, (error as Error).message);
      }
    },
    /** Run official parity once: the same check the rebuild agent runs. Never retried. */
    async runParity(candidateId: string, version: string, signal?: AbortSignal): Promise<ParityRun> {
      if (!candidateId.trim() || !version.trim())
        throw new Error("Candidate model and version are required.");
      const value = await request(
        `/models/${encodeURIComponent(candidateId)}/versions/${encodeURIComponent(version)}/parity`,
        { method: "POST", signal },
      );
      let run: ParityRun;
      try {
        run = readParityRun(value);
      } catch (error) {
        throw new ApiError(200, (error as Error).message);
      }
      if (run.candidate_model_id !== candidateId || run.candidate_version !== version)
        throw new ApiError(200, "Backend returned parity for another candidate.");
      return run;
    },
    async startRun(
      modelId: string,
      payload: RunCreate,
      signal?: AbortSignal,
    ): Promise<RunRead> {
      if (!modelId.trim()) throw new Error("Model ID is required.");
      const run = readRun(
        await request(`/models/${encodeURIComponent(modelId)}/runs`, {
          method: "POST",
          body: JSON.stringify(payload),
          signal,
        }),
      );
      if (
        run.model_id !== modelId ||
        run.model_version !== payload.model_version ||
        run.dataset_id !== payload.dataset_id ||
        run.operation !== payload.operation
      )
        throw new ApiError(
          200,
          "Backend run does not match the submitted request.",
        );
      return run;
    },
    async listFindings(modelId: string, offset = 0, signal?: AbortSignal) {
      if (!Number.isInteger(offset) || offset < 0) throw new Error("Invalid finding page.");
      const value = await request(`/findings?model_id=${encodeURIComponent(modelId)}&offset=${offset}&limit=20`, { signal });
      if (!Array.isArray(value) || value.length > 20) throw new Error("Invalid findings list.");
      const rows = value.map(row => readFinding(row, modelId));
      if (new Set(rows.map(row => row.id)).size !== rows.length) throw new Error("Duplicate finding IDs.");
      return rows;
    },
    async findingOwners(signal?: AbortSignal) {
      return readFindingOwners(await request("/findings/owners", { signal }));
    },
    async updateFinding(shown: Finding, patch: FindingPatch, offset: number, signal?: AbortSignal) {
      const value = await request(`/findings?model_id=${encodeURIComponent(shown.model_id)}&offset=${offset}&limit=20`, { signal });
      if (!Array.isArray(value)) throw new Error("Cannot verify current finding.");
      const current = value.map(row => readFinding(row, shown.model_id)).find(row => row.id === shown.id);
      if (!current || !sameFinding(current, shown)) throw new Error("Finding changed or moved. Refresh findings before editing again.");
      const saved = readFinding(await request(`/findings/${encodeURIComponent(shown.id)}`, {
        method: "PATCH", body: JSON.stringify(patch), signal,
      }), shown.model_id);
      if (saved.id !== shown.id || saved.report_id !== shown.report_id || saved.check_id !== shown.check_id ||
          (patch.status !== undefined && saved.status !== patch.status) ||
          (patch.owner_id !== undefined && saved.owner_id !== patch.owner_id) ||
          (patch.due_date !== undefined && (patch.due_date === null ? saved.due_date !== null :
            !saved.due_date || Date.parse(saved.due_date) !== Date.parse(patch.due_date))) ||
          saved.comments.length <= shown.comments.length || saved.comments.at(-1)?.comment !== patch.comment)
        throw new Error("Finding update response differs from the request. Refresh saved findings; do not retry automatically.");
      return saved;
    },
    async listModelHistory(modelId: string, offset = 0, signal?: AbortSignal) {
      if (!modelId.trim() || !Number.isInteger(offset) || offset < 0) throw new Error("Invalid model history page.");
      return readAuditEvents(await request(`/models/${encodeURIComponent(modelId)}/history?offset=${offset}&limit=20`, { signal }));
    },
    async listAccounts(signal?: AbortSignal) {
      return readAccounts(await request("/auth/users", { signal }));
    },
    async createAccount(username: string, password: string, role: Account["role"], signal?: AbortSignal) {
      if (!username.trim() || username.length > 100 || password.length < 12 || password.length > 256 || !accountRoles.includes(role))
        throw new Error("Invalid account fields.");
      const saved = readAccount(await request("/auth/users", {
        method: "POST", body: JSON.stringify({ username: username.trim(), password, role }), signal,
      }));
      if (saved.role !== role || !saved.active) throw new Error("Unexpected account result. Refresh saved accounts.");
      return saved;
    },
    async disableAccount(shown: Account, actorId: string, signal?: AbortSignal) {
      if (shown.id === actorId || !shown.active) throw new Error("Cannot disable this account.");
      const rows = readAccounts(await request("/auth/users", { signal }));
      const current = rows.find(row => row.id === shown.id);
      if (!current || current.username !== shown.username || current.role !== shown.role || !current.active)
        throw new Error("Account changed. Refresh accounts before confirming again.");
      const saved = readAccount(await request(`/auth/users/${encodeURIComponent(shown.id)}/disable`, { method: "POST", signal }));
      if (saved.id !== shown.id || saved.username !== shown.username || saved.role !== shown.role || saved.active)
        throw new Error("Unexpected disable result. Refresh saved accounts.");
      return saved;
    },
    async getDashboard(signal?: AbortSignal): Promise<DashboardSummary> {
      return readDashboard(await request("/dashboard", { signal }));
    },
    async listPortfolioMonitoring(
      offset = 0, limit = 20, signal?: AbortSignal,
    ): Promise<PortfolioMonitoringRow[]> {
      if (!Number.isInteger(offset) || offset < 0 || !Number.isInteger(limit) ||
        limit < 1 || limit > 100) throw new RangeError("Invalid monitoring portfolio page.");
      const query = new URLSearchParams({ offset: String(offset), limit: String(limit) });
      return readPortfolioMonitoring(await request(`/monitoring?${query}`, { signal }));
    },
    async listModelMonitoring(
      modelId: string, offset = 0, limit = 20, signal?: AbortSignal,
    ): Promise<MonitoringEvent[]> {
      if (!modelId.trim()) throw new Error("Model ID required for monitoring.");
      if (!Number.isInteger(offset) || offset < 0 || !Number.isInteger(limit) ||
        limit < 1 || limit > 100) throw new RangeError("Invalid monitoring history page.");
      const query = new URLSearchParams({ offset: String(offset), limit: String(limit) });
      return readMonitoringEvents(await request(
        `/models/${encodeURIComponent(modelId)}/monitoring?${query}`, { signal },
      ), modelId);
    },
    /** Persisted run request: intentionally no retries; a 503 can include a durable runId. */
    async simulateMonitoring(
      modelId: string, feature: string, factor: number, windowLabel: string,
      signal?: AbortSignal,
    ): Promise<RunRead> {
      if (!modelId.trim() || !feature.trim() || feature.length > 100 ||
        !Number.isFinite(factor) || factor <= 0 || factor > 100 ||
        !windowLabel.trim() || windowLabel.length > 120)
        throw new Error("Invalid declared PSI simulation inputs.");
      const run = readRun(await request(
        `/models/${encodeURIComponent(modelId)}/monitoring/simulate`, {
          method: "POST",
          body: JSON.stringify({ feature, factor, window_label: windowLabel.trim() }),
          signal,
        },
      ));
      if (run.model_id !== modelId || run.operation !== "monitor")
        throw new ApiError(200, "Backend returned another monitoring execution.");
      return run;
    },
    /** CSV bytes are sent as multipart, never as an aggregate PSI number. */
    async submitMonitoringBatch(
      modelId: string, file: File, windowLabel: string,
      onProgress: (pct: number | null) => void, signal?: AbortSignal,
    ): Promise<RunRead> {
      if (!modelId.trim() || !file.name.toLowerCase().endsWith(".csv") ||
        file.size === 0 || file.size > 20_000_000 ||
        !windowLabel.trim() || windowLabel.length > 120)
        throw new Error("Select a non-empty CSV (max 20 MB) and a window label (max 120 characters).");
      const form = new FormData();
      form.append("batch", file);
      form.append("window_label", windowLabel.trim());
      const run = readRun(await multipart(
        base + `/models/${encodeURIComponent(modelId)}/monitoring/batches`,
        form, onProgress, signal,
      ));
      if (run.model_id !== modelId || run.operation !== "monitor")
        throw new ApiError(200, "Backend returned another monitoring batch execution.");
      return run;
    },
    async createConversation(signal?: AbortSignal): Promise<string> {
      const data = await request("/conversations", {
        method: "POST",
        body: "{}",
        signal,
      });
      if (
        !data ||
        typeof data !== "object" ||
        !("id" in data) ||
        typeof data.id !== "string" ||
        !data.id.trim()
      )
        throw new ApiError(
          200,
          "Conversation ID response contract needs backend agreement.",
        );
      return data.id;
    },
    async sendMessage(
      id: string,
      message: string,
      context: { model_id: string | null },
      onText: (text: string) => void,
      onQuestion?: (question: ChatQuestion) => void,
      signal?: AbortSignal,
      onNotice?: (notice: ChatNotice) => void,
    ): Promise<void> {
      if (!id.trim() || !message.trim())
        throw new Error("Conversation ID and message are required.");
      const response = await responseFor(
        `/conversations/${encodeURIComponent(id)}/messages`,
        {
          method: "POST",
          headers: { Accept: "text/event-stream" },
          body: JSON.stringify({ message, context }),
          signal,
        },
      );
      await consumeChat(response, onText, onQuestion, onNotice);
    },
    async list(
      offset = 0,
      limit = 50,
      signal?: AbortSignal,
    ): Promise<ModelRecord[]> {
      if (
        !Number.isInteger(offset) ||
        offset < 0 ||
        !Number.isInteger(limit) ||
        limit < 1 ||
        limit > 100
      )
        throw new RangeError(
          "Offset must be nonnegative; limit must be 1–100.",
        );
      const result = await request(`/models?offset=${offset}&limit=${limit}`, {
        signal,
      });
      if (!Array.isArray(result))
        throw new ApiError(200, "Backend model list must be an array.");
      return result.map(record);
    },
    async get(id: string, signal?: AbortSignal): Promise<ModelRecord> {
      if (!id.trim()) throw new Error("Model ID is required.");
      return record(
        await request(`/models/${encodeURIComponent(id)}`, { signal }),
      );
    },
    async create(
      model: ModelCreate,
      signal?: AbortSignal,
    ): Promise<ModelRecord> {
      // Never retry writes automatically: the scaffold has no idempotency contract.
      return record(
        await request("/models", {
          method: "POST",
          body: JSON.stringify(model),
          signal,
        }),
      );
    },
  };
}
