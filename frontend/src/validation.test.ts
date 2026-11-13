import { expect, it, vi } from "vitest";
import {
  checkStatuses,
  readValidationEvidence,
  statusDisplay,
} from "./validation";
import { createModelClient } from "./api";
const evidence = {
  schema_version: "1.0.0",
  report_kind: "legacy_model_validation",
  policy_version: "1.0.0",
  model_id: "legacy-credit",
  domain: "uci",
  task: "binary_classification",
  model_kind: "real_dataset",
  artifact_sha256: null,
  generated_at_utc: "2026-11-05T12:00:00Z",
  execution_requested: false,
  checks: [
    {
      check_id: "parity",
      status: "NOT_APPLICABLE",
      required: false,
      summary: "Independent fit",
      evidence_kind: "saved_evidence",
      details: {},
    },
  ],
  summary: {
    overall_status: "REVIEW_REQUIRED",
    required_checks_passed: true,
    blocking_check_ids: [],
    review_check_ids: [],
    human_approval_granted: false,
  },
  saved_metrics: {},
  limitations: ["No fresh holdout"],
};
it("preserves all canonical statuses without converting N/A or unavailable into PASS", () => {
  expect(statusDisplay("NOT_APPLICABLE")).toEqual({
    label: "NOT_APPLICABLE (N/A)",
    tone: "neutral",
  });
  expect(statusDisplay("NOT_ASSESSABLE").tone).toBe("unavailable");
  expect(statusDisplay("not_run").label).toBe("NOT_RUN");
  expect(statusDisplay("green").label).toBe("PASS");
  expect(readValidationEvidence(evidence).summary.human_approval_granted).toBe(
    false,
  );
});
it("rejects forged approval, invalid checks and unknown blocker identities", () => {
  for (const invalid of [
    {
      ...evidence,
      summary: { ...evidence.summary, human_approval_granted: true },
    },
    { ...evidence, checks: [{ ...evidence.checks[0], status: "APPROVED" }] },
    { ...evidence, checks: [evidence.checks[0], evidence.checks[0]] },
    {
      ...evidence,
      summary: { ...evidence.summary, blocking_check_ids: ["other"] },
    },
    { ...evidence, generated_at_utc: "bad" },
    { ...evidence, checks: [] },
  ])
    expect(() => readValidationEvidence(invalid)).toThrow("Invalid canonical");
});
it("accepts canonical and existing API metric statuses but rejects unknown statuses", async () => {
  const report = {
    id: "r",
    model_id: "m",
    run_id: "run",
    kind: "validation",
    artifact_id: "a",
    metrics: [
      ...checkStatuses,
      "green",
      "amber",
      "red",
      "not_assessable",
      "not_run",
    ].map((status) => ({
      name: status,
      value: null,
      status,
      reason: "Evidence",
      evidence_ids: [],
    })),
  };
  const fetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(Response.json([report]))
    .mockResolvedValueOnce(
      Response.json([
        { ...report, metrics: [{ ...report.metrics[0], status: "approved" }] },
      ]),
    );
  const client = createModelClient("/api/v1", fetcher);
  expect(await client.listReports("m")).toEqual([report]);
  await expect(client.listReports("m")).rejects.toThrow("Invalid report");
});
it("retrieves canonical evidence only through the authorized artifact route", async () => {
  const fetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(Response.json(evidence))
    .mockResolvedValueOnce(
      Response.json({ detail: "Denied" }, { status: 403 }),
    );
  const client = createModelClient("/api/v1", fetcher);
  expect(await client.getValidationEvidence("a/b")).toEqual(evidence);
  expect(fetcher.mock.calls[0][0]).toBe("/api/v1/artifacts/a%2Fb/download");
  await expect(client.getValidationEvidence("a/b")).rejects.toMatchObject({
    status: 403,
  });
});
