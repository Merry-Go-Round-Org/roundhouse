"""The metrics bundle copies the official validation report's aggregates and nothing else."""
from __future__ import annotations

import json

import pytest

from harness.metrics_bundle import (
    BundleError,
    bundle_from_validation_report,
    bundle_mount_problems,
)
from harness.validation_report import unsupported_numbers


def _partition(auc: float, rows: int) -> dict:
    return {
        "rows": rows,
        "positive_count": rows // 5,
        "prevalence": 0.2,
        "pr_auc_average_precision_sklearn": 0.51,
        "roc_auc": auc,
        "log_loss": 0.43,
        "brier": 0.137,
        "mean_predicted_probability": 0.21,
        "calibration_gap_observed_minus_expected": -0.01,
        "threshold_selected_on_validation": 0.27,
        "confusion_matrix": {"tn": 3900, "fp": 800, "fn": 500, "tp": 800},
        "precision": 0.5,
        "recall": 0.6153,
        "specificity": 0.83,
        "f1": 0.5517,
        "gini": 2 * auc - 1,
        "ks": 0.38,
        "calibration_bins": [
            {"bin": 0, "n": 600, "positives": 30, "observed_rate": 0.05, "predicted_mean": 0.06},
            {"bin": 1, "n": 600, "positives": 60, "observed_rate": 0.1, "predicted_mean": 0.11},
        ],
        "uncertainty": {
            "method": "iid_percentile_bootstrap_95",
            "n_resamples": 100,
            "note": "Descriptive only",
            "pr_auc_ci95": [0.49, 0.53],
            "roc_auc_ci95": [auc - 0.01, auc + 0.01],
            "ks_ci95": [0.36, 0.4],
            "gini_ci95": [2 * auc - 1.02, 2 * auc - 0.98],
        },
    }


def report(**overrides) -> dict:
    evaluation = {"validation": _partition(0.7213, 6000), "test": _partition(0.7158, 6000)}
    base = {
        "schema_version": "1.0.0",
        "report_kind": "legacy_model_validation",
        "model_id": "credit_logistic_r",
        "artifact_sha256": "f" * 64,
        "policy_version": "1.0.0",
        "checks": [
            {"check_id": "artifact_integrity", "status": "PASS", "required": True, "summary": "ok",
             "evidence_kind": "x", "details": {"files": ["model.rds"]}},
            {"check_id": "reject_target_leakage", "status": "PASS", "required": True, "summary": "ok",
             "evidence_kind": "x", "details": {}},
            {"check_id": "fresh_metric_consistency", "status": "FAIL", "required": True,
             "summary": "issues", "evidence_kind": "x",
             "details": {"issues": ["row 17 has label 1 and score 0.93"]}},
            {"check_id": "performance", "status": "REVIEW", "required": False, "summary": "s",
             "evidence_kind": "x", "details": evaluation},
            {"check_id": "uncertainty_calibration", "status": "REVIEW", "required": False,
             "summary": "s", "evidence_kind": "x", "details": {}},
            {"check_id": "demographic_fairness", "status": "REVIEW", "required": False,
             "summary": "s", "evidence_kind": "x",
             "details": {
                 "groups": [
                     {"group": {"SEX": "1"}, "n": 2400, "positives": 500, "negatives": 1900,
                      "selected": 700, "selection_rate": 0.2917, "tpr": 0.6, "fpr": 0.21,
                      "selection_rate_ci95_wilson": [0.27, 0.31], "unstable": False},
                     {"group": {"SEX": "2", "age_band": "60_plus"}, "n": 40, "positives": 9,
                      "negatives": 31, "selected": 12, "selection_rate": 0.3, "tpr": 0.55,
                      "fpr": 0.2, "unstable": True},
                 ],
                 "gaps": [{"grouping": ["SEX"], "metric": "selection_rate", "max_minus_min": 0.04}],
             }},
        ],
        "stability": {
            "score": {"psi": 0.012, "band": "green", "status": "PASS"},
            "features": {"PAY_0": {"psi": 0.13, "band": "amber"}, "LIMIT_BAL": {"psi": 0.02, "band": "green"}},
        },
        "saved_metrics": {"fresh_evaluation": evaluation},
    }
    base.update(overrides)
    return base


