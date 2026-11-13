#!/usr/bin/env python3
"""Build portable, integrity-checked legacy model ZIPs for Issue #10.

Run from any working directory. This does NOT retrain, modify source packages,
relabel historical provenance, or include raw datasets/test labels. Generated
ZIPs live in datasets/upload_bundles/ by default (gitignored).

Each ZIP has manifest.json at its root. For the six binary Python models, the
shared Python modules are copied under shared/ and the two tiny entry-point
wrappers are rewritten *inside the ZIP only* to import from that location.
The original source files/hashes are retained in bundle_provenance.json.

--smoke loads serialized model artifacts. Use it ONLY with trusted artifacts
inside Justin's isolated sandbox image, never on untrusted uploaded packages.
"""
import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
LEGACY = ROOT / "legacy_models"
DEFAULT_OUT = ROOT / "datasets" / "upload_bundles"
MODEL_IDS = (
    "credit_logistic_r", "fraud_logistic_r",
    "credit_logistic_python", "fraud_logistic_python",
    "credit_xgboost_python", "fraud_xgboost_python",
    "credit_lightgbm_python", "fraud_lightgbm_python",
    "credit_cox_python",
)
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", ".git", ".venv", "private_test"}
ALLOWED_SUFFIXES = {".py", ".R", ".rds", ".joblib", ".json", ".md", ".txt", ".csv"}
PUBLIC_FIXTURES = {"fixtures/development_inputs.csv", "fixtures/reference_predictions.csv"}
PUBLIC_TOP_LEVEL = {"manifest.json", "schema.json", "evaluation.json",
                    "fairness.json", "r_coefficients.json", "README.md",
                    "score.py", "score.R", "train.py", "train.R",
                    "model.rds", "model.joblib", "generate_demo.py"}
SHARED_IMPORT_OLD = "Path(__file__).resolve().parents[1]/'shared'"
SHARED_IMPORT_NEW = "Path(__file__).resolve().parent/'shared'"
COX_PACKAGE_OLD = 'default="legacy_models/credit_cox_python"'
COX_PACKAGE_NEW = "default=str(Path(__file__).resolve().parent)"


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def checked_name(name):
    path = PurePosixPath(name)
    if (not name or name.startswith("/") or "\\" in name
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != name):
        raise ValueError("Unsafe ZIP entry: " + name)
    return name


def read_source_files(folder):
    """Select reviewable model files only, never local secrets or data caches."""
    if not folder.is_dir():
        raise FileNotFoundError("Model directory missing: " + str(folder))
    result = {}
    for file in sorted(folder.rglob("*")):
        relative = file.relative_to(folder)
        if any(part in EXCLUDE_PARTS or part.startswith(".") for part in relative.parts):
            continue
        if file.is_symlink():
            raise ValueError("Symlink is not allowed in bundle: " + str(file))
        if not file.is_file() or file.suffix not in ALLOWED_SUFFIXES:
            continue
        name = checked_name(relative.as_posix())
        if relative.parts[0] == "fixtures":
            if name not in PUBLIC_FIXTURES:
                continue
        elif relative.parts[0] == "documentation":
            if file.suffix not in {".md", ".txt"}:
                continue
        elif len(relative.parts) != 1 or not (
                name in PUBLIC_TOP_LEVEL or
                (name.startswith("requirements") and file.suffix == ".txt")):
            continue
        result[name] = file.read_bytes()
    return result


def change_wrapper(files, name, old, new, mandatory):
    if name not in files:
        if mandatory:
            raise ValueError("Required wrapper missing: " + name)
        return False
    source = files[name].decode("utf-8")
    if old not in source:
        if mandatory:
            raise ValueError("Unknown wrapper layout in " + name + ": refuses unsafe rewrite")
        return False
    rewritten = source.replace(old, new)
    if rewritten.count(new) != source.count(old):
        raise ValueError("Unexpected wrapper replacement count: " + name)
    files[name] = rewritten.encode("utf-8")
    return True


def model_artifacts(manifest):
    paths = manifest.get("artifact_paths", [])
    paths = [paths] if isinstance(paths, str) else paths
    if not isinstance(paths, list) or len(paths) != 1:
        raise ValueError("Expected exactly one trusted artifact per current model")
    return [checked_name(x) for x in paths]


