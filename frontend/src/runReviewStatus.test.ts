import { describe, it, expect } from "vitest";
import { isRevisionRequested } from "./runReviewStatus";
describe("review revision state", () => {
  it("recognizes the review service's closed revision checkpoint", () => {
    expect(isRevisionRequested({ status: "failed", stage: "changes_requested" })).toBe(true);
  });
  it("does not disguise execution failures or active runs", () => {
    expect(isRevisionRequested({ status: "failed", stage: "validation" })).toBe(false);
    expect(isRevisionRequested({ status: "failed", stage: null })).toBe(false);
    expect(isRevisionRequested({ status: "running", stage: "changes_requested" })).toBe(false);
    expect(isRevisionRequested(null)).toBe(false);
  });
});
