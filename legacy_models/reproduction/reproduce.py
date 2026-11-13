#!/usr/bin/env python3
"""Reproduce EXL legacy training without modifying any released model or report.

Modes:
  --prepare-only: checksum-check original datasets and canonical frozen splits
  (default): train the eight binary models in an isolated temporary worktree,
  score the *released* 100-row development fixtures, and write an audit report.

No network by default. No refit touches the committed model packages.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "legacy_models" / "shared"
LOCK = ROOT / "legacy_models" / "provenance" / "requirements.txt"
SEED = 20261104
EXPECTED_PYTHON = "3.13.5"
MODELS = {
    "credit_logistic_python": ("uci", "logistic", "Python"),
    "credit_xgboost_python": ("uci", "xgboost", "Python"),
    "credit_lightgbm_python": ("uci", "lightgbm", "Python"),
    "fraud_logistic_python": ("ulb", "logistic", "Python"),
    "fraud_xgboost_python": ("ulb", "xgboost", "Python"),
    "fraud_lightgbm_python": ("ulb", "lightgbm", "Python"),
    "credit_logistic_r": ("uci", "logistic", "R"),
    "fraud_logistic_r": ("ulb", "logistic", "R"),
}
DATA = {
    "uci": ("uci/default of credit card clients.xls",
            "30c6be3abd8dcfd3e6096c828bad8c2f011238620f5369220bd60cfc82700933",
            "e8feb21a0c57c5d6c8344af11aaaab079064f259e4861faad8f4aef40fe6d6cb",
            {"train": (17981, 3968), "validation": (6037, 1351), "test": (5982, 1317)}),
    "ulb": ("ulb/creditcard.csv",
            "76274b691b16a6c49d3f159c883398e03ccd6d1ee12d9d8ee38f4b4b98551a89",
            "5d9edf4df20e866d3198e74893fd2e1cf75f5fc3c5b4e175efd5a4a478b827fa",
            {"train": (162992, 356), "validation": (61675, 59), "test": (60140, 77)}),
}
MAX_FIXTURE_DIFF = {"R": 1e-8, "Python": 1e-7}
def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checked_run(command: list[str], *, env: dict[str, str] | None = None) -> str:
    """Capture subprocess output for error reports, with live activity heartbeats.

    Model trainers do not stream progress reliably. A long refit must never
    appear indistinguishable from a hung container.
    """
    with subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as child:
        while True:
            try:
                stdout, stderr = child.communicate(timeout=30)
                break
            except subprocess.TimeoutExpired:
                print(f"[reproduce] still running: {Path(command[0]).name} "
                      f"(PID {child.pid})", flush=True)
        if child.returncode:
            raise RuntimeError(f"{Path(command[0]).name} exit {child.returncode}: "
                               f"{(stderr or stdout)[-1800:]}")
        return stdout[-1800:]


def deterministic_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(PYTHONHASHSEED="0", OPENBLAS_NUM_THREADS="1",
               OMP_NUM_THREADS="2", MKL_NUM_THREADS="1",
               NUMEXPR_NUM_THREADS="1", VECLIB_MAXIMUM_THREADS="1",
               PYTHONDONTWRITEBYTECODE="1", TZ="UTC")
    return env


def pinned_environment() -> dict:
    expected = {}
    for line in LOCK.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, version = line.strip().split("==")
            expected[name.lower()] = version
    observed = {}
    for name in expected:
        try:
            observed[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            observed[name] = None
    return {
        "python": platform.python_version(),
        "python_version_match": platform.python_version() == EXPECTED_PYTHON,
        "expected_python": EXPECTED_PYTHON,
        "platform": platform.platform(),
        "requirements_sha256": sha256(LOCK),
        "expected_python_packages": expected,
        "observed_python_packages": observed,
        "python_packages_match": expected == observed,
        "Rscript_available": bool(shutil.which("Rscript")),
        "R_dependency_versions": r_versions(),
        "R_version_lock": "not_locked; record exact version, do not claim cross-platform identity",
    }


def r_versions() -> dict | None:
    if not shutil.which("Rscript"):
        return None
    expr = ('cat(R.version.string,"\\n");'
            'for(p in c("readxl","digest","jsonlite")) {'
            'cat(p,"=",if(requireNamespace(p,quietly=TRUE))'
            'as.character(packageVersion(p)) else "MISSING","\\n",sep="")}')
    out = checked_run(["Rscript", "--vanilla", "-e", expr])
    return {"runtime_and_packages": out.strip().splitlines()}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def source_paths(data_root: Path, download_missing: bool) -> dict[str, Path]:
    paths = {domain: data_root / spec[0] for domain, spec in DATA.items()}
    missing = [domain for domain, path in paths.items() if not path.is_file()]
    if missing and download_missing:
        if data_root.resolve() != (ROOT / "datasets").resolve():
            raise ValueError("--download-missing works only with the default repo datasets directory")
        checked_run([sys.executable, str(ROOT / "download_datasets.py"), *missing])
    for domain, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"{domain} source missing: {path}; run python download_datasets.py uci ulb")
        actual = sha256(path)
        if actual != DATA[domain][1]:
            raise ValueError(f"{domain} source SHA-256 mismatch: {actual}")
    return paths


def prepare(data_root: Path, sandbox: Path, download_missing: bool) -> tuple[dict, dict]:
    paths = source_paths(data_root, download_missing)
    evidence = sandbox / "legacy_models" / "evidence"
    evidence.mkdir(parents=True)
    uci = ROOT / "legacy_models/evidence/uci_split_ids.csv"
    if not uci.is_file() or sha256(uci) != DATA["uci"][2]:
        raise ValueError("Canonical UCI split CSV missing or hash mismatch; do not regenerate it")
    shutil.copyfile(uci, evidence / "uci_split_ids.csv")
    sys.path.insert(0, str(SHARED))
    from restore_ulb_split import restore
    from pipeline import read_data, freeze_split, DATA as PIPELINE_DATA
    restore(evidence / "ulb_split_ids.csv")
    result = {}
    for domain, path in paths.items():
        split = evidence / f"{domain}_split_ids.csv"
        if sha256(split) != DATA[domain][2]:
            raise ValueError(f"Canonical {domain} split hash mismatch")
        frame, _ = read_data(domain, path)
        parts = freeze_split(domain, frame, evidence)
        counts = {
            key: (int((parts == key).sum()),
                  int(frame.loc[parts == key, PIPELINE_DATA[domain]["target"]].sum()))
            for key in ("train", "validation", "test")
        }
        if counts != DATA[domain][3]:
            raise ValueError(f"{domain} split partition counts changed: {counts}")
        result[domain] = {
            "source_sha256": sha256(path), "split_sha256": sha256(split),
            "partition_counts": {key: {"rows": count, "positives": positives}
                                 for key, (count, positives) in counts.items()},
            "split_definition_verified": True,
        }
    return result, paths


def fixture_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError(f"Missing/duplicated fixture headers: {path}")
        return list(reader)


def compare_development(expected_inputs: Path, produced_inputs: Path,
                        expected_scores: Path, produced_scores: Path,
                        tolerance: float) -> dict:
    left, right = fixture_rows(expected_inputs), fixture_rows(produced_inputs)
    a, b = fixture_rows(expected_scores), fixture_rows(produced_scores)
    if (len(left) != 100 or len(right) != 100 or len(a) != 100 or len(b) != 100):
        raise ValueError("Exactly 100 development rows are required")
    if (list(left[0]) != list(right[0]) or "row_id" not in left[0] or
        list(a[0]) != ["row_id", "positive_probability"] or
        list(b[0]) != ["row_id", "positive_probability"]):
        raise ValueError("Development fixture schema drift")
    ids = [record["row_id"] for record in left]
    if (len(set(ids)) != len(ids) or any(not key for key in ids) or
        ids != [x["row_id"] for x in right] or
        ids != [x["row_id"] for x in a] or
        ids != [x["row_id"] for x in b]):
        raise ValueError("Development row IDs/order diverged")
    feature_diff = 0.0
    for older, rebuilt in zip(left, right):
        for name in left[0]:
            if name != "row_id":
                x, y = float(older[name]), float(rebuilt[name])
                if not math.isfinite(x) or not math.isfinite(y):
                    raise ValueError("Nonfinite feature value")
                feature_diff = max(feature_diff, abs(x - y))
                if not math.isclose(x, y, rel_tol=1e-10, abs_tol=1e-9):
                    raise ValueError(f"Development feature values differ: {name}")
    differences = []
    for older, rebuilt in zip(a, b):
        x, y = float(older["positive_probability"]), float(rebuilt["positive_probability"])
        if not (math.isfinite(x) and math.isfinite(y) and 0 <= x <= 1 and 0 <= y <= 1):
            raise ValueError("Invalid probability")
        differences.append(abs(x - y))
    maximum = max(differences)
    return {"rows": 100, "max_abs_feature_difference": feature_diff,
            "max_abs_prediction_difference": maximum,
            "n_above_tolerance": sum(d > tolerance for d in differences),
            "tolerance_absolute": tolerance,
            "status": "PASS" if maximum <= tolerance else "DIVERGED"}


def metric_value(partition: dict, key: str) -> float:
    """Handle the actual released vs new Python training metric vocabularies."""
    aliases = (("pr_auc_average_precision_sklearn", "pr_auc_average_precision")
               if key == "pr_auc_average_precision" else (key,))
    for alias in aliases:
        value = partition.get(alias)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
    raise ValueError(f"Missing or nonfinite metric: {key} ({'/'.join(aliases)})")


def train_one(name: str, paths: dict, sandbox: Path, env: dict, retain: Path | None) -> dict:
    domain, algorithm, language = MODELS[name]
    published = ROOT / "legacy_models" / name
    released_manifest = read_json(published / "manifest.json")
    artifact_name = "model.rds" if language == "R" else "model.joblib"
    original = published / artifact_name
    if sha256(original) != released_manifest.get("artifact_sha256"):
        raise ValueError(f"{name}: released model artifact hash mismatch")
    target = sandbox / "legacy_models" / name
    target.mkdir(parents=True, exist_ok=False)
    data = str(paths[domain].resolve())
    if language == "Python":
        checked_run([sys.executable, str(SHARED / "train_python.py"),
                     "--domain", domain, "--algorithm", algorithm,
                     "--data", data, "--package", str(target)], env=env)
        scorer = [sys.executable, str(SHARED / "score_python.py")]
        train_file = SHARED / "train_python.py"
    else:
        if not shutil.which("Rscript"):
            raise RuntimeError("Rscript unavailable; R models cannot be reproduced")
        manifest = dict(released_manifest)
        # This is a NEW FIT, not the old packaging-only compaction. Do not
        # copy historical compaction attestation into the new release candidate.
        manifest.pop("artifact_compaction", None)
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        shutil.copyfile(published / "score.R", target / "score.R")
        train_file = published / "train.R"
        checked_run(["Rscript", str(train_file), "--domain", domain,
                     "--data", data, "--package", str(target),
                     "--splits", str(sandbox / "legacy_models/evidence" /
                                     f"{domain}_split_ids.csv")], env=env)
        checked_run([sys.executable, str(SHARED / "evaluate_r.py"),
                     "--domain", domain, "--data", data,
                     "--package", str(target)], env=env)
        scorer = ["Rscript", str(target / "score.R")]
    rebuilt_manifest = read_json(target / "manifest.json")
    generated = target / artifact_name
    actual_hash = sha256(generated)
    if actual_hash != rebuilt_manifest.get("artifact_sha256"):
        raise ValueError(f"{name}: refit artifact hash not bound to generated manifest")
    # Always score the SAME released development features for valid comparison.
    fixture = published / "fixtures/development_inputs.csv"
    scored = sandbox / f"{name}_development_scores.csv"
    checked_run([*scorer, "--package", str(target), "--input", str(fixture),
                 "--output", str(scored)], env=env)
    reference = published / "fixtures/reference_predictions.csv"
    comparison = compare_development(
        fixture, target / "fixtures/development_inputs.csv",
        reference, scored, MAX_FIXTURE_DIFF[language])
    if language == "R":
        replay = sandbox / f"{name}_python_replay.csv"
        parity_file = sandbox / f"{name}_r_parity.json"
        checked_run([sys.executable, str(SHARED / "migrate_r_coefficients.py"),
                     "--package", str(target), "--input", str(fixture),
                     "--output", str(replay)], env=env)
        checked_run([sys.executable, str(SHARED / "compare_parity.py"),
                     "--r", str(scored), "--python", str(replay),
                     "--output", str(parity_file)], env=env)
        comparison["r_to_python_coefficient_parity"] = read_json(parity_file)
    evaluation = read_json(target / "evaluation.json")
    released_eval = read_json(published / "evaluation.json")
    performance = {}
    # For post-release audit only. The original test labels are never used for
    # refitting or parameter selection, and raw test-level scores are not saved.
    for partition in ("validation", "test"):
        performance[partition] = {}
        for key in ("roc_auc", "pr_auc_average_precision"):
            new_value = metric_value(evaluation.get(partition, {}), key)
            old_value = metric_value(released_eval.get(partition, {}), key)
            performance[partition][key] = {
                "released": old_value, "refit": new_value,
                "difference": new_value - old_value,
            }
    if retain is not None:
        dest = retain / name
        dest.mkdir(parents=True, exist_ok=False)
        for item in (artifact_name, "manifest.json"):
            shutil.copyfile(target / item, dest / item)
        # Keep summary metrics but never carry forward the scratch-only private
        # test prediction pointer or any row-level held-out predictions.
        clean_evaluation = dict(evaluation)
        clean_evaluation.pop("private_test_predictions", None)
        clean_evaluation["reproduction_only_not_approved_release"] = True
        (dest / "evaluation.json").write_text(
            json.dumps(clean_evaluation, indent=2) + "\n", encoding="utf-8")
        shutil.copytree(target / "fixtures", dest / "fixtures")
        # Never copy the scratch private_test directory or test predictions.
    return {
        "status": comparison["status"],
        "language": language, "source_dataset": domain,
        "training_entrypoint_sha256": sha256(train_file),
        "released_artifact_sha256": sha256(original),
        "refit_artifact_sha256": actual_hash,
        "serialized_artifact_hash_same": actual_hash == sha256(original),
        "development_fixture_comparison": comparison,
        "performance_metrics_observed": performance,
        "training_only_fit": True,
        "historical_fit_time_attestation": False,
    }


def optional_source_commit() -> str | None:
    """Collect traceability metadata when Git is available.

    A minimal offline Docker image may omit Git. This is a report annotation,
    not a data/split/model integrity check, so it cannot block reproduction.
    """
    try:
        return checked_run(["git", "rev-parse", "HEAD"]).strip() or None
    except (OSError, RuntimeError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true",
                        help="Verify raw sources and frozen splits without training")
    parser.add_argument("--download-missing", action="store_true",
                        help="Explicitly allow downloader network access for missing datasets")
    parser.add_argument("--data-root", default=str(ROOT / "datasets"))
    parser.add_argument("--models", default="all",
                        help="Comma-separated model names or all (eight binary models)")
    parser.add_argument("--retain-rebuilt", action="store_true",
                        help="Keep refitted models/manifest/dev fixtures in ignored datasets/reports")
    parser.add_argument("--report-only", action="store_true",
                        help="Exit zero on measured refit divergence, but retain DIVERGED report status")
    args = parser.parse_args(argv)
    chosen = list(MODELS) if args.models == "all" else [
        value.strip() for value in args.models.split(",")
    ]
    if not chosen or len(set(chosen)) != len(chosen) or any(x not in MODELS for x in chosen):
        parser.error("Unknown or duplicated model in --models")
    output_dir = ROOT / "datasets/reports/reproduction"
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%dT%H%M%SZ") + f"_{os.getpid()}"
    report_path = output_dir / (run_id + ".json")
    state = {
        "report_kind": "isolated_legacy_training_reproduction",
        "schema_version": "1.0.0", "created_at_utc": now.isoformat(),
        "status": "STARTED", "seed": SEED,
        "source_branch_commit": None,
        "scope": ("Eight binary models; synthetic survival demo excluded. "
                  "No model approval, no new holdout, no fit-time history rewritten."),
        "environment": {}, "datasets": {}, "models": {},
    }
    try:
        print("[reproduce] checking runtime and dependency versions", flush=True)
        state["source_branch_commit"] = optional_source_commit()
        state["environment"] = pinned_environment()
        env = deterministic_env()
        with tempfile.TemporaryDirectory(prefix="scratch_", dir=output_dir) as scratch:
            sandbox = Path(scratch)
            print("[reproduce] verifying raw dataset hashes and frozen splits", flush=True)
            state["datasets"], paths = prepare(
                Path(args.data_root).resolve(), sandbox, args.download_missing)
            print("[reproduce] datasets and splits verified", flush=True)
            if not args.prepare_only:
                retained = output_dir / "rebuilt" / run_id if args.retain_rebuilt else None
                for i, name in enumerate(chosen, start=1):
                    print(f"[reproduce] training {i}/{len(chosen)}: {name}", flush=True)
                    try:
                        state["models"][name] = train_one(name, paths, sandbox, env, retained)
                    except Exception as exc:
                        state["models"][name] = {
                            "status": "ERROR", "type": type(exc).__name__,
                            "reason": str(exc)[:1800]}
                    print(f"[reproduce] {name}: {state['models'][name]['status']}", flush=True)
            bad = [x for x in state["models"].values() if x["status"] == "ERROR"]
            diverged = [x for x in state["models"].values() if x["status"] == "DIVERGED"]
            env_bad = (not state["environment"]["python_packages_match"] or
                       not state["environment"]["python_version_match"])
            state["status"] = ("ERROR" if bad else
                               "ENVIRONMENT_MISMATCH" if env_bad else
                               "DIVERGED" if diverged else "PASS")
    except Exception as exc:
        state["status"] = "ERROR"
        state["failure"] = {"type": type(exc).__name__, "reason": str(exc)[:1800]}
    with report_path.open("x", encoding="utf-8") as output:
        json.dump(state, output, indent=2, allow_nan=False)
        output.write("\n")
    print(json.dumps({"report": str(report_path), "status": state["status"],
                      "models": {key: item["status"]
                                 for key, item in state["models"].items()}}, indent=2))
    if state["status"] == "PASS" or (args.report_only and state["status"] == "DIVERGED"):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
