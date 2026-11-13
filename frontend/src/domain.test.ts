import { describe, it, expect } from "vitest";
import { demoModels } from "./demo";
import { canAccept, reviewModel, draftDocument } from "./domain";

describe("review safeguards", () => {
  it("does not let parity override missing or adverse validation evidence", () => {
    expect(demoModels[0].parity.status).toBe("green");
    expect(canAccept(demoModels[0])).toBe(false);
    expect(() =>
      reviewModel(demoModels[0], "Reviewer", "Accepted", "Looks good"),
    ).toThrow("Resolve required checks");
  });
  it("requires an independent name and a written rationale", () => {
    expect(() =>
      reviewModel(demoModels[0], " demo owner a ", "Changes requested", "Fix"),
    ).toThrow("different");
    expect(() =>
      reviewModel(demoModels[0], "Reviewer", "Changes requested", "  "),
    ).toThrow("reason");
  });
  it("records requested changes without modifying original evidence", () => {
    const result = reviewModel(
      demoModels[0],
      "Reviewer",
      "Changes requested",
      "Investigate calibration",
    );
    expect(result.review?.note).toBe("Investigate calibration");
    expect(demoModels[0].review).toBeNull();
    expect(result.metrics).toEqual(demoModels[0].metrics);
  });
  it("requires validation-stage evidence and passing parity even when metrics pass", () => {
    const passing = {
      ...demoModels[0],
      metrics: demoModels[0].metrics.map((m) => ({
        ...m,
        status: "green" as const,
      })),
    };
    expect(canAccept(passing)).toBe(true);
    expect(canAccept({ ...passing, metrics: [] })).toBe(false);
    expect(canAccept({ ...passing, stage: "Inventory" })).toBe(false);
    expect(
      canAccept({ ...passing, parity: { ...passing.parity, status: "red" } }),
    ).toBe(false);
  });
});
it("keeps fraud labels and missing evidence explicit in draft documents", () => {
  const draft = draftDocument(demoModels[1]);
  expect(draft).toContain("Class=1 (fraud)");
  expect(draft).toContain("No executed evidence");
  expect(draft).toContain("DEMO TEMPLATE");
});
