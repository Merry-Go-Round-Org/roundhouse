import { describe, expect, it } from "vitest";
import { demoModels } from "./demo";
import type { ModelRecord } from "./api";
import { demoModelFacts, liveModelFacts } from "./modelDetailAdapters";

const backend: ModelRecord = {
  id: "backend-model",
  name: "Actual uploaded GLM",
  version: "1.0.0",
  language: "R",
  dataset_id: "uci_credit",
  owner: null,
  purpose: "Predict next-month default",
  lifecycle_status: "development",
  created_at: "2026-11-07T01:00:00Z",
};

describe("model detail metadata adapters", () => {
  it("labels demo risk and outcomes as illustrative", () => {
    const facts = demoModelFacts(demoModels[0]);
    expect(facts.find((x) => x.label === "Risk tier")?.value).toContain("illustrative");
    expect(facts.find((x) => x.label === "Positive outcome")?.value).toContain("(demo)");
    expect(facts.find((x) => x.label === "Dataset version")?.value).toContain("awaiting evidence");
  });

  it("exposes only supplied Backend identity and lifecycle metadata", () => {
    const facts = liveModelFacts(backend);
    expect(facts.find((x) => x.label === "Lifecycle (not approval)")?.value).toBe("development");
    expect(facts.find((x) => x.label === "Created")?.value).toBe(backend.created_at);
    expect(facts.find((x) => x.label === "Domain / task / algorithm")?.value).toBeUndefined();
    expect(facts.some((x) => /risk tier|approval|parity|validation/i.test(x.label) && x.label !== "Lifecycle (not approval)")).toBe(false);
    expect(facts.find((x) => x.label === "Name")?.value).toBe(backend.name);
  });
});
