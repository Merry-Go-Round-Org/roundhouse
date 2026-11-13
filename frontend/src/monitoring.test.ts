import { describe, it, expect, vi } from "vitest";
import { createModelClient } from "./api";
import {
  bandLabel, readDashboard, readMonitoringEvents, readPortfolioMonitoring,
  readPsiWindow,
} from "./monitoring";
import type { PsiWindow, PsiMetric } from "./monitoring";

const sha = "a".repeat(64);
const metric = (psi: number, band: "green" | "amber" | "red" = "green"): PsiMetric => ({
  status: band === "green" ? "PASS" : "REVIEW",
  band, psi, reference_rows: 200, current_rows: 120,
  reason: "PSI describes marginal drift only",
});
const window: PsiWindow = {
  schema_version: "1.0.0", kind: "model_stability_window",
  model_id: "credit_xgboost_python",
  artifact_sha256: sha, baseline_sha256: sha,
  policy_version: "1.0.0", reference_scope: "development",
  reference_rows: 200, current_rows: 120, status: "REVIEW",
  band: "red", score_band: "green", feature_band: "red",
  revalidation_required: true, revalidation_run_id: "validation-run-1",
  red_features: ["LIMIT_BAL"], score: metric(0.031),
  features: { LIMIT_BAL: metric(0.31, "red"), AGE: metric(0.15, "amber") },
  limitations: [
    "Marginal shift only; no model performance evidence",
    "Project PSI bands are heuristics",
  ],
  simulated: true, shift: { feature: "LIMIT_BAL", factor: 1.5 },
  dataset_version_id: "dataset-version-1", package_sha256: sha,
  runtime_image: "sha256:example", input_scope: "validation_partition_simulation",
  baseline_partition: "train", window_label: "Declared test shift",
};

const event = {
  id: "event-1", model_id: "model-1", run_id: "monitor-run-1",
  created_at: "2026-11-07T13:00:00Z", payload: window,
};
const portfolio = [
  { model_id: "model-1", model_name: "Credit XGBoost",
    lifecycle_status: "revalidation_due", latest: window,
    last_run: "2026-11-07T13:00:00Z" },
  { model_id: "model-2", model_name: "Synthetic Cox",
    lifecycle_status: "development", latest: null, last_run: null },
];
const dashboard = {
  models: 2, packages: 1, reports: 2, monitoring_events: 1,
  runs: { completed: 4, awaiting_review: 1 },
  scoring_runs: {}, reviews: { decided: 1, pending: 1 },
  findings: { open: 2 }, lifecycle: { revalidation_due: 1, development: 1 },
  stage_elapsed_seconds: { monitor: { count: 1, mean: 12, total: 12 } },
  review_elapsed_seconds: { count: 0, mean: null, total: 0 },
  llm_cost_usd: 0.02, red_alerts: 1,
  measurement_limitations: ["Elapsed is not active human effort"],
  source: "persisted_records",
};

