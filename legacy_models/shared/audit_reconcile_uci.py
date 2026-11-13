#!/usr/bin/env python3
"""Audit and explicitly reconcile UCI model split partitions.

Never fabricate a 5,948-row membership list from aggregate counts. The
existing local frozen uci_split_ids.csv is authoritative for a new, clearly
versioned reproduction. --apply backs up and retrains THREE Python models on
that split, preserving the already-fit R GLM only if its training partition
counts match. It does not touch ULB, Cox, or the private data on Git.
"""
import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve()
ROOT = HERE.parents[2]
SHARED = HERE.parent
sys.path.insert(0, str(SHARED))
from engine import dataset, PATH, TARGET, FEATURES, checksum, train, score  # noqa: E402

PYTHON_NAMES = ("credit_logistic_python", "credit_xgboost_python", "credit_lightgbm_python")
R_NAME = "credit_logistic_r"
EXPECTED_HISTORICAL = {"validation": (5996, 1313), "test": (5948, 1310)}

def stats(data, partition):
    target = TARGET["uci"]
    return {
        name: {"rows": int((partition == name).sum()),
               "positives": int(data.loc[partition == name, target].sum())}
        for name in ("train", "validation", "test")
    }

def stored_counts(package):
    path = package / "evaluation.json"
    if not path.is_file():
        raise FileNotFoundError(f"Missing evaluation: {path}")
    eval_obj = json.loads(path.read_text())
    out = {}
    for name in ("validation", "test"):
        row = eval_obj[name]
        out[name] = {
            "rows": int(row.get("n", row.get("rows", -1))),
            "positives": int(row.get("positives", row.get("positive_count", -1))),
        }
    return out

