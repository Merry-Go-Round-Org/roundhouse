#!/usr/bin/env python3
"""Stage P0 dataset-upload files for UCI and ULB, verifying immutable hashes.

Issue #10 upload unit is exactly three files per dataset: original public source,
dataset.json descriptor, and frozen row_id/split CSV. This tool does not
repartition datasets, rewrite model artifacts, access final-test labels, or
publish raw data to Git. ULB's canonical split is regenerated with its
existing verified deterministic restore_ulb_split.py function.

By default only the two small metadata/split files are staged, deliberately
reported NOT_UPLOAD_READY. Use --with-source to include original data and
mark the three-file directory ready for Browser/Backend upload.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGACY = ROOT / "legacy_models"
DEFAULT_OUT = ROOT / "datasets" / "dataset_uploads"
SOURCES = {"uci": ("uci", "default of credit card clients.xls"),
           "ulb": ("ulb", "creditcard.csv")}
EXPECTED_IDS = {"uci": "uci_credit", "ulb": "ulb_fraud"}
EXPECTED_COUNTS = {"uci": 30000, "ulb": 284807}
REQUIRED_DESCRIPTOR = {
    "dataset_id", "name", "version", "source_url", "license",
    "source_filename", "sha256", "row_count", "target_column",
    "positive_class", "row_id_rule", "audit_only_columns",
    "split_file", "split_sha256", "partition_counts",
}

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from legacy_models.shared.restore_ulb_split import restore as restore_ulb_split


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for piece in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(piece)
    return hasher.hexdigest()


def descriptor_for(dataset, *, legacy_root=LEGACY):
    path = legacy_root / "dataset_descriptors" / dataset / "dataset.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    missing = REQUIRED_DESCRIPTOR - set(doc)
    if missing:
        raise ValueError(f"{dataset}: descriptor missing required keys: {sorted(missing)}")
    if doc["dataset_id"] != EXPECTED_IDS[dataset]:
        raise ValueError(f"{dataset}: unexpected dataset_id")
    if doc["row_count"] != EXPECTED_COUNTS[dataset]:
        raise ValueError(f"{dataset}: unexpected row_count")
    if doc["source_filename"] != SOURCES[dataset][1]:
        raise ValueError(f"{dataset}: unexpected source filename")
    if doc["split_file"] != f"{dataset}_split_ids.csv":
        raise ValueError(f"{dataset}: unexpected split filename")
    for key in ("sha256", "split_sha256"):
        value = doc[key]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError(f"{dataset}: malformed {key}")
    if (not isinstance(doc["partition_counts"], dict) or
            set(doc["partition_counts"]) != {"train", "validation", "test"} or
            any(type(v) is not int or v <= 0 for v in doc["partition_counts"].values()) or
            sum(doc["partition_counts"].values()) != doc["row_count"]):
        raise ValueError(f"{dataset}: invalid partition_counts")
    return path, doc


def verify_manifest_dataset_linkage(dataset_id, checksum, *, legacy_root=LEGACY):
    """No binary model may silently refer to a different source-data hash."""
    models = []
    for path in sorted(legacy_root.glob("*/manifest.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("dataset_id") != dataset_id:
            continue
        observed = doc.get("dataset_sha256", doc.get("dataset_sha256_expected"))
        if observed != checksum:
            raise ValueError(f"{path.parent.name}: dataset SHA-256 disagrees with {dataset_id} descriptor")
        models.append(path.parent.name)
    if len(models) != 4:
        raise ValueError(f"{dataset_id}: expected four linked binary models, got {len(models)}")
    return models


def verify_split(path, dataset, descriptor):
    """Streaming row-order, uniqueness, exact partition counts and SHA check."""
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{dataset}: split missing, invalid or symlink")
    counts = Counter()
    rows = 0
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["row_id", "split"]:
            raise ValueError(f"{dataset}: split CSV must have row_id,split columns")
        for i, record in enumerate(reader):
            if None in record:
                raise ValueError(f"{dataset}: malformed split row")
            expected = f"uci-{i+1:06d}" if dataset == "uci" else f"ulb-{i:06d}"
            if record["row_id"] != expected:
                raise ValueError(f"{dataset}: unexpected/out-of-order split ID at row {i}: {record['row_id']}")
            if record["split"] not in ("train", "validation", "test"):
                raise ValueError(f"{dataset}: invalid split assignment")
            counts[record["split"]] += 1
            rows += 1
    if rows != descriptor["row_count"] or dict(counts) != descriptor["partition_counts"]:
        raise ValueError(f"{dataset}: split partition counts do not match descriptor")
    actual = digest(path)
    if actual != descriptor["split_sha256"]:
        raise ValueError(f"{dataset}: split SHA-256 mismatch: {actual}")
    return {"status": "PASS", "rows": rows, "partition_counts": dict(counts),
            "sha256": actual, "filename": path.name}


def copy_checked(source, target, expected_sha):
    """Idempotent, stream-copy source bytes without touching the original."""
    source = Path(source)
    target = Path(target)
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"Missing or unsafe original source: {source}")
    actual = digest(source)
    if actual != expected_sha:
        raise ValueError(f"Source checksum mismatch for {source}: {actual}")
    if target.exists():
        if not target.is_file() or target.is_symlink() or digest(target) != expected_sha:
            raise ValueError(f"Existing upload file differs; refuse overwrite: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".stage-", dir=target.parent, delete=False) as tmp:
        pending = Path(tmp.name)
    try:
        shutil.copyfile(source, pending)
        if digest(pending) != expected_sha:
            raise ValueError(f"Staged file differs from source: {source}")
        pending.replace(target)
    finally:
        pending.unlink(missing_ok=True)


def stage_dataset(dataset, out_dir, data_root, *, with_source=False,
                  verify_only=False, legacy_root=LEGACY):
    descriptor_path, descriptor = descriptor_for(dataset, legacy_root=legacy_root)
    linked_models = verify_manifest_dataset_linkage(
        descriptor["dataset_id"], descriptor["sha256"], legacy_root=legacy_root)
    target_dir = out_dir / dataset
    staged_descriptor = target_dir / "dataset.json"
    staged_split = target_dir / descriptor["split_file"]
    original_source = data_root / SOURCES[dataset][0] / SOURCES[dataset][1]
    staged_source = target_dir / descriptor["source_filename"]

    if not verify_only:
        copy_checked(descriptor_path, staged_descriptor, digest(descriptor_path))
        if dataset == "uci":
            canonical = legacy_root / "evidence" / "uci_split_ids.csv"
            verify_split(canonical, dataset, descriptor)
            copy_checked(canonical, staged_split, descriptor["split_sha256"])
        else:
            # The canonical 284,807-row split is intentionally not a Git blob.
            # Existing restore routine refuses to overwrite a mismatched file.
            restore_ulb_split(staged_split)
        if with_source:
            copy_checked(original_source, staged_source, descriptor["sha256"])

    if (not staged_descriptor.is_file() or staged_descriptor.is_symlink() or
            digest(staged_descriptor) != digest(descriptor_path)):
        raise ValueError(f"{dataset}: staged dataset.json differs from checked-in descriptor")
    frozen = verify_split(staged_split, dataset, descriptor)
    source_status = "NOT_STAGED"
    if with_source:
        if not staged_source.is_file() or staged_source.is_symlink():
            raise ValueError(f"{dataset}: original source is not present in staged upload")
        staged_sha = digest(staged_source)
        if staged_sha != descriptor["sha256"]:
            raise ValueError(f"{dataset}: staged original data checksum mismatch: {staged_sha}")
        source_status = "PASS"
    else:
        # Do not lie about upload readiness even if a source was staged earlier.
        # The supplied mode must explicitly request source integrity checks.
        source_status = "NOT_VERIFIED"
    return {
        "dataset": dataset, "dataset_id": descriptor["dataset_id"],
        "source_name": descriptor["source_filename"],
        "descriptor": {"status": "PASS", "sha256": digest(staged_descriptor)},
        "split": frozen, "source_status": source_status,
        "upload_ready": source_status == "PASS",
        "files_for_backend": [
            str(staged_descriptor), str(staged_split),
            str(staged_source) if source_status == "PASS" else None],
        "linked_models": linked_models,
        "notes": ("Three files verified and staged. Browser/Backend upload acceptance still pending."
                  if source_status == "PASS" else
                  "Two metadata/split files staged; use --with-source for the full three-file upload."),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--data-root", type=Path, default=ROOT / "datasets",
                        help="Directory containing original datasets under uci/ and ulb/")
    parser.add_argument("--dataset", action="append", choices=tuple(SOURCES),
                        help="Select uci or ulb (repeatable); default both")
    parser.add_argument("--with-source", action="store_true",
                        help="Stage and verify original data: required for 3-file upload readiness")
    parser.add_argument("--verify-only", action="store_true",
                        help="Validate existing staged files; never regenerate/overwrite")
    args = parser.parse_args(argv)
    selected = list(dict.fromkeys(args.dataset or SOURCES))
    result = {"format_version": "1", "purpose": "P0 three-file dataset upload readiness",
              "with_source": args.with_source, "datasets": [], "all_upload_ready": False}
    failed = False
    for dataset in selected:
        try:
            result["datasets"].append(stage_dataset(
                dataset, args.out_dir, args.data_root,
                with_source=args.with_source, verify_only=args.verify_only))
        except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            failed = True
            result["datasets"].append({
                "dataset": dataset, "upload_ready": False,
                "status": "FAIL", "error": str(error)})
    result["all_upload_ready"] = (not failed and
        len(result["datasets"]) == len(selected) and
        all(record["upload_ready"] for record in result["datasets"]))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "build_report.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
