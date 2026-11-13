"""Trusted calculation entry point. Runs without uploaded code or model weights."""

import json
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import numpy as np
import pipeline as metrics
import standardize_exl_models as standardizer
import validate_all_models as validator


def calculate(root):
    manifest = json.loads((root / "manifest.json").read_text())
    context = json.loads((root / "context.json").read_text())
    domain = "uci" if manifest["dataset_id"] == "uci_credit" else "ulb"
    parts = {}
    for name in ("validation", "test"):
        data = pd.read_csv(root / f"{name}.csv")
        scores = pd.read_csv(root / f"{name}_scores.csv")
        if data.row_id.tolist() != scores.row_id.tolist():
            raise ValueError("Score/label row alignment mismatch")
        labels = (
            data[context["target_column"]].to_numpy() == context["positive_class"]
        ).astype(int)
        if len(np.unique(labels)) != 2:
            raise ValueError(
                "Evaluation needs both outcome classes; metrics are not assessable"
            )
        parts[name] = data, labels, scores.positive_probability.to_numpy()
    threshold = metrics.threshold_by_f1(parts["validation"][1], parts["validation"][2])
    evaluation, edges = {}, None
    for name, (_, labels, scores) in parts.items():
        values = metrics.metrics(labels, scores, threshold, domain)
        bins, edges = metrics.calibration_bins(labels, scores, edges)
        values["calibration_bins"] = bins
        values["uncertainty"] = metrics.bootstrap_intervals(
            labels, scores, seed=20261104, n_boot=100
        )
        from sklearn.metrics import roc_curve

        fpr, tpr, _ = roc_curve(labels, scores)
        values["ks"] = float(np.max(tpr - fpr))
        rng = np.random.default_rng(20261104)
        ks_samples = []
        for _ in range(100):
            selection = rng.integers(0, len(labels), len(labels))
            if len(np.unique(labels[selection])) < 2:
                continue
            fpr, tpr, _ = roc_curve(labels[selection], scores[selection])
            ks_samples.append(float(np.max(tpr - fpr)))
        values["uncertainty"]["ks_ci95"] = (
            np.quantile(ks_samples, [0.025, 0.975]).tolist()
            if len(ks_samples) > 30
            else None
        )
        auc_ci = values["uncertainty"].get("roc_auc_ci95")
        values["uncertainty"]["gini_ci95"] = (
            [2 * x - 1 for x in auc_ci] if auc_ci else None
        )
        evaluation[name] = values
    fair = metrics.fairness(*parts["test"], threshold) if domain == "uci" else []
    # Positive selection means flagged default risk, not loan approval.
    gaps = []
    for grouping in (["SEX"], ["age_band"], ["SEX", "age_band"]):
        groups = [g for g in fair if set(g["group"]) == set(grouping)]
        for metric in ("selection_rate", "tpr", "fpr"):
            values = [g[metric] for g in groups if g[metric] is not None]
            gaps.append(
                {
                    "grouping": grouping,
                    "metric": metric,
                    "max_minus_min": max(values) - min(values)
                    if len(values) > 1
                    else None,
                }
            )
    checks = context["checks"]
    c = validator.check
    saved = json.loads((root / "saved_evaluation.json").read_text())
    saved_issues = []
    for name in ("validation", "test"):
        saved_issues.extend(
            validator.metric_issues(
                standardizer.normal_metric(saved.get(name)), name, 1e-6
            )
        )
    checks.append(
        c(
            "saved_metric_consistency",
            "PASS" if not saved_issues else "FAIL" if saved else "NOT_ASSESSABLE",
            True,
            "Historical uploaded metrics checked separately from fresh calculations",
            "saved_evaluation_crosscheck",
            {"issues": saved_issues, "fresh_metrics_are_separate": True},
        )
    )
    checks.append(
        c(
            "uploaded_data_evaluation",
            "PASS",
            True,
            "Rescored immutable validation/test partitions; threshold selected on validation only",
            "fresh_backend_execution",
            context["lineage"],
        )
    )
    normalized = {k: standardizer.normal_metric(v) for k, v in evaluation.items()}
    issues = sum(
        (validator.metric_issues(v, k, 1e-6) for k, v in normalized.items()), []
    )
    checks.append(
        c(
            "fresh_metric_consistency",
            "FAIL" if issues else "PASS",
            True,
            "Fresh metric arithmetic checked",
            "fresh_backend_execution",
            {"issues": issues},
        )
    )
    checks.append(
        c(
            "performance",
            "REVIEW",
            False,
            "Fresh performance estimates; acceptance thresholds require owner approval",
            "fresh_backend_execution",
            evaluation,
        )
    )
    checks.append(
        c(
            "uncertainty_calibration",
            "REVIEW",
            False,
            "Validation-frozen calibration bins and 100-resample descriptive bootstrap",
            "fresh_backend_execution",
            {"validation": evaluation["validation"], "test": evaluation["test"]},
        )
    )
    checks.append(
        c(
            "demographic_fairness",
            "REVIEW" if domain == "uci" else "NOT_ASSESSABLE",
            False,
            "Descriptive UCI risk-flag rates; not lending approval or legal clearance"
            if domain == "uci"
            else "ULB has no demographic attributes",
            "fresh_backend_execution",
            {
                "groups": fair,
                "gaps": gaps,
                "small_group_rule": "n < 100 or fewer than 30 positives/negatives is unstable",
            },
        )
    )
    checks.append(
        c(
            "out_of_time_stability",
            "NOT_ASSESSABLE",
            False,
            "No independent post-deployment time cohort supplied",
            "applicability",
        )
    )
    failures = [x["check_id"] for x in checks if x["status"] == "FAIL"]
    limitations = [
        x["check_id"] for x in checks
        if x["required"] and x["status"] in ("REVIEW", "NOT_ASSESSABLE")
    ]
    blockers = [*failures, *limitations]
    return dict(
        schema_version="1.0.0",
        report_kind="legacy_model_validation",
        model_id=manifest["name"],
        domain=domain,
        task="binary_classification",
        model_kind="real_dataset",
        model_status=manifest.get("status", "uploaded"),
        artifact_sha256=manifest["artifact_sha256"],
        checks=checks,
        stability=context.get("stability"),
        summary=dict(
            overall_status="VALIDATION_FAILED"
            if failures
            else "VALIDATION_INCOMPLETE"
            if blockers
            else "REVIEW_REQUIRED",
            required_checks_passed=not blockers,
            blocking_check_ids=blockers,
            hard_blocking_check_ids=failures,
            limitation_check_ids=limitations,
            review_check_ids=[
                x["check_id"]
                for x in checks
                if x["status"] in ("REVIEW", "NOT_ASSESSABLE")
            ],
            human_approval_granted=False,
        ),
        saved_metrics={
            **normalized,
            "fresh_evaluation": evaluation,
            "source": "fresh_backend_execution",
            "threshold_selection": "maximum F1 on validation partition only",
        },
        limitations=[
            "Research prototype; no automatic model approval.",
            "The existing test partition is not a newly collected independent holdout.",
            "Historical training membership is not established by upload checks.",
            "Bootstrap and fairness estimates are descriptive; no regulatory thresholds claimed.",
            "PSI uses a separate validation cohort against train rows; it is not post-deployment drift evidence.",
        ],
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        policy_version="1.0.0",
        execution_requested=True,
    )


if __name__ == "__main__":
    print(json.dumps(calculate(Path("/inputs")), allow_nan=False))
