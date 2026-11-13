import { describe, expect, it } from "vitest";
import { createModelClient } from "./api";
import { derivedFrom, latestCandidate, parityOutcome, readParityRun, readParityRuns } from "./parity";

const run = {
  id: "parity-1",
  candidate_model_id: "candidate-1",
  candidate_version: "1.0.0-r1",
  candidate_bundle_sha256: "a".repeat(64),
  parent_model_id: "parent-1",
  parent_version: "1.0.0",
  rebuild_run_id: "run-1",
  initiator: "agent",
  actor_id: "developer-1",
  status: "completed",
  passed: false,
  error: null,
  created_at: "2026-11-07T10:00:00Z",
  result: {
    method: "parity-v1",
    n_rows: 100,
    n_above_tolerance: 2,
    max_absolute_difference: 0.002,
    worst_rows: [{ row_id: "r-1", absolute_difference: 0.002 }],
  },
};

describe("official parity runs", () => {
  it("reads the backend contract and rejects contradictory or foreign runs", () => {
    expect(readParityRun(run).initiator).toBe("agent");
    expect(parityOutcome(readParityRun(run))).toBe("FAIL");
    expect(parityOutcome(readParityRun({ ...run, passed: true }))).toBe("PASS");
    expect(parityOutcome(readParityRun({ ...run, status: "failed", error: "crash" }))).toBe("ERROR");
    expect(() => readParityRun({ ...run, status: "failed", passed: true })).toThrow();
    expect(() => readParityRun({ ...run, initiator: "system" })).toThrow();
    expect(() => readParityRun({ ...run, result: { worst_rows: [{ row_id: "r" }] } })).toThrow();
    expect(readParityRuns([run], "parent-1")).toHaveLength(1);
    expect(readParityRuns([run], "candidate-1")).toHaveLength(1);
    expect(() => readParityRuns([run], "other")).toThrow("another model");
  });

  it("finds the candidate to rerun and recognises rebuild candidates", () => {
    expect(latestCandidate([readParityRun(run)])).toEqual({ id: "candidate-1", version: "1.0.0-r1" });
    expect(latestCandidate([])).toBeNull();
    expect(derivedFrom({ derived_from: { model_id: "parent-1" } })).toBe("parent-1");
    expect(derivedFrom({})).toBeNull();
    expect(derivedFrom(undefined)).toBeNull();
  });

  it("posts one parity run for the exact candidate version", async () => {
    const calls: { url: string; method?: string }[] = [];
    const client = createModelClient("/api/v1", (async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method });
      return new Response(JSON.stringify({ ...run, initiator: "human", passed: true }), { status: 201 });
    }) as typeof fetch);
    const result = await client.runParity("candidate-1", "1.0.0-r1");
    expect(result.initiator).toBe("human");
    expect(calls).toEqual([{ url: "/api/v1/models/candidate-1/versions/1.0.0-r1/parity", method: "POST" }]);
    await expect(client.runParity("candidate-1", "2.0.0")).rejects.toThrow("another candidate");
  });
});
