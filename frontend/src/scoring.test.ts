import { expect, it, vi } from "vitest";
import { createModelClient } from "./api";
import { readScoring } from "./scoringTypes";
const pkg = {
  id: "p/1",
  model_id: "m/1",
  name: "credit_logistic_r",
  version: "1.0.0",
  language: "R",
  dataset_id: "uci_credit",
  sha256: "a".repeat(64),
  input_sha256: "b".repeat(64),
  n_rows: 100,
};
const run = {
  id: "r/1",
  package_id: pkg.id,
  status: "queued",
  created_at: "2026-11-05T12:00:00Z",
  started_at: null,
  finished_at: null,
  error: null,
  result: null,
};
it("registers and restores package identities with encoded model paths", async () => {
  const transport = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(Response.json(pkg))
    .mockResolvedValueOnce(Response.json([pkg]));
  const api = createModelClient("/custom", transport);
  expect(await api.registerPackage(pkg.model_id, pkg.name)).toEqual(pkg);
  expect(transport.mock.calls[0]).toEqual([
    "/custom/models/m%2F1/packages",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ package_name: pkg.name }),
    }),
  ]);
  expect(await api.listPackages(pkg.model_id)).toEqual([pkg]);
});
it("submits scoring without a body, preserves signals, and restores history", async () => {
  const transport = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(Response.json(run, { status: 202 }))
    .mockResolvedValueOnce(Response.json([run]))
    .mockResolvedValueOnce(Response.json(run));
  const api = createModelClient("/custom", transport),
    signal = new AbortController().signal;
  expect(await api.startScoring(pkg.id, signal)).toEqual(run);
  expect(transport.mock.calls[0][1]).toMatchObject({ method: "POST", signal });
  expect(transport.mock.calls[0][1]?.body).toBeUndefined();
  await api.listScoring(pkg.id, 20, signal);
  await api.getScoring(pkg.id, run.id, signal);
  expect(transport.mock.calls[1][0]).toBe(
    "/custom/packages/p%2F1/scoring-runs?offset=20&limit=20",
  );
  expect(transport.mock.calls[2][0]).toBe("/custom/scoring-runs/r%2F1");
});
it("rejects unrelated executions and malformed results", () => {
  expect(() => readScoring(run, "other")).toThrow("mismatch");
  expect(() => readScoring(run, pkg.id, "other")).toThrow("mismatch");
  expect(() =>
    readScoring({ ...run, result: { scope: "approved" } }, pkg.id),
  ).toThrow("evidence");
});
it("retains saved execution IDs on dispatch errors without write retries", async () => {
  const transport = vi
    .fn<typeof fetch>()
    .mockResolvedValue(
      Response.json(
        { detail: { code: "dispatch_unavailable", run_id: "saved-job" } },
        { status: 503 },
      ),
    );
  await expect(
    createModelClient("/api/v1", transport).startScoring(pkg.id),
  ).rejects.toMatchObject({ status: 503, runId: "saved-job" });
  expect(transport).toHaveBeenCalledTimes(1);
});
it("checks status and content type before returning CSV bytes", async () => {
  const transport = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(
      Response.json({ detail: "Integrity failed" }, { status: 409 }),
    )
    .mockResolvedValueOnce(
      new Response("oops", { headers: { "Content-Type": "text/html" } }),
    )
    .mockResolvedValueOnce(
      new Response("row_id,positive_probability\n1,0.5", {
        headers: { "Content-Type": "text/csv" },
      }),
    );
  const api = createModelClient("/api/v1", transport);
  await expect(api.downloadPredictions("r")).rejects.toThrow(
    "Integrity failed",
  );
  await expect(api.downloadPredictions("r")).rejects.toThrow("Expected");
  expect(await (await api.downloadPredictions("r")).text()).toContain(
    "positive_probability",
  );
});
it("does not retry an ambiguous scoring write", async () => {
  const transport = vi
    .fn<typeof fetch>()
    .mockRejectedValue(new TypeError("connection lost"));
  await expect(
    createModelClient("/api/v1", transport).startScoring(pkg.id),
  ).rejects.toMatchObject({ status: 0 });
  expect(transport).toHaveBeenCalledTimes(1);
});