def by_id(bundle):
    return {c.check_id: c for c in bundle.checks}


def test_bundle_holds_every_partition_metric_with_gini_and_intervals():
    bundle = bundle_from_validation_report(report(), data_sha256={"validation": "1" * 64})
    checks = by_id(bundle)
    test = checks["performance.test"]
    assert test.partition == "test" and test.section == 6 and test.status == "REVIEW"
    assert test.values["roc_auc"] == 0.7158
    assert test.values["gini"] == pytest.approx(0.4316)
    assert test.values["roc_auc_ci95_low"] == pytest.approx(0.7058)
    assert test.values["tp"] == 800 and test.values["bootstrap_resamples"] == 100
    assert checks["calibration.validation"].values["bin1_observed_rate"] == 0.1
    assert checks["artifact_integrity"].status == "PASS"
    assert checks["artifact_integrity"].settings == {"required": True}
    assert checks["fresh_metric_consistency"].status == "FAIL"
    assert checks["stability.feature_psi"].values == {"PAY_0": 0.13, "LIMIT_BAL": 0.02}
    assert checks["stability.feature_psi"].status == "REVIEW"
    assert checks["fairness.group.SEX-2.age_band-60_plus"].settings == {"unstable": True}
    assert checks["fairness.gaps"].values == {"SEX_selection_rate_gap": 0.04}
    assert bundle.policy_version == "1.0.0" and bundle.artifact_sha256 == "f" * 64


def test_no_free_text_or_row_detail_reaches_the_bundle():
    text = bundle_from_validation_report(report()).model_dump_json()
    assert "row 17" not in text and "model.rds" not in text
    assert "Descriptive only" not in text and "summary" not in text


def test_a_narrative_quoting_the_report_passes_and_an_invented_number_fails():
    bundle = bundle_from_validation_report(report()).model_dump(mode="json")
    good = "AUC 0.72 on test, Gini 0.43, KS 0.38, 61.5% recall, PSI 0.13 on PAY_0."
    assert unsupported_numbers(good, bundle) == []
    assert unsupported_numbers("AUC 0.77 on test.", bundle) == ["0.77"]


def test_a_challenger_report_adds_only_its_metric_checks():
    challenger = report(artifact_sha256="c" * 64)
    challenger["saved_metrics"]["fresh_evaluation"]["test"]["roc_auc"] = 0.75
    checks = by_id(bundle_from_validation_report(report(), challenger=challenger))
    assert checks["challenger.performance.test"].values["roc_auc"] == 0.75
    assert "challenger.artifact_integrity" not in checks
    assert "challenger.fairness" not in checks


def test_wrong_reports_are_refused():
    with pytest.raises(BundleError):
        bundle_from_validation_report({"report_kind": "documentation"})
    with pytest.raises(BundleError):
        bundle_from_validation_report(report(), expected_artifact_sha256="0" * 64)
    with pytest.raises(BundleError):
        bundle_from_validation_report(report(artifact_sha256=""))


def test_non_finite_values_become_none():
    r = report()
    r["saved_metrics"]["fresh_evaluation"]["test"]["specificity"] = float("nan")
    assert by_id(bundle_from_validation_report(r))["performance.test"].values["specificity"] is None


def test_the_mount_holds_only_a_valid_bundle():
    raw = bundle_from_validation_report(report()).model_dump_json().encode()
    assert bundle_mount_problems({"metrics_bundle.json": raw}) == []
    assert bundle_mount_problems({"metrics_bundle.json": raw, "test.csv": b"row_id\n1"})
    assert bundle_mount_problems({"metrics_bundle.json": b"{}"})
    leaked = json.loads(raw)
    leaked["checks"][0]["rows"] = [{"row_id": "1"}]
    assert bundle_mount_problems({"metrics_bundle.json": json.dumps(leaked).encode()})
