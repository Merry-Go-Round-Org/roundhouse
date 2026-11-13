"""The validator's metrics bundle, built from the platform's official validation report.

The metric code and the frozen policy are the legacy owner's (Wit's): `legacy_models/shared/
pipeline.py`, `validate_all_models.py` and `legacy_models/validation_policy.json`. The backend
already runs them in a trusted container (`app/services/validation_metrics.py`, called by
`live_validation.generate_uploaded_validation`), and the result is the validation report. This
module does not compute any metric. It copies the report's numbers into a `MetricsBundle`:

- aggregates only. Each value is one number, and each check has a fixed set of names, so no row,
  row ID, label or score from any partition can reach the validator. Free text from the report,
  such as check details and issue lists, is left out, because it can quote data.
- every number a reader needs, such as Gini as well as AUC and both ends of each interval, so the
  narrative check (`validation_report.narrative_problems`) can match every number the agent
  writes.

The final-test partition appears as `test`, as in the report. Its aggregates are allowed: the rule
is that the validator never sees final-test rows.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from harness.scoring import Check, CheckStatus, MetricsBundle
from harness.validation_report import METRICS_BUNDLE_FILE

PARTITIONS: tuple[str, ...] = ("validation", "test")

# Report check IDs and their template section (`templates/validation_narrative.md`).
_SECTIONS: dict[str, int] = {
    "artifact_integrity": 5,
    "schema_contract": 5,
    "fixture_scoring": 5,
    "reject_missing_row_id": 5,
    "reject_duplicated_row_id": 5,
    "reject_target_leakage": 3,
    "split_provenance": 3,
    "migration_parity": 5,
    "saved_metric_consistency": 6,
    "uploaded_data_evaluation": 6,
    "fresh_metric_consistency": 6,
    "performance": 6,
    "uncertainty_calibration": 6,
    "demographic_fairness": 7,
    "out_of_time_stability": 8,
    "score_stability_psi": 8,
    "feature_stability_psi": 8,
}
_METRIC_CHECKS = {"performance", "uncertainty_calibration", "demographic_fairness"}

# The scalar metrics `pipeline.metrics` returns, plus `ks` from `validation_metrics`.
_PERFORMANCE_FIELDS: tuple[str, ...] = (
    "rows",
    "positive_count",
    "prevalence",
    "roc_auc",
    "gini",
    "ks",
    "pr_auc_average_precision_sklearn",
    "log_loss",
    "brier",
    "mean_predicted_probability",
    "calibration_gap_observed_minus_expected",
    "threshold_selected_on_validation",
    "precision",
    "recall",
    "specificity",
    "f1",
    "recall_at_fpr_at_most_0_001",
)
_INTERVALS: tuple[str, ...] = ("roc_auc_ci95", "gini_ci95", "pr_auc_ci95", "ks_ci95")
_GROUP_FIELDS: tuple[str, ...] = (
    "n",
    "positives",
    "negatives",
    "selected",
    "selection_rate",
    "tpr",
    "fpr",
)
_MAX_BINS = 20
_ID_UNSAFE = re.compile(r"[^a-zA-Z0-9_.:-]+")


class BundleError(ValueError):
    """The report cannot be turned into a bundle, for example because it is not a validation
    report or it is bound to another artifact."""


def bundle_from_validation_report(
    report: dict[str, Any],
    *,
    data_sha256: dict[str, str] | None = None,
    expected_artifact_sha256: str | None = None,
    challenger: dict[str, Any] | None = None,
) -> MetricsBundle:
    """The metrics bundle for one validation report.

    `challenger` is the same trusted calculation run on the validator's challenger scores. Its
    performance and calibration checks are added with the prefix `challenger.`, with the same
    policy, so the narrative can compare the two.
    """
    if report.get("report_kind") != "legacy_model_validation":
        raise BundleError("not a legacy model validation report")
    artifact = report.get("artifact_sha256")
    if not isinstance(artifact, str) or not artifact:
        raise BundleError("the report has no artifact hash")
    if expected_artifact_sha256 is not None and artifact != expected_artifact_sha256:
        raise BundleError("the report is bound to another artifact")
    checks = _report_checks(report, prefix="")
    if challenger is not None:
        if challenger.get("report_kind") != "legacy_model_validation":
            raise BundleError("the challenger report is not a validation report")
        checks += [
            c for c in _report_checks(challenger, prefix="challenger.")
            if c.check_id.startswith(("challenger.performance.", "challenger.calibration."))
        ]  # fmt: skip
    ids = [c.check_id for c in checks]
    if len(set(ids)) != len(ids):
        raise BundleError("the report gives repeated check IDs")
    return MetricsBundle(
        policy_version=str(report.get("policy_version") or "unknown"),
        created_at=datetime.now(timezone.utc),
        artifact_sha256=artifact,
        data_sha256=dict(data_sha256 or {}),
        checks=checks,
    )


def _report_checks(report: dict[str, Any], *, prefix: str) -> list[Check]:
    checks: list[Check] = []
    evaluation = _fresh_evaluation(report)
    statuses: dict[str, CheckStatus] = {}
    for item in report.get("checks") or []:
        check_id = str(item.get("check_id", ""))
        status = item.get("status")
        if not check_id or status not in ("PASS", "FAIL", "REVIEW", "NOT_ASSESSABLE", "NOT_APPLICABLE"):
            continue
        statuses[check_id] = status
        if check_id in _METRIC_CHECKS:
            continue  # their numbers follow, one check per partition
        checks.append(
            Check(
                check_id=_safe_id(prefix + check_id),
                status=status,
                section=_SECTIONS.get(check_id),
                settings={"required": bool(item.get("required"))},
                # The measured meaning of the status, which the narrative quotes, not restates.
                note=str(item.get("summary") or "")[:1_000] or None,
            )
        )
    performance_status = statuses.get("performance", "REVIEW")
    calibration_status = statuses.get("uncertainty_calibration", "REVIEW")
    for partition in PARTITIONS:
        values = evaluation.get(partition)
        if not isinstance(values, dict):
            continue
        checks.append(
            Check(
                check_id=_safe_id(f"{prefix}performance.{partition}"),
                status=performance_status,
                section=6,
                partition=partition,
                values=_performance_values(values),
                settings={"threshold_selection": "maximum F1 on validation partition only"},
            )
        )
        bins = values.get("calibration_bins")
        if isinstance(bins, list) and bins:
            checks.append(
                Check(
                    check_id=_safe_id(f"{prefix}calibration.{partition}"),
                    status=calibration_status,
                    section=6,
                    partition=partition,
                    values=_calibration_values(bins),
                    settings={"bins": "validation-frozen risk quantiles"},
                )
            )
    if not prefix:
        checks += _fairness_checks(report, statuses.get("demographic_fairness"))
        checks += _stability_checks(report.get("stability"))
    return checks


def _fresh_evaluation(report: dict[str, Any]) -> dict[str, Any]:
    saved = report.get("saved_metrics")
    if isinstance(saved, dict) and isinstance(saved.get("fresh_evaluation"), dict):
        return saved["fresh_evaluation"]
    for item in report.get("checks") or []:
        if item.get("check_id") == "performance" and isinstance(item.get("details"), dict):
            return item["details"]
    return {}


def _performance_values(values: dict[str, Any]) -> dict[str, float | int | None]:
    out: dict[str, float | int | None] = {}
    for name in _PERFORMANCE_FIELDS:
        if name in values:
            out[name] = _number(values[name])
    matrix = values.get("confusion_matrix")
    if isinstance(matrix, dict):
        for cell in ("tn", "fp", "fn", "tp"):
            if cell in matrix:
                out[cell] = _number(matrix[cell])
    budget = values.get("alert_budget")
    if isinstance(budget, dict):
        for name in ("rate", "count", "precision_at_budget", "fraud_recall_at_budget"):
            if name in budget:
                out[f"alert_budget_{name}"] = _number(budget[name])
    uncertainty = values.get("uncertainty")
    if isinstance(uncertainty, dict):
        for name in _INTERVALS:
            interval = uncertainty.get(name)
            if isinstance(interval, (list, tuple)) and len(interval) == 2:
                out[f"{name}_low"] = _number(interval[0])
                out[f"{name}_high"] = _number(interval[1])
        if "n_resamples" in uncertainty:
            out["bootstrap_resamples"] = _number(uncertainty["n_resamples"])
    if out.get("gini") is None and isinstance(out.get("roc_auc"), float):
        out["gini"] = 2 * out["roc_auc"] - 1
    return out


def _calibration_values(bins: Iterable[Any]) -> dict[str, float | int | None]:
    out: dict[str, float | int | None] = {}
    for item in list(bins)[:_MAX_BINS]:
        if not isinstance(item, dict) or not isinstance(item.get("bin"), int):
            continue
        b = item["bin"]
        out[f"bin{b}_n"] = _number(item.get("n"))
        out[f"bin{b}_observed_rate"] = _number(item.get("observed_rate"))
        out[f"bin{b}_predicted_mean"] = _number(item.get("predicted_mean"))
        if len(out) >= 48:
            break
    return out


def _fairness_checks(report: dict[str, Any], status: CheckStatus | None) -> list[Check]:
    item = next(
        (c for c in report.get("checks") or [] if c.get("check_id") == "demographic_fairness"),
        None,
    )
    if item is None:
        return []
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    checks = [
        Check(
            check_id="fairness",
            status=status or item.get("status", "REVIEW"),
            section=7,
            partition="test",
            settings={"small_group_rule": "n < 100 or fewer than 30 positives or negatives"},
            note=None if details.get("groups") else "no protected attributes in this dataset",
        )
    ]
    gaps: dict[str, float | int | None] = {}
    for gap in details.get("gaps") or []:
        grouping = gap.get("grouping") or []
        metric = gap.get("metric")
        if isinstance(grouping, list) and isinstance(metric, str):
            gaps[_safe_id("_".join(map(str, grouping)) + f"_{metric}_gap")] = _number(
                gap.get("max_minus_min")
            )
    if gaps:
        checks.append(
            Check(
                check_id="fairness.gaps",
                status=status or "REVIEW",
                section=7,
                partition="test",
                values=dict(list(gaps.items())[:50]),
            )
        )
    for group in details.get("groups") or []:
        key = group.get("group")
        if not isinstance(key, dict) or not key:
            continue
        name = ".".join(f"{k}-{v}" for k, v in key.items())
        checks.append(
            Check(
                check_id=_safe_id(f"fairness.group.{name}"),
                status=status or "REVIEW",
                section=7,
                partition="test",
                values={f: _number(group.get(f)) for f in _GROUP_FIELDS if f in group},
                settings={"unstable": bool(group.get("unstable"))},
            )
        )
    return checks


def _stability_checks(stability: Any) -> list[Check]:
    """PSI from the stability engine, with the same bands as monitoring."""
    if not isinstance(stability, dict):
        return []
    checks: list[Check] = []
    score = stability.get("score")
    if isinstance(score, dict):
        checks.append(
            Check(
                check_id="stability.score_psi",
                status=_band_status(score),
                section=8,
                values={"psi": _number(score.get("psi"))},
                settings={"band": str(score.get("band"))},
            )
        )
    features = stability.get("features")
    if isinstance(features, dict) and features:
        values = {
            _safe_id(name): _number(r.get("psi"))
            for name, r in list(features.items())[:50]
            if isinstance(r, dict)
        }
        bands = [r.get("band") for r in features.values() if isinstance(r, dict)]
        worst = "red" if "red" in bands else "amber" if "amber" in bands else "green"
        checks.append(
            Check(
                check_id="stability.feature_psi",
                status="PASS" if worst == "green" else "REVIEW",
                section=8,
                values=values,
                settings={"worst_band": worst},
            )
        )
    return checks


def _band_status(result: dict[str, Any]) -> CheckStatus:
    if result.get("status") == "NOT_ASSESSABLE":
        return "NOT_ASSESSABLE"
    return "PASS" if result.get("band") == "green" else "REVIEW"


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def _safe_id(value: str) -> str:
    return _ID_UNSAFE.sub("_", value)[:100]


# ---------------------------------------------------------------- the mount


def bundle_mount_problems(files: dict[str, bytes]) -> list[str]:
    """Checks a metrics folder before the resolver mounts it for a validator: exactly one file,
    `metrics_bundle.json`, that parses as a `MetricsBundle`. The model has no field for rows."""
    names = sorted(files)
    if names != [METRICS_BUNDLE_FILE]:
        return [f"the metrics folder must hold only {METRICS_BUNDLE_FILE}, found: {', '.join(names) or 'nothing'}"]
    try:
        MetricsBundle.model_validate_json(files[METRICS_BUNDLE_FILE])
    except ValueError as exc:
        return [f"{METRICS_BUNDLE_FILE} is not a valid metrics bundle: {str(exc)[:300]}"]
    return []