def assemble(model_id, legacy_root=LEGACY):
    """Return ZIP entry bytes, with no changes to checked-in source files."""
    if model_id not in MODEL_IDS:
        raise ValueError("Unknown model: " + model_id)
    folder = legacy_root / model_id
    files = read_source_files(folder)
    required = {"manifest.json", "schema.json", "evaluation.json"}
    required.add("score.R" if model_id.endswith("_r") else "score.py")
    missing = sorted(required - set(files))
    if missing:
        raise ValueError(model_id + ": required package files missing: " + str(missing))

    original_manifest = files["manifest.json"]
    manifest = json.loads(original_manifest)
    if manifest.get("name") != model_id:
        raise ValueError("Model name does not match directory")
    if manifest.get("model_version") is None:
        raise ValueError("Missing version")
    if not all(manifest.get(key) for key in (
            "purpose", "intended_use", "model_owner", "risk_tier", "tier_rationale")):
        raise ValueError("Issue #10 inventory fields are incomplete")
    for artifact in model_artifacts(manifest):
        if artifact not in files or digest(files[artifact]) != manifest.get("artifact_sha256"):
            raise ValueError(model_id + ": saved model artifact missing or hash mismatch")

    is_python_binary = model_id.endswith("_python") and model_id != "credit_cox_python"
    notes = []
    modified = []
    provenance = {
        "format_version": "1",
        "model_id": model_id,
        "source_manifest_sha256": digest(original_manifest),
        "source_scoring_code_sha256_claimed": manifest.get("scoring_code_sha256"),
        "source_training_code_sha256_claimed": manifest.get("model_code_sha256"),
        "source_shared_pipeline_sha256_claimed": manifest.get("shared_pipeline_sha256"),
        "source_files_rewritten_in_bundle_only": modified,
    }
    scorer = "score.py" if model_id.endswith("_python") else "score.R"
    provenance["source_scoring_code_sha256_observed"] = digest(files[scorer])
    if "train.py" in files:
        provenance["source_training_code_sha256_observed"] = digest(files["train.py"])

    if is_python_binary:
        shared = legacy_root / "shared"
        if not shared.is_dir():
            raise ValueError("Missing legacy_models/shared directory")
        for file in sorted(shared.glob("*.py")):
            if file.is_symlink() or not file.is_file():
                raise ValueError("Unexpected shared Python module: " + str(file))
            files[checked_name("shared/" + file.name)] = file.read_bytes()
        for necessary in ("pipeline.py", "score_python.py", "train_python.py"):
            if "shared/" + necessary not in files:
                raise ValueError("Missing required shared module: " + necessary)
        if not change_wrapper(files, "score.py",
                              SHARED_IMPORT_OLD, SHARED_IMPORT_NEW, True):
            raise ValueError("Cannot relocate scorer")
        modified.append("score.py")
        if "train.py" in files:
            if not change_wrapper(files, "train.py",
                                  SHARED_IMPORT_OLD, SHARED_IMPORT_NEW, True):
                raise ValueError("Cannot relocate training wrapper")
            modified.append("train.py")
        observed = digest(files["shared/pipeline.py"])
        provenance["shared_pipeline_sha256_observed_in_bundle"] = observed
        claimed = manifest.get("shared_pipeline_sha256")
        history = manifest.get("historical_shared_pipeline_sha256_claimed")
        provenance["source_shared_pipeline_claim_matches_observed"] = claimed == observed
        provenance["historical_shared_pipeline_sha256_claimed"] = history
        provenance["historical_shared_pipeline_claim_matches_observed"] = (
            history == observed if history is not None else None)
        if claimed != observed:
            raise ValueError(model_id + ": source shared_pipeline_sha256 does not "
                             "match current shared/pipeline.py; reconcile source "
                             "manifest before creating upload package")
        if history is not None and history != observed:
            notes.append("REVIEW: preserved original historical pipeline hash differs "
                         "from current upload code; original fit-time provenance "
                         "remains unverified")
        # These refer to the actual uploaded code, not to the historical source.
        manifest["shared_pipeline_sha256"] = observed
        manifest["scoring_code_sha256"] = digest(files["score.py"])
        if "train.py" in files:
            manifest["model_code_sha256"] = digest(files["train.py"])
    elif model_id == "credit_cox_python":
        if change_wrapper(files, "score.py", COX_PACKAGE_OLD,
                          COX_PACKAGE_NEW, True):
            modified.append("score.py")
        if "train.py" in files and change_wrapper(
                files, "train.py", COX_PACKAGE_OLD, COX_PACKAGE_NEW, False):
            modified.append("train.py")
        # Cox does not share the binary-model pipeline or output contract.
        if manifest.get("task") != "survival_analysis" or manifest.get("dataset_id") is not None:
            raise ValueError("Cox must be an explicitly synthetic, dataset-free survival demo")
    if "score.py" in files and (is_python_binary or model_id == "credit_cox_python"):
        manifest["scoring_code_sha256"] = digest(files["score.py"])

    files["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True)
                              + "\n").encode("utf-8")
    provenance["bundled_manifest_sha256"] = digest(files["manifest.json"])
    provenance["bundled_scoring_code_sha256"] = digest(files[scorer])
    provenance["bundled_training_code_sha256"] = (
        digest(files["train.py"]) if "train.py" in files else None)
    provenance["warnings"] = notes
    provenance["file_sha256"] = {key: digest(data) for key, data in sorted(files.items())}
    files["bundle_provenance.json"] = (
        json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return files, notes


def verify_archive(path):
    """Static ZIP verification; never deserializes a saved model."""
    with zipfile.ZipFile(path, "r") as archive:
        members = archive.infolist()
        names = [checked_name(info.filename) for info in members]
        if len(names) != len(set(names)) or len(names) > 256:
            raise ValueError("Duplicate/too many ZIP entries")
        for info in members:
            if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Directory or symlink ZIP entry not permitted")
            if info.file_size > 32 * 1024 * 1024:
                raise ValueError("Oversize ZIP entry: " + info.filename)
        if not {"manifest.json", "schema.json", "bundle_provenance.json"}.issubset(names):
            raise ValueError("Required root-level files absent")
        manifest = json.loads(archive.read("manifest.json"))
        provenance = json.loads(archive.read("bundle_provenance.json"))
        expected = provenance["file_sha256"]
        if set(expected) != (set(names) - {"bundle_provenance.json"}):
            raise ValueError("ZIP content differs from provenance file list")
        for name, expected_digest in expected.items():
            if digest(archive.read(name)) != expected_digest:
                raise ValueError("File checksum mismatch: " + name)
        if provenance["bundled_manifest_sha256"] != digest(archive.read("manifest.json")):
            raise ValueError("Bundle manifest checksum mismatch")
        for model_file in model_artifacts(manifest):
            if digest(archive.read(model_file)) != manifest["artifact_sha256"]:
                raise ValueError("Model artifact checksum mismatch")
        if manifest.get("language") == "Python":
            if digest(archive.read("score.py")) != manifest.get("scoring_code_sha256"):
                raise ValueError("Packaged scorer checksum mismatch")
            if manifest.get("task") == "binary_classification":
                if (digest(archive.read("shared/pipeline.py"))
                        != manifest.get("shared_pipeline_sha256")):
                    raise ValueError("Packaged pipeline checksum mismatch")
                if SHARED_IMPORT_NEW not in archive.read("score.py").decode("utf-8"):
                    raise ValueError("Scorer does not import bundled shared modules")
        return {"model": manifest["name"], "version": manifest["model_version"],
                "files": len(names), "static_integrity": "PASS",
                "warnings": provenance.get("warnings", [])}


def write_zip(path, files):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".zip.pending")
    try:
        with zipfile.ZipFile(temp, mode="w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9, allowZip64=False) as archive:
            for name, data in sorted(files.items()):
                info = zipfile.ZipInfo(checked_name(name), date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED,
                                 compresslevel=9)
        verdict = verify_archive(temp)
        temp.replace(path)
        verdict["zip_sha256"] = digest(path.read_bytes())
        verdict["zip_size_bytes"] = path.stat().st_size
        return verdict
    finally:
        temp.unlink(missing_ok=True)


def smoke_test(path, atol):
    """Execute only trusted local artifacts in a pre-existing isolated sandbox."""
    with zipfile.ZipFile(path, "r") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("task") == "survival_analysis":
            return {"status": "NOT_APPLICABLE", "reason": "Synthetic Cox: structural checks only"}
        required = {"fixtures/development_inputs.csv",
                    "fixtures/reference_predictions.csv"}
        if not required.issubset(archive.namelist()):
            return {"status": "FAIL", "reason": "Development scoring fixtures absent"}
        with tempfile.TemporaryDirectory(prefix="mrg-bundle-") as folder:
            root = Path(folder)
            archive.extractall(root)
            out = root / "candidate_predictions.csv"
            command = ([sys.executable, "score.py"] if manifest["language"] == "Python"
                       else ["Rscript", "score.R"])
            command += ["--input", str(root / "fixtures/development_inputs.csv"),
                        "--output", str(out)]
            env = dict(os.environ)
            for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
                env.pop(key, None)
            try:
                result = subprocess.run(command, cwd=root, env=env, timeout=120,
                                        capture_output=True, text=True, check=False)
            except (FileNotFoundError, subprocess.TimeoutExpired) as error:
                return {"status": "FAIL", "reason": str(error)}
            if result.returncode != 0:
                return {"status": "FAIL", "reason": result.stderr[-1200:]}
            with out.open(newline="", encoding="utf-8") as stream:
                scored = list(csv.DictReader(stream))
            with (root / "fixtures/reference_predictions.csv").open(
                    newline="", encoding="utf-8") as stream:
                expected = list(csv.DictReader(stream))
            if not scored or len(scored) != len(expected):
                return {"status": "FAIL", "reason": "Prediction row-count mismatch"}
            diffs = []
            for a, b in zip(scored, expected):
                if a["row_id"] != b["row_id"]:
                    return {"status": "FAIL", "reason": "Prediction row-ID/order mismatch"}
                value = float(a["positive_probability"])
                target = float(b["positive_probability"])
                if not (math.isfinite(value) and 0 <= value <= 1
                        and math.isfinite(target) and 0 <= target <= 1):
                    return {"status": "FAIL", "reason": "Nonfinite or invalid probability"}
                diffs.append(abs(value - target))
            worst = max(diffs)
            return {"status": "PASS" if worst <= atol else "FAIL",
                    "rows": len(diffs), "max_abs_difference": worst,
                    "fixture_atol": atol, "reason": "Development-fixture scoring only; "
                    "not official rebuild parity or protected-test validation"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--model", action="append", choices=MODEL_IDS,
                        help="One or more model IDs; default builds all nine")
    parser.add_argument("--verify-only", action="store_true",
                        help="Check existing ZIPs rather than rebuilding them")
    parser.add_argument("--smoke", action="store_true",
                        help="Run development scorers; ONLY in trusted isolated sandbox")
    parser.add_argument("--fixture-atol", type=float, default=1e-7,
                        help="Development-fixture smoke tolerance, not official parity")
    args = parser.parse_args()
    if not math.isfinite(args.fixture_atol) or args.fixture_atol < 0:
        parser.error("--fixture-atol must be nonnegative and finite")
    selected = args.model or MODEL_IDS
    report = {"format_version": "1", "purpose": "P0 upload ZIP readiness",
              "smoke_executed": args.smoke, "models": []}
    failed = False
    for model_id in selected:
        path = args.out_dir / (model_id + ".zip")
        try:
            if not args.verify_only:
                entries, _ = assemble(model_id)
                item = write_zip(path, entries)
            else:
                item = verify_archive(path)
                item["zip_sha256"] = digest(path.read_bytes())
                item["zip_size_bytes"] = path.stat().st_size
            item["path"] = str(path)
            item["smoke"] = smoke_test(path, args.fixture_atol) if args.smoke else {
                "status": "NOT_RUN"}
            failed |= item["smoke"]["status"] == "FAIL"
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as error:
            failed = True
            item = {"model": model_id, "static_integrity": "FAIL", "error": str(error)}
        report["models"].append(item)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "build_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
