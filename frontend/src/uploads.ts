import { notifySessionExpired } from "./auth";
/** Current Backend upload/check contract. UI submission stays opt-in until full E2E acceptance. */
export interface UploadCheck {
  name: string;
  status: "pending" | "running" | "passed" | "failed" | "not_applicable";
  blocking: boolean;
  reason: string;
}
export interface UploadState {
  id: string;
  status: "queued" | "running" | "completed" | "failed";
  checks: UploadCheck[];
  files: string[];
  model_id: string | null;
  error: string | null;
}
export function readUpload(v: unknown): UploadState {
  if (!v || typeof v !== "object") throw new Error("Invalid upload response.");
  const r = v as UploadState;
  if (
    typeof r.id !== "string" ||
    !r.id.trim() ||
    !["queued", "running", "completed", "failed"].includes(r.status) ||
    !Array.isArray(r.files) ||
    !r.files.every((f) => typeof f === "string") ||
    !(
      r.model_id === null ||
      (typeof r.model_id === "string" && !!r.model_id.trim())
    ) ||
    !(r.error === null || typeof r.error === "string") ||
    !Array.isArray(r.checks) ||
    r.checks.some(
      (c) =>
        !c ||
        typeof c.name !== "string" ||
        typeof c.reason !== "string" ||
        typeof c.blocking !== "boolean" ||
        !["pending", "running", "passed", "failed", "not_applicable"].includes(c.status),
    )
  )
    throw new Error("Upload response does not match the backend contract.");
  if (
    r.status === "completed" &&
    (!r.model_id ||
      !r.checks.length ||
      r.checks.some(
        (c) =>
          c.status === "pending" ||
          c.status === "running" ||
          (c.blocking && c.status === "failed"),
      ))
  )
    throw new Error(
      "Upload completion lacks a model or contains unresolved blocking checks.",
    );
  return r;
}
export function uploadError(status: number, value: unknown): Error {
  const detail =
    value && typeof value === "object" && "detail" in value
      ? value.detail
      : undefined;
  let message =
    status === 501
      ? "Upload is not implemented by this backend."
      : status === 0
        ? "Upload connection failed; its outcome may be uncertain."
        : `Upload failed (${status}).`;
  if (typeof detail === "string") message = detail;
  else if (
    detail &&
    typeof detail === "object" &&
    "message" in detail &&
    typeof detail.message === "string"
  )
    message = detail.message;
  else if (Array.isArray(detail))
    message =
      detail
        .map((d) => (typeof d?.msg === "string" ? d.msg : ""))
        .filter(Boolean)
        .join("; ") || message;
  const error: Error & { archiveId?: string; status: number } = Object.assign(new Error(message), { status });
  if (detail && typeof detail === "object" && "archive_id" in detail && typeof detail.archive_id === "string")
    error.archiveId = detail.archive_id;
  return error;
}
export function multipart(
  url: string,
  form: FormData,
  onProgress: (percent: number | null) => void,
  signal?: AbortSignal,
): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const cleanup = () => signal?.removeEventListener("abort", abort);
    const abort = () => {
      xhr.abort();
      cleanup();
      reject(
        new DOMException(
          "Upload stopped locally; backend processing may continue.",
          "AbortError",
        ),
      );
    };
    if (signal?.aborted) {
      abort();
      return;
    }
    xhr.open("POST", url);
    xhr.withCredentials = true;
    xhr.timeout = 120000;
    xhr.upload.onprogress = (e) =>
      onProgress(
        e.lengthComputable ? Math.round((e.loaded / e.total) * 100) : null,
      );
    xhr.onload = () => {
      cleanup();
      let data: unknown;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        reject(
          new Error(`Backend returned invalid upload JSON (${xhr.status}).`),
        );
        return;
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        if (xhr.status === 401) notifySessionExpired();
        reject(uploadError(xhr.status, data));
        return;
      }
      resolve(data);
    };
    xhr.onerror = () => {
      cleanup();
      reject(uploadError(0, null));
    };
    xhr.ontimeout = () => {
      cleanup();
      reject(
        new Error("Upload timed out; inspect server state before retrying."),
      );
    };
    signal?.addEventListener("abort", abort, { once: true });
    xhr.send(form); // Browser sets multipart boundary; no JSON header or automatic write retry.
  });
}
