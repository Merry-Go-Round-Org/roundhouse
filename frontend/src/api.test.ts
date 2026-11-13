import { describe, it, expect, vi } from "vitest";
import {
  createModelClient,
  readRun,
  type RunRead,
  type ModelRecord,
} from "./api";
const run: RunRead = {
  id: "r-1",
  model_id: "m-1",
  model_version: "v1",
  dataset_id: "uci_credit",
  operation: "validate",
  status: "queued",
  created_at: "2026-11-04T12:00:00Z",
  started_at: null,
  finished_at: null,
  stage: null,
  error: null,
};
it("submits the issue #5 run payload and polls with the shared base and signal", async () => {
  const fetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(Response.json({ ...run, model_id: "a/b" }))
    .mockResolvedValueOnce(Response.json({ ...run, id: "r/1" }));
  const client = createModelClient("/custom/", fetcher);
  const signal = new AbortController().signal;
  const input = {
    operation: run.operation,
    model_version: run.model_version,
    dataset_id: run.dataset_id,
  };
  expect(await client.startRun("a/b", input, signal)).toEqual({ ...run, model_id: "a/b" });
  expect(fetcher.mock.calls[0]).toEqual([
    "/custom/models/a%2Fb/runs",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify(input),
      signal,
    }),
  ]);
  await client.getRun("r/1", signal);
  expect(fetcher.mock.calls[1]).toEqual([
    "/custom/runs/r%2F1",
    expect.objectContaining({ signal }),
  ]);
});
it("rejects incomplete run schemas and invalid timestamps", () => {
  expect(() => readRun({ ...run, model_version: undefined })).toThrow("schema");
  expect(() => readRun({ ...run, started_at: "yesterday" })).toThrow("schema");
});
it("surfaces run errors without retrying submission", async () => {
  for (const status of [404, 422, 501]) {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response(null, { status }));
    await expect(
      createModelClient("/api/v1", fetcher).startRun("m", run),
    ).rejects.toMatchObject({ status });
    expect(fetcher).toHaveBeenCalledTimes(1);
  }
});
it("routes chat creation and streaming through the existing client", async () => {
  const fetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(Response.json({ id: "c/1" }))
    .mockResolvedValueOnce(
      new Response(
        'data: {"event":"token","text":"Hello"}\n\ndata: {"event":"done"}\n\n',
        { headers: { "Content-Type": "text/event-stream" } },
      ),
    );
  const client = createModelClient("/custom", fetcher);
  const signal = new AbortController().signal;
  const id = await client.createConversation(signal);
  let reply = "";
  await client.sendMessage(
    id,
    "Help",
    { model_id: "m" },
    (text) => (reply += text),
    undefined,
    signal,
  );
  expect(reply).toBe("Hello");
  expect(fetcher.mock.calls[1]).toEqual([
    "/custom/conversations/c%2F1/messages",
    expect.objectContaining({
      signal,
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({ message: "Help", context: { model_id: "m" } }),
    }),
  ]);
});
const model: ModelRecord = {
  id: "m-1",
  name: "Credit model",
  version: "v1",
  language: "R",
  dataset_id: "uci_credit",
  purpose: "Predict next-month default",
  owner: null,
  lifecycle_status: "development",
  created_at: "2026-11-03T12:00:00Z",
};
describe("backend model contract", () => {
  it("uses backend pagination and preserves nullable ownership", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json([model]));
    expect(await createModelClient("/api/v1/", fetcher).list(50, 25)).toEqual([
      model,
    ]);
    expect(fetcher.mock.calls[0][0]).toBe("/api/v1/models?offset=50&limit=25");
  });
  it("rejects invalid pagination before requesting", async () => {
    const fetcher = vi.fn<typeof fetch>();
    await expect(
      createModelClient("/api/v1", fetcher).list(0, 101),
    ).rejects.toThrow(RangeError);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("does not replace malformed evidence with demo data", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json([{ ...model, dataset_id: "unknown" }]));
    await expect(createModelClient("/api/v1", fetcher).list()).rejects.toThrow(
      "schema",
    );
  });
  it("surfaces not-implemented and conflicts distinctly", async () => {
    for (const [status, message] of [
      [501, "not implemented"],
      [409, "changed"],
    ] as const) {
      const fetcher = vi
        .fn<typeof fetch>()
        .mockResolvedValue(new Response("", { status }));
      await expect(
        createModelClient("/api/v1", fetcher).get("m-1"),
      ).rejects.toThrow(message);
    }
  });
  it("encodes model IDs as one path segment", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json(model));
    await createModelClient("/api/v1", fetcher).get("a/b");
    expect(fetcher.mock.calls[0][0]).toBe("/api/v1/models/a%2Fb");
  });
  it("does not retry a failed write", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockRejectedValue(new TypeError("Network error"));
    await expect(
      createModelClient("/api/v1", fetcher).create({ ...model, dataset_id: "uci_credit" }),
    ).rejects.toThrow("Cannot reach");
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("preserves cancellation", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockRejectedValue(new DOMException("Cancelled", "AbortError"));
    await expect(
      createModelClient("/api/v1", fetcher).list(),
    ).rejects.toMatchObject({ name: "AbortError" });
  });
});

