#!/usr/bin/env python3
"""Verify and stage nine small model binaries as ordinary Git blobs, not LFS.

Must run on a trusted Mac checkout with both RDS files already compacted.
Does not train, alter model files, commit, push or change original audit history.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODELS = (
    "credit_logistic_r", "credit_logistic_python", "credit_xgboost_python",
    "credit_lightgbm_python", "fraud_logistic_r", "fraud_logistic_python",
    "fraud_xgboost_python", "fraud_lightgbm_python", "credit_cox_python",
)
OLD_RULES = {
    "legacy_models/**/*.joblib filter=lfs diff=lfs merge=lfs -text":
        "legacy_models/**/*.joblib -filter -diff -merge -text",
    "legacy_models/**/*.rds filter=lfs diff=lfs merge=lfs -text":
        "legacy_models/**/*.rds -filter -diff -merge -text",
}



def normalize_model_attributes(lines: list[str]) -> str:
    """Allow a restart after an earlier run already changed .gitattributes.

    Require exactly one recognized old or plain-Git rule for EACH extension.
    Never silently overwrite unrelated or contradictory attribute rules.
    """
    normalized = list(lines)
    for lfs_rule, plain_rule in OLD_RULES.items():
        count_old = normalized.count(lfs_rule)
        count_plain = normalized.count(plain_rule)
        if count_old + count_plain != 1:
            raise ValueError(
                "Unexpected existing LFS tracking rules for "
                + lfs_rule.split()[0]
                + "; expected exactly one old or plain-Git rule. "
                + "Inspect .gitattributes using: cat .gitattributes"
            )
        if count_old:
            normalized[normalized.index(lfs_rule)] = plain_rule
    return "\n".join(normalized) + "\n"


def run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def lfs_pointer(content: bytes) -> tuple[str, int]:
    text = content.decode("ascii")
    if not text.startswith("version https://git-lfs.github.com/spec/v1\n"):
        raise ValueError("The original Git index is not an LFS pointer")
    digest = re.search(r"^oid sha256:([a-f0-9]{64})$", text, re.MULTILINE)
    size = re.search(r"^size (\d+)$", text, re.MULTILINE)
    if digest is None or size is None:
        raise ValueError("Malformed original LFS pointer")
    return digest.group(1), int(size.group(1))


def verify_binary(model: str, manifest: dict, path: Path,
                  historical_sha: str, historical_bytes: int) -> tuple[str, int]:
    if not path.is_file():
        raise ValueError(f"{model}: binary missing from the working tree")
    if path.read_bytes()[:45].startswith(b"version https://git-lfs.github.com/spec/"):
        raise ValueError(f"{model}: only LFS pointer available; do not stage it")
    size = path.stat().st_size
    max_bytes = 1_000_000 if model.endswith("_r") else 2_000_000
    if not 0 < size < max_bytes:
        raise ValueError(f"{model}: artifact not compact ({size} bytes)")
    sha = sha256(path)
    if sha != manifest.get("artifact_sha256"):
        raise ValueError(f"{model}: binary does not match manifest SHA-256")
    if model.endswith("_r"):
        comp = manifest.get("artifact_compaction") or {}
        difference = comp.get("max_absolute_prediction_difference")
        if (comp.get("format") != "glm_coefficients_only_v1"
            or comp.get("original_artifact_sha256") != historical_sha
            or comp.get("retrained") is not False
            or comp.get("development_rows_checked") != 100
            or type(difference) not in (float, int)
            or not math.isfinite(difference) or not 0 <= difference <= 1e-12
            or not size < historical_bytes):
            raise ValueError(f"{model}: invalid coefficients-only compaction lineage")
    elif sha != historical_sha:
        raise ValueError(f"{model}: original Python/synthetic artifact changed")
    return sha, size


def check_r_scores(model: str) -> None:
    if shutil.which("Rscript") is None:
        raise ValueError("Rscript missing; R saved-artifact scoring must be verified")
    package = ROOT / "legacy_models" / model
    fixture = package / "fixtures/development_inputs.csv"
    reference = package / "fixtures/reference_predictions.csv"
    with tempfile.TemporaryDirectory(prefix="exl_r_compact_") as folder:
        result = Path(folder) / "scores.csv"
        run("Rscript", str(package / "score.R"), "--package", str(package),
            "--input", str(fixture), "--output", str(result),
            capture_output=True, text=True)
        with reference.open(newline="") as file:
            expected = list(csv.DictReader(file))
        with result.open(newline="") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames != ["row_id", "positive_probability"]:
                raise ValueError(f"{model}: invalid score output schema")
            got = list(reader)
        if (len(expected) != 100 or len(got) != 100 or
            [x["row_id"] for x in expected] != [x["row_id"] for x in got]):
            raise ValueError(f"{model}: development row IDs differ")
        p = [float(x["positive_probability"]) for x in expected]
        q = [float(x["positive_probability"]) for x in got]
        if any(not math.isfinite(x) or x < 0 or x > 1 for x in (*p, *q)):
            raise ValueError(f"{model}: invalid probability")
        difference = max(abs(x - y) for x, y in zip(p, q))
        if difference > 1e-10:
            raise ValueError(f"{model}: development fixture mismatch ({difference})")
        print(f"  {model}: 100-row scoring PASS, max diff {difference:.3g}")


def stage_unfiltered_blob(path: str) -> None:
    """Stage exact already-verified bytes without invoking Git LFS clean filters.

    Git add may re-stage an existing LFS pointer while the working-tree binary
    remains clean/smudged. Using Git's raw plumbing bypasses all clean filters,
    then validates the staged SHA and size against the trusted local file.
    """
    source = ROOT / path
    expected_sha = run("git", "hash-object", "--no-filters", "--", path,
                       capture_output=True, text=True).stdout.strip()
    saved_sha = run("git", "hash-object", "-w", "--no-filters", "--", path,
                    capture_output=True, text=True).stdout.strip()
    if saved_sha != expected_sha or len(saved_sha) != 40:
        raise ValueError(f"{path}: could not store exact unfiltered Git blob")
    run("git", "update-index", "--add", "--cacheinfo",
        f"100644,{saved_sha},{path}")
    indexed_sha = run("git", "rev-parse", ":" + path,
                      capture_output=True, text=True).stdout.strip()
    indexed_size = int(run("git", "cat-file", "-s", ":" + path,
                           capture_output=True, text=True).stdout.strip())
    if indexed_sha != saved_sha or indexed_size != source.stat().st_size:
        raise ValueError(f"{path}: staged object is not the original binary")


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--check-only", action="store_true")
    args = cli.parse_args()
    branch = run("git", "branch", "--show-current",
                 capture_output=True, text=True).stdout.strip()
    if branch != "legacy":
        raise ValueError(f"Expected legacy branch, got {branch}")
    # Resume a prior failed migration, but never overwrite unrelated staged work.
    staged = set(run("git", "diff", "--cached", "--name-only",
                     capture_output=True, text=True).stdout.splitlines())
    allowed = {".gitattributes"}
    for name in MODELS:
        allowed.add(f"legacy_models/{name}/" +
                    ("model.rds" if name.endswith("_r") else "model.joblib"))
        if name.endswith("_r"):
            allowed.add(f"legacy_models/{name}/manifest.json")
    unrelated = staged - allowed
    if not args.check_only and unrelated:
        raise ValueError("Unrelated changes are already staged; unstage only those: " +
                         ", ".join(sorted(unrelated)))
    if not args.check_only and staged:
        print("Resuming partially staged model migration; preserving verified model bytes.")
    attribute_file = ROOT / ".gitattributes"
    current_attributes = attribute_file.read_text()
    lines = current_attributes.splitlines()
    updated_attributes = normalize_model_attributes(lines)
    if updated_attributes == current_attributes:
        print("Already using plain-Git attributes; resuming a previous migration attempt.")

    artifacts = []
    metadata = []
    for model in MODELS:
        folder = ROOT / "legacy_models" / model
        manifest = json.loads((folder / "manifest.json").read_text())
        refs = manifest.get("artifact_paths")
        refs = [refs] if isinstance(refs, str) else refs
        extension = "model.rds" if model.endswith("_r") else "model.joblib"
        if refs != [extension]:
            raise ValueError(f"{model}: unexpected model artifact reference")
        relpath = f"legacy_models/{model}/{extension}"
        historical_content = run("git", "show", "HEAD:" + relpath,
                                 capture_output=True).stdout
        old_sha, old_bytes = lfs_pointer(historical_content)
        digest, size = verify_binary(model, manifest, ROOT / relpath, old_sha, old_bytes)
        print(f"  {model}: {size:,} bytes, SHA-256 {digest}")
        artifacts.append(relpath)
        if model.endswith("_r"):
            metadata.append(f"legacy_models/{model}/manifest.json")

    for model in ("credit_logistic_r", "fraud_logistic_r"):
        check_r_scores(model)
    if args.check_only:
        print("PASS: all nine artifacts verified; no files staged")
        return 0

    # Do not change attributes or Git staging until all model checks succeed.
    attribute_file.write_text(updated_attributes)
    try:
        # Stage attribute rules FIRST. Ordinary git add might otherwise keep
        # the already-indexed LFS pointer, even after a local attribute change.
        run("git", "add", "--", ".gitattributes", *metadata)
        for relpath in artifacts:
            attr = run("git", "check-attr", "--cached", "filter", "--", relpath,
                       capture_output=True, text=True).stdout.strip()
            if not attr.endswith("filter: unset"):
                raise ValueError(f"{relpath}: LFS filter still active ({attr})")
            stage_unfiltered_blob(relpath)
            print(f"  staged plain Git blob: {relpath}")
    except Exception:
        print("Staging incomplete; inspect git diff --cached. Nothing committed.",
              file=sys.stderr)
        raise

    print("\nPASS: nine real binaries staged as ordinary Git blobs.")
    print("Original SHA values are retained in the two R manifest compaction records.")
    print("Next: regenerate split provenance, standardization and strict validation.")
    print("Only after they pass, commit staged binaries and two R manifests.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print("MIGRATION FAILED:", exc, file=sys.stderr)
        raise SystemExit(1)
