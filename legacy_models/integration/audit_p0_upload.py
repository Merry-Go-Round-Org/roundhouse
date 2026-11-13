#!/usr/bin/env python3
"""P0 read-only upload readiness audit. Never modifies trained model artifacts."""
import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGACY = ROOT / "legacy_models"
DATASETS = {
    "uci": ("uci_credit", "uci", "default of credit card clients.xls", "uci_split_ids.csv", 30000),
    "ulb": ("ulb_fraud", "ulb", "creditcard.csv", "ulb_split_ids.csv", 284807),
}
MODEL_IDS = (
    "credit_logistic_r", "fraud_logistic_r",
    "credit_logistic_python", "fraud_logistic_python",
    "credit_xgboost_python", "fraud_xgboost_python",
    "credit_lightgbm_python", "fraud_lightgbm_python",
    "credit_cox_python",
)


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check_split(name, path, expected_count):
    result = {"path": str(path), "exists": path.is_file(), "valid": False}
    if not path.is_file():
        result["reason"] = "Missing: restore ULB with legacy_models/shared/restore_ulb_split.py"
        return result
    seen = set()
    counts = Counter()
    errors = []
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["row_id", "split"]:
            return {**result, "reason": "Expected header row_id,split"}
        for row in reader:
            rid, part = row["row_id"], row["split"]
            if rid in seen:
                errors.append("Duplicate row ID: " + rid)
            seen.add(rid)
            if part not in {"train", "validation", "test"}:
                errors.append("Unknown partition: " + str(part))
            counts[part] += 1
    expected = {f"uci-{i:06d}" for i in range(1, expected_count + 1)} if name == "uci" else {f"ulb-{i:06d}" for i in range(expected_count)}
    if seen != expected:
        errors.append(f"ID membership mismatch: missing={len(expected - seen)} extra={len(seen - expected)}")
    result.update({"rows": sum(counts.values()), "counts": dict(counts),
                   "sha256": sha256(path), "valid": not errors and len(seen) == expected_count,
                   "errors": errors[:10]})
    return result


def check_source_membership(name, path, split_path, descriptor):
    if not path.is_file():
        return {"status": "NOT_ASSESSABLE", "reason": "Original source file unavailable"}
    if sha256(path) != descriptor["sha256"]:
        return {"status": "FAIL", "reason": "Source dataset SHA-256 mismatch"}
    if name == "uci":
        try:
            import pandas as pd
            df = pd.read_excel(path, sheet_name="Data", header=1)
        except (ImportError, ValueError, OSError) as error:
            return {"status": "NOT_ASSESSABLE", "reason": f"Install pandas and xlrd to read UCI XLS: {error}"}
        actual = [f"uci-{int(i):06d}" for i in df["ID"]]
    else:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            actual = [f"ulb-{i:06d}" for i, _ in enumerate(csv.DictReader(stream))]
    if len(actual) != descriptor["row_count"] or len(set(actual)) != len(actual):
        return {"status": "FAIL", "reason": "Original source has unexpected row IDs/count"}
    with split_path.open(encoding="utf-8", newline="") as stream:
        frozen = {row["row_id"] for row in csv.DictReader(stream)}
    return {"status": "PASS" if set(actual) == frozen else "FAIL",
            "source_sha256": descriptor["sha256"], "rows": len(actual),
            "reason": "Matched source-derived row ID set to frozen split"}


def audit_model(model_id):
    folder = LEGACY / model_id
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    missing = [field for field in ("name", "model_version", "language", "task", "purpose",
               "intended_use", "model_owner", "risk_tier", "tier_rationale")
               if not manifest.get(field)]
    artifacts = manifest.get("artifact_paths", [])
    artifacts = [artifacts] if isinstance(artifacts, str) else artifacts
    artifact_checks = {p: (folder / p).is_file() and
                       sha256(folder / p) == manifest.get("artifact_sha256")
                       for p in artifacts}
    python = manifest.get("language") == "Python"
    scorer = (folder / "score.py") if python else (folder / "score.R")
    text = scorer.read_text(encoding="utf-8") if scorer.is_file() else ""
    external_shared_import = python and ("parents[1]/'shared'" in text or 'parents[1]/"shared"' in text)
    declared_shared = manifest.get("shared_pipeline_sha256")
    shared_path = LEGACY / "shared" / "pipeline.py"
    shared_match = (sha256(shared_path) == declared_shared) if declared_shared and shared_path.is_file() else None
    historical_hash = manifest.get("historical_shared_pipeline_sha256_claimed")
    historical_matches_current = (
        historical_hash == sha256(shared_path)
        if historical_hash and shared_path.is_file() else None)
    return {"model": model_id, "metadata_complete": not missing, "missing_fields": missing,
            "artifact_hashes_valid": bool(artifacts) and all(artifact_checks.values()),
            "artifact_checks": artifact_checks, "scorer_present": scorer.is_file(),
            "uses_external_shared_directory": external_shared_import,
            "shared_pipeline_hash_matches_repo": shared_match,
            "historical_shared_pipeline_sha256_claimed": historical_hash,
            "historical_shared_pipeline_claim_matches_current": historical_matches_current,
            "historical_fit_time_attested": False if historical_hash else None,
            "source_folder_requires_bundle_relocation": external_shared_import,
            "standalone_folder_self_contained": not external_shared_import,
            "note": "The source Python folder intentionally references ../shared; its upload ZIP is self-contained. The historical pipeline claim remains unverified; verify the ZIP separately."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=ROOT / "datasets")
    parser.add_argument("--out", type=Path, help="Optional audit JSON destination")
    args = parser.parse_args()
    result = {"datasets": {}, "models": []}
    for key, (dataset_id, folder, filename, splitname, rows) in DATASETS.items():
        descriptor = json.loads((LEGACY / "dataset_descriptors" / key / "dataset.json").read_text())
        split_path = LEGACY / "evidence" / splitname
        split = check_split(key, split_path, rows)
        if split["valid"] and split["sha256"] != descriptor.get("split_sha256"):
            split["valid"] = False
            split["reason"] = "Frozen split SHA-256 does not match dataset descriptor"
        source = check_source_membership(key, args.data_root / folder / filename, split_path, descriptor) if split["valid"] else {"status": "NOT_ASSESSABLE", "reason": "Split missing or invalid"}
        result["datasets"][key] = {"id": dataset_id, "split": split, "source_membership": source}
    result["models"] = [audit_model(n) for n in MODEL_IDS]
    output = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if all(d["split"]["valid"] and d["source_membership"]["status"] == "PASS" for d in result["datasets"].values()) and all(m["metadata_complete"] and m["artifact_hashes_valid"] and m["scorer_present"] and m["shared_pipeline_hash_matches_repo"] is not False for m in result["models"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