describe("run connection", () => {
  const run = {
    id: "run-1", model_id: "m-1", model_version: "v1", dataset_id: "uci_credit",
    operation: "validate", status: "queued", created_at: "2026-11-04T10:00:00Z",
    started_at: null, finished_at: null, stage: null, error: null,
  };
  it("lists model-specific runs with pagination", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json([run]));
    expect(await createModelClient("/api/v1", fetcher).listRuns("m-1", 20, 20)).toEqual([run]);
    expect(fetcher.mock.calls[0][0]).toBe("/api/v1/runs?model_id=m-1&offset=20&limit=20");
  });
  it("surfaces 503 details without retrying submission", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({detail: {
      code: "execution_unavailable", message: "No run was saved or started.",
    }}, {status: 503}));
    await expect(createModelClient("/api/v1", fetcher).startRun("m-1", {
      operation: "validate", model_version: "v1", dataset_id: "uci_credit",
    })).rejects.toThrow("No run was saved or started.");
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(JSON.parse(fetcher.mock.calls[0][1]?.body as string)).toEqual({
      operation: "validate", model_version: "v1", dataset_id: "uci_credit",
    });
  });
  it("rejects invalid and cross-model run responses", async () => {
    for (const invalid of [{...run, status: "passed"}, {...run, model_id: "another"}, {...run, created_at: "invalid"}]) {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json([invalid]));
      await expect(createModelClient("/api/v1", fetcher).listRuns("m-1")).rejects.toThrow();
    }
  });
  it("displays FastAPI validation details", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({detail: [
      {loc: ["query", "limit"], msg: "Input should be greater than or equal to 1"},
    ]}, {status: 422}));
    await expect(createModelClient("/api/v1", fetcher).getRun("run-1")).rejects.toThrow("query.limit");
  });
  it("encodes run ids and checks the returned identity", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({...run, id: "a/b"}));
    await createModelClient("/api/v1", fetcher).getRun("a/b");
    expect(fetcher.mock.calls[0][0]).toBe("/api/v1/runs/a%2Fb");
  });
});

describe("connection recovery", () => {
  it("recovers run history after a proxy failure when explicitly retried", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response("", { status: 500 }))
      .mockResolvedValueOnce(Response.json([]));
    const client = createModelClient("/api/v1", fetcher);
    await expect(client.listRuns("m-1")).rejects.toThrow("Check the backend server");
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(await client.listRuns("m-1")).toEqual([]);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
  it("recovers inventory after a connection failure on explicit retry", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockRejectedValueOnce(new TypeError("Network failure"))
      .mockResolvedValueOnce(Response.json([model]));
    const client = createModelClient("/api/v1", fetcher);
    await expect(client.list()).rejects.toThrow("Cannot reach");
    expect(await client.list()).toEqual([model]);
  });
});


