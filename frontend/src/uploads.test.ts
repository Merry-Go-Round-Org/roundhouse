import { expect, it, vi } from "vitest";
import { readUpload, uploadError, multipart } from "./uploads";
const state = {
  id: "u1",
  status: "completed",
  model_id: "m1",
  error: null,
  files: ["manifest.json"],
  checks: [
    { name: "Hash", status: "passed", blocking: true, reason: "Matched" },
  ],
};
it("does not call accepted transfers complete", () => {
  expect(
    readUpload({ ...state, status: "queued", model_id: null, checks: [] })
      .status,
  ).toBe("queued");
});
it("rejects completion with failed or unfinished blocking checks", () => {
  for (const status of ["failed", "pending", "running"]) {
    expect(() =>
      readUpload({ ...state, checks: [{ ...state.checks[0], status }] }),
    ).toThrow("completion");
  }
  expect(() => readUpload({ ...state, model_id: null })).toThrow("completion");
});
it("retains nonblocking reproduction failures on an admitted model", () => {
  expect(
    readUpload({
      ...state,
      checks: [{ ...state.checks[0], status: "failed", blocking: false }],
    }).checks[0].status,
  ).toBe("failed");
});
it("rejects malformed response shapes", () => {
  for (const v of [null, {}, { ...state, checks: [{ name: "unchecked" }] }])
    expect(() => readUpload(v)).toThrow();
});
it("shows structured upload errors without reporting success", () => {
  expect(uploadError(501, null).message).toContain("not implemented");
  expect(
    uploadError(422, { detail: [{ msg: "Invalid manifest" }] }).message,
  ).toBe("Invalid manifest");
  expect(uploadError(0, null).message).toContain("uncertain");
});


it("accepts the Backend's explicit NOT_APPLICABLE upload check without calling it a pass", () => {
  const c = { name: "Binary reproduction", status: "not_applicable", blocking: false,
    reason: "Synthetic Cox model; no binary scoring performed" };
  const result = readUpload({ ...state, checks: [state.checks[0], c] });
  expect(result.status).toBe("completed");
  expect(result.checks[1].status).toBe("not_applicable");
  expect(result.checks[1].reason).toContain("Synthetic");
});

it("preserves the saved archive ID when dispatch fails after upload", () => {
  expect(uploadError(503, { detail: { archive_id: "saved-archive", message: "Dispatch unavailable" } })).toMatchObject({ archiveId: "saved-archive", message: "Dispatch unavailable" });
});
it("reports transfer progress without resolving before the server acknowledgement", async () => {
  const xhr = { upload: {} as { onprogress: (e: { lengthComputable: boolean; loaded: number; total: number }) => void }, open: vi.fn(), send: vi.fn(), abort: vi.fn(), onload: () => {}, status: 202, responseText: '{"status":"queued"}', withCredentials: false, timeout: 0 };
  vi.stubGlobal("XMLHttpRequest", function () { return xhr; });
  try {
    const progress = vi.fn(), settled = vi.fn();
    const pending = multipart("/api/v1/intake/archives", new FormData(), progress).then(settled);
    xhr.upload.onprogress({ lengthComputable: false, loaded: 0, total: 0 });
    expect(progress).toHaveBeenLastCalledWith(null);
    xhr.upload.onprogress({ lengthComputable: true, loaded: 100, total: 100 });
    expect(progress).toHaveBeenLastCalledWith(100);
    await Promise.resolve();
    expect(settled).not.toHaveBeenCalled();
    expect(xhr.withCredentials).toBe(true);
    xhr.onload();
    await pending;
    expect(settled).toHaveBeenCalledWith({ status: "queued" });
    expect(xhr.send).toHaveBeenCalledTimes(1);
  } finally { vi.unstubAllGlobals(); }
});