def needs_change(observed, expected):
    return any(observed[name] != expected[name] for name in ("validation", "test"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Back up and retrain three Python credit models")
    a = ap.parse_args()
    split_path = ROOT / "legacy_models/evidence/uci_split_ids.csv"
    dataset_path = ROOT / PATH["uci"]
    models = ROOT / "legacy_models"
    if not split_path.is_file():
        raise FileNotFoundError(f"Missing frozen split {split_path}. Do not recreate it without auditing.")
    data, data_sha = dataset("uci", dataset_path)
    split_sha = checksum(split_path)
    ids = pd.read_csv(split_path, dtype="string")
    if list(ids.columns) != ["row_id", "split"]:
        raise ValueError("Expected exactly row_id,split columns")
    if len(ids) != len(data) or not ids.row_id.reset_index(drop=True).equals(data.row_id.astype("string").reset_index(drop=True)):
        raise ValueError("Frozen CSV row IDs/order do not exactly match original UCI dataset")
    if set(ids.split.dropna().unique()) != {"train", "validation", "test"} or ids.isna().any().any():
        raise ValueError("Invalid split assignments")
    partition = ids.split.astype(str)
    # Enforce no duplicate predictor profile assigned to multiple partitions.
    hashes = pd.util.hash_pandas_object(data[FEATURES["uci"]], index=False)
    if pd.DataFrame({"group": hashes, "split": partition}).groupby("group").split.nunique().gt(1).any():
        raise ValueError("Grouped duplicate predictor-profile leakage detected")
    expected = stats(data, partition)
    r_package = models / R_NAME
    r_eval = stored_counts(r_package)
    print(f"Dataset SHA-256: {data_sha}")
    print(f"Frozen split SHA-256: {split_sha}")
    print("Frozen split:", json.dumps(expected))
    print("R GLM evaluation:", json.dumps(r_eval))
    if needs_change(r_eval, expected):
        raise RuntimeError("R GLM is NOT aligned to frozen CSV. Stop: retrain and re-evaluate R on this split before any Python rebuild.")
    incorrect = []
    for name in PYTHON_NAMES:
        observed = stored_counts(models / name)
        is_bad = needs_change(observed, expected)
        print(f"{name}: evaluation={observed}; aligned={not is_bad}")
        if is_bad:
            incorrect.append(name)
    if not incorrect:
        print("PASS: all four UCI credit models report aligned partition sizes and positive counts.")
        print("NOTE: counts alone are not proof of exact ID identity; require saved per-model split fingerprints for future releases.")
        return
    print("MISMATCH: Python models use a different historical partition.")
    print("Historical Python test: 5948 rows / 1310 defaults; current R-frozen test:",
          expected["test"]["rows"], "rows /", expected["test"]["positives"], "defaults")
    if not a.apply:
        print("DRY RUN ONLY: run this script again with --apply to back up and refit ALL THREE credit Python models on the existing R-frozen split.")
        print("Existing historical Python evaluations remain valid for their ORIGINAL partition, not comparable directly with this R GLM.")
        return
    # Require all three original Python packages to be consistently rebuilt to
    # avoid a mixed-vintage leaderboard.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = models / "evidence/reconciliation_backups" / timestamp
    if backup.exists():
        raise FileExistsError(backup)
    backup.mkdir(parents=True)
    for name in PYTHON_NAMES:
        source = models / name
        target = backup / name
        target.mkdir()
        for filename in ("model.joblib", "manifest.json", "evaluation.json", "fairness.json"):
            f = source / filename
            if f.is_file():
                shutil.copy2(f, target / filename)
        fixture = source / "fixtures"
        if fixture.exists():
            shutil.copytree(fixture, target / "fixtures")
        private = models / "evidence/private_test" / (name + "_test_predictions.csv")
        if private.is_file():
            shutil.copy2(private, target / private.name)
    (backup / "README.txt").write_text(
        "Historical outputs before UCI partition reconciliation. PRIVATE local backup; do not commit.\n"
        f"New frozen split SHA-256: {split_sha}\n"
        "Original Python test: 5948 rows, 1310 positives; R-frozen test partition differs.\n")
    print("PRIVATE original artifacts backed up at:", backup)
    for name, algo in (
        ("credit_logistic_python", "logistic"),
        ("credit_xgboost_python", "xgboost"),
        ("credit_lightgbm_python", "lightgbm"),
    ):
        print(f"Retraining {name} on frozen split...")
        train("uci", algo, dataset_path, models / name)
        current = stored_counts(models / name)
        if needs_change(current, expected):
            raise RuntimeError(f"{name}: unexpected partition metrics after training; outputs backed up at {backup}")
        print(f"{name}: split alignment PASS")
    report = {
        "status": "reconciled_retrained_python_on_r_frozen_split",
        "data_sha256": data_sha,
        "frozen_split_sha256": split_sha,
        "split_counts": expected,
        "baseline_r_glm_artifact_sha256": json.loads((r_package / "manifest.json").read_text())["artifact_sha256"],
        "retrained_python_artifacts": {
            name: json.loads((models / name / "manifest.json").read_text())["artifact_sha256"]
            for name in PYTHON_NAMES
        },
        "original_python_test_counts": EXPECTED_HISTORICAL["test"],
        "limitations": [
            "Historical Python results were on a DIFFERENT partition; do not mix old and new metrics",
            "R GLM was trained on the existing local frozen split, and its metric counts agree",
            "Identical counts do not cryptographically prove R used identical IDs: R training execution did not capture split SHA",
            "Final test metrics were seen before reconciliation; this is not a new untouched holdout",
            "Repeat uncertainty, fairness and verification diagnostics after retraining before sign-off"
        ],
    }
    dest = models / "UCI_SPLIT_RECONCILIATION.json"
    dest.write_text(json.dumps(report, indent=2) + "\n")
    print("Reconciled new Python artifacts; created", dest)
    print("NEXT: rerun evaluation_diagnostics.py, uci_fairness.py, verify_package.py for each of the three Python models.")
    print("IMPORTANT: keep historical backups local, verify LFS, update docs, then commit new artifacts and evidence.")

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"UCI SPLIT RECONCILIATION FAILED: {error}", file=sys.stderr)
        sys.exit(1)