describe("current Backend P0 contract reconciliation", () => {
  const sha = "a".repeat(64);
  const syntheticModel = {
    ...model,
    id: "credit-cox-python",
    name: "Synthetic Cox survival",
    dataset_id: null,
    inventory: {
      lifecycle_stage: "documented",
      synthetic_demo: true,
      manifest: { task: "survival_analysis", algorithm: "cox_ph", domain: "credit" },
      applicability: {
        document: "applicable",
        rebuild: "NOT_APPLICABLE",
        validate: "NOT_APPLICABLE",
        monitor: "NOT_APPLICABLE",
      },
    },
  };

  it("reads Backend-enriched Cox model records and does not misclassify null dataset as fraud", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json([syntheticModel]));
    const rows = await createModelClient("/api/v1", fetcher).list();
    expect(rows[0].dataset_id).toBeNull();
    expect(rows[0].inventory?.lifecycle_stage).toBe("documented");
    expect(rows[0].inventory?.synthetic_demo).toBe(true);
    expect(rows[0].inventory?.applicability?.validate).toBe("NOT_APPLICABLE");
  });

  it("accepts null dataset runs for synthetic documents while rejecting unknown dataset ids", async () => {
    const syntheticRun = { ...run, operation: "document", dataset_id: null };
    expect(readRun(syntheticRun)).toEqual(syntheticRun);
    expect(() => readRun({ ...run, dataset_id: "unexpected" })).toThrow("schema");
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ ...syntheticRun, model_id: "cox" }));
    await expect(createModelClient("/api/v1", fetcher).startRun("cox", {
      operation: "document", model_version: run.model_version, dataset_id: null,
    })).resolves.toMatchObject({ dataset_id: null });
  });

  const stats = {
    row_count: 10,
    positive_rate: 0.3,
    partition_counts: { train: 5, validation: 4, test: 1 },
    positive_rates: { train: 0.4, validation: 0, test: 1 },
    partition_sha256: { train: sha, validation: sha, test: sha },
  };
  const dataset = {
    id: "uci_credit",
    name: "UCI Credit",
    filename: "credit.xls",
    target_description: "default: 1",
    limitations: ["Historical population"],
    verified_upload: true,
    version: "2026-11",
    version_id: "uuid-001",
    sha256: sha,
    split_sha256: sha,
    statistics: stats,
    descriptor: { dataset_id: "uci_credit", source_url: "https://archive.ics.uci.edu", license: "CC BY" },
    columns: ["ID", "SEX", "default"],
    model_count: 2,
    created_at: "2026-11-07T10:00:00Z",
  };

  it("accepts persisted dataset versions, counts, hash evidence and model usage", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json([dataset]));
    const [row] = await createModelClient("/api/v1", fetcher).listDatasets();
    expect(row.verified_upload).toBe(true);
    expect(row.statistics?.partition_counts.train).toBe(5);
    expect(row.statistics?.positive_rate).toBe(0.3);
    expect(row.model_count).toBe(2);
  });

  it("rejects tampered dataset hashes, invalid verification flags and unreconciled partitions", async () => {
    const rows = [
      { ...dataset, sha256: "wrong" },
      { ...dataset, verified_upload: "yes" },
      { ...dataset, statistics: { ...stats, partition_counts: { ...stats.partition_counts, train: 6 } } },
      { ...dataset, model_count: -1 },
    ];
    for (const row of rows) {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json([row]));
      await expect(createModelClient("/api/v1", fetcher).listDatasets()).rejects.toThrow();
    }
  });

  it("accepts real report versions and lowercase NOT_APPLICABLE without fabricating approval", async () => {
    const report = {
      id: "report-01",
      model_id: "m-1",
      run_id: "run-01",
      kind: "validation",
      artifact_id: "artifact-01",
      metrics: [
        { name: "survival applicability", value: null, status: "not_applicable", reason: "Synthetic only", evidence_ids: [] },
      ],
      artifact_version: "3",
      artifact_sha256: sha,
      created_at: "2026-11-07T10:30:00Z",
      model_version: "v1",
      review_status: null,
      review_id: null,
      evidence: {},
      source_files: [],
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json([report]));
    const [loaded] = await createModelClient("/api/v1", fetcher).listReports("m-1");
    expect(loaded.artifact_version).toBe("3");
    expect(loaded.metrics[0].status).toBe("not_applicable");
    expect(loaded.review_status).toBeNull();
    const bad = vi.fn<typeof fetch>().mockResolvedValue(Response.json([{ ...report, artifact_sha256: "bad" }]));
    await expect(createModelClient("/api/v1", bad).listReports("m-1")).rejects.toThrow();
  });
});