describe("persisted P2 monitoring contracts", () => {
  it("preserves current and historical simulation scopes across portfolio and history", () => {
    for (const scope of ["validation_partition_simulation", "test_partition_simulation"] as const) {
      const payload = { ...window, input_scope: scope };
      expect(readMonitoringEvents([{ ...event, payload }], "model-1")[0].payload.input_scope).toBe(scope);
      expect(readPortfolioMonitoring([{ ...portfolio[0], latest: payload }])[0].latest?.input_scope).toBe(scope);
      expect(() => readPsiWindow({ ...payload, simulated: false })).toThrow();
    }
    expect(() => readPsiWindow({ ...window, input_scope: "uploaded_batch" })).toThrow();
    expect(readPsiWindow({ ...window, input_scope: "uploaded_batch", simulated: false, shift: null }).input_scope).toBe("uploaded_batch");
  });
  it("validates actual PSI engine result and keeps simulation distinct from production", () => {
    expect(readPsiWindow(window).band).toBe("red");
    expect(readPsiWindow(window).revalidation_run_id).toBe("validation-run-1");
    expect(readPsiWindow(window).simulated).toBe(true);
    expect(bandLabel("not_assessable")).toBe("NOT ASSESSABLE");
  });
  it("distinguishes NOT_ASSESSABLE from a successful green window", () => {
    const unassessable = {
      ...window, band: "not_assessable", status: "NOT_ASSESSABLE",
      revalidation_required: false, revalidation_run_id: undefined,
      score: { ...metric(0.04), band: "not_assessable", status: "NOT_ASSESSABLE" },
    };
    expect(readPsiWindow(unassessable).status).toBe("NOT_ASSESSABLE");
    expect(() => readPsiWindow({ ...unassessable, revalidation_required: true })).toThrow();
    expect(() => readPsiWindow({ ...window, score_band: "passing" })).toThrow();
    expect(() => readPsiWindow({ ...window, baseline_sha256: "wrong" })).toThrow();
    expect(() => readPsiWindow({ ...window, input_scope: "production", simulated: true })).toThrow();
    expect(() => readPsiWindow({ ...window, red_features: ["missing"] })).toThrow();
  });
  it("rejects cross-model and duplicate events, keeps no-event portfolios as null", () => {
    expect(readMonitoringEvents([event], "model-1")[0].run_id).toBe("monitor-run-1");
    expect(() => readMonitoringEvents([event], "model-2")).toThrow();
    expect(() => readMonitoringEvents([event, event], "model-1")).toThrow();
    expect(readPortfolioMonitoring(portfolio)[1].latest).toBeNull();
    expect(() => readPortfolioMonitoring([{ ...portfolio[1], last_run: "2026-11-07T13:00:00Z" }])).toThrow();
  });
  it("parses real persisted dashboard counters without fabricating savings", () => {
    const summary = readDashboard(dashboard);
    expect(summary.monitoring_events).toBe(1);
    expect(summary.review_elapsed_seconds.mean).toBeNull();
    expect(() => readDashboard({ ...dashboard, red_alerts: "1" })).toThrow();
    expect(() => readDashboard({ ...dashboard, llm_cost_usd: Number.NaN })).toThrow();
    expect(() => readDashboard({ ...dashboard, source: "simulated" })).toThrow();
  });
  it("uses authenticated read endpoints, offset pagination and strict model binding", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json([event]))
      .mockResolvedValueOnce(Response.json(portfolio))
      .mockResolvedValueOnce(Response.json(dashboard));
    const client = createModelClient("/api/v1", fetcher);
    const signal = new AbortController().signal;
    expect(await client.listModelMonitoring("model-1", 20, 20, signal)).toHaveLength(1);
    expect(await client.listPortfolioMonitoring(0, 20, signal)).toHaveLength(2);
    expect((await client.getDashboard(signal)).source).toBe("persisted_records");
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/models/model-1/monitoring?offset=20&limit=20",
      "/api/v1/monitoring?offset=0&limit=20",
      "/api/v1/dashboard",
    ]);
    for (const [, init] of fetcher.mock.calls)
      expect(init?.credentials).toBe("include");
  });
  it("sends explicit simulation inputs as one POST and verifies the run identity", async () => {
    const run = {
      id: "run-1", model_id: "model-1", model_version: "1.0.0", dataset_id: "uci_credit",
      operation: "monitor", status: "queued", stage: null, error: null,
      created_at: "2026-11-07T13:00:00Z", started_at: null, finished_at: null,
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(run));
    const client = createModelClient("/api/v1", fetcher);
    expect((await client.simulateMonitoring("model-1", "LIMIT_BAL", 1.5, "Declared shift")).id).toBe("run-1");
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][0]).toBe("/api/v1/models/model-1/monitoring/simulate");
    expect(JSON.parse(fetcher.mock.calls[0][1]?.body as string)).toEqual({
      feature: "LIMIT_BAL", factor: 1.5, window_label: "Declared shift",
    });
    expect(fetcher.mock.calls[0][1]?.method).toBe("POST");
    await expect(client.simulateMonitoring("model-1", "LIMIT_BAL", Number.NaN, "Test")).rejects.toThrow("Invalid");
    expect(fetcher).toHaveBeenCalledTimes(1);
    const mismatch = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ ...run, model_id: "another" }));
    await expect(createModelClient("/api/v1", mismatch).simulateMonitoring("model-1", "AGE", 2, "Test"))
      .rejects.toThrow("another monitoring execution");
    for (const code of [403, 409, 422, 503]) {
      const failed = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: code }));
      await expect(createModelClient("/api/v1", failed).simulateMonitoring("model-1", "AGE", 2, "Test"))
        .rejects.toMatchObject({ status: code });
      expect(failed).toHaveBeenCalledTimes(1);
    }
  });
});
