#!/usr/bin/env python3
"""Turn the EXL validation suite's local JSON outputs into a backend-consumable handoff.

Pure data transformation; does not score/load models, approve models, or touch the
existing backend/frontend implementations. Includes no raw source data or predictions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

CONTRACT_VERSION = "1.0.0"
CHECK_STATES = {"PASS", "FAIL", "REVIEW", "NOT_ASSESSABLE", "NOT_APPLICABLE"}
OVERALL_STATES = {"VALIDATION_FAILED", "VALIDATION_INCOMPLETE", "REVIEW_REQUIRED"}


class ContractError(ValueError):
    """Input has inconsistent or unsafe validation report references."""


def read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ContractError(f"Expected JSON object at {path.name}")
    return data


def summary_report(report: dict) -> dict:
    """Frontend-ready data, but still subject to backend authorization."""
    model_id = report["model_id"]
    summary = report["summary"]
    checks = report["checks"]
    if not isinstance(checks, list) or not checks:
        raise ContractError(f"{model_id}: missing check records")
    check_ids: set[str] = set()
    for record in checks:
        cid = record["check_id"]
        if not isinstance(cid, str) or not cid or cid in check_ids:
            raise ContractError(f"{model_id}: missing or duplicate check_id")
        check_ids.add(cid)
        if record["status"] not in CHECK_STATES or not isinstance(record["required"], bool):
            raise ContractError(f"{model_id}: invalid check status/required flag")
    blockers = sorted(x["check_id"] for x in checks if
                      x["status"] == "FAIL" or (x["required"] and x["status"] != "PASS"))
    if sorted(summary["blocking_check_ids"]) != blockers:
        raise ContractError(f"{model_id}: blocking checks don't match reported check states")
    if summary["required_checks_passed"] != (not blockers):
        raise ContractError(f"{model_id}: required_checks_passed inconsistent")
    if summary["human_approval_granted"] is not False:
        raise ContractError(f"{model_id}: auto-approval is forbidden")
    expected_overall = ("VALIDATION_FAILED" if any(x["status"] == "FAIL" for x in checks)
                        else "VALIDATION_INCOMPLETE" if blockers else "REVIEW_REQUIRED")
    if summary["overall_status"] not in OVERALL_STATES or summary["overall_status"] != expected_overall:
        raise ContractError(f"{model_id}: overall status is inconsistent")
    metric = report.get("saved_metrics", {}).get("test", {})
    if report["task"] == "binary_classification" and not isinstance(metric, dict):
        raise ContractError(f"{model_id}: binary test metrics must be an object")
    relevant = ["rows", "positives", "roc_auc", "pr_auc", "f1", "recall", "brier",
                "precision", "threshold", "confusion_matrix", "bootstrap", "alert_budget"]
    test = {key: metric.get(key) for key in relevant} if report["task"] == "binary_classification" else None
    return {
        "model_id": model_id,
        "domain": report["domain"],
        "task": report["task"],
        "model_kind": report["model_kind"],
        "overall_status": expected_overall,
        "required_checks_passed": not blockers,
        "blocking_check_ids": blockers,
        "review_check_ids": summary["review_check_ids"],
        "human_approval_granted": False,
        "artifact_sha256": report["artifact_sha256"],
        "performance": {
            "source_kind": "saved_evaluation_crosscheck",
            "new_holdout_evaluation": False,
            "test": test,
            "synthetic_c_index": (report.get("saved_metrics", {}).get("c_index")
                                  if report["task"] == "survival_analysis" else None),
        },
        "checks": [{"check_id": c["check_id"], "status": c["status"],
                    "required": c["required"], "summary": c["summary"],
                    "evidence_kind": c["evidence_kind"], "details": c["details"]}
                   for c in checks],
        "limitations": report["limitations"],
        "execution_requested": report["execution_requested"],
    }


def build(reports_dir: Path) -> dict:
    reports_dir = reports_dir.resolve(strict=True)
    index = read_json(reports_dir / "validation_index.json")
    if (index.get("schema_version") != CONTRACT_VERSION or
        index.get("report_kind") != "legacy_model_validation_index"):
        raise ContractError("Unexpected validation index version or type")
    if index.get("no_human_approvals_issued") is not True:
        raise ContractError("Index must not assert a human approval")
    models = index.get("models")
    if not isinstance(models, list) or not models:
        raise ContractError("Validation index has no model records")
    seen: set[str] = set()
    compact = []
    for entry in models:
        model_id = entry["model_id"]
        if not isinstance(model_id, str) or not model_id or not all(
            c.isascii() and (c.isalnum() or c in "-_") for c in model_id
        ) or model_id in seen:
            raise ContractError("Unsafe, empty or duplicate model ID")
        seen.add(model_id)
        # Do not trust report_path from another process: enforce the exact shape.
        if entry.get("report_path") != f"{model_id}/validation_report.json":
            raise ContractError(f"{model_id}: unsafe or unexpected report path")
        path = (reports_dir / entry["report_path"]).resolve(strict=True)
        if not path.is_relative_to(reports_dir):
            raise ContractError(f"{model_id}: report path escapes validation root")
        report = read_json(path)
        if (report.get("report_kind") != "legacy_model_validation" or
            report.get("schema_version") != CONTRACT_VERSION or
            report.get("model_id") != model_id or
            report.get("generated_at_utc") != index["generated_at_utc"] or
            report.get("policy_version") != index["policy_version"] or
            report.get("execution_requested") != index["execution_requested"]):
            raise ContractError(f"{model_id}: model report/index provenance mismatch")
        data = summary_report(report)
        if data["overall_status"] != entry["overall_status"] or data["required_checks_passed"] != entry["required_checks_passed"]:
            raise ContractError(f"{model_id}: model report/index status mismatch")
        data["report_path"] = entry["report_path"]
        compact.append(data)
    return {"contract_version": CONTRACT_VERSION, "kind": "model_validation_handoff",
            "source_report_kind": index["report_kind"],
            "generated_at_utc": index["generated_at_utc"],
            "policy_version": index["policy_version"],
            "execution_requested": index["execution_requested"],
            "governance_approval": "not_granted",
            "models": compact,
            "comparability": index["comparability"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", default="datasets/reports/validation")
    parser.add_argument("--output", default="datasets/reports/validation/validation_handoff.json")
    args = parser.parse_args(argv)
    payload = build(Path(args.reports_dir))
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Validated {len(payload['models'])} reports -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
