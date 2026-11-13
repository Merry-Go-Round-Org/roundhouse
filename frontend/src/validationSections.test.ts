import { it, expect } from "vitest";
import {
  groupChecks,
  numericEvidence,
  intervalEvidence,
} from "./validationSections";
import type { ValidationCheck } from "./validation";
function check(id: string, kind = "saved_evaluation"): ValidationCheck {
  return {
    check_id: id,
    status: "NOT_ASSESSABLE",
    required: false,
    summary: "Missing",
    evidence_kind: kind,
    details: {},
  };
}
it("groups exact producer IDs and preserves every unfamiliar check without guessed classification", () => {
  const checks = [
    check("saved_metric_consistency"),
    check("migration_parity"),
    check("fixture_scoring", "fresh_execution"),
    check("future_psi_check"),
    check("demographic_fairness"),
  ];
  const groups = groupChecks(checks);
  expect(groups.find((g) => g.id === "performance")?.checks).toEqual([
    checks[0],
  ]);
  expect(groups.find((g) => g.id === "migration")?.checks[0].status).toBe(
    "NOT_ASSESSABLE",
  );
  expect(groups.find((g) => g.id === "other")?.checks).toEqual([checks[3]]);
  expect(groups.flatMap((g) => g.checks)).toHaveLength(checks.length);
});
it("preserves zero metrics and intervals without coercion, calculation or hidden rounding", () => {
  expect(numericEvidence(0)).toBe("0");
  expect(numericEvidence(null)).toBe("Not supplied");
  expect(numericEvidence("0.7")).toBe("Invalid supplied value");
  expect(numericEvidence(Infinity)).toBe("Invalid supplied value");
  expect(intervalEvidence([0, 0.8])).toBe("[0, 0.8]");
  expect(intervalEvidence([0.8, 0.7])).toBe("Invalid supplied interval");
  expect(intervalEvidence(["0.1", 0.2])).toBe("Invalid supplied interval");
  expect(intervalEvidence(undefined)).toBe("Not supplied");
});
