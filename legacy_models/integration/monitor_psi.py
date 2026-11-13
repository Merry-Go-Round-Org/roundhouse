#!/usr/bin/env python3
"""PSI baseline and monitoring adapter. Does NOT score models or implement HTTP.

Backend contract (Aditya): score a permitted batch in the trusted sandbox,
preserve row_id alignment, then call evaluate(...) on the immutable baseline.
The same legacy_models.stability.evaluate_window is used by validation.
The adapter never reads targets, test data, RDS or joblib objects.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from legacy_models.stability import (
    freeze_baseline, evaluate_window, validate_baseline, policy_config, file_hash,
)


def load_manifest(path):
    path = Path(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("task") != "binary_classification":
        raise ValueError("PSI score monitoring applies to binary models only")
    if not manifest.get("name") or not manifest.get("feature_order"):
        raise ValueError("Manifest lacks model identity or feature order")
    artifact = path.parent / "model.joblib"
    if manifest.get("language") == "R":
        artifact = path.parent / "model.rds"
    if not artifact.is_file() or file_hash(artifact) != manifest.get("artifact_sha256"):
        raise ValueError("Model artifact SHA-256 does not match manifest")
    return manifest


def freeze(*, manifest_path, reference_inputs, reference_scores, policy,
           reference_scope="development"):
    manifest = load_manifest(manifest_path)
    return freeze_baseline(
        model_id=manifest["name"], feature_order=manifest["feature_order"],
        reference_inputs=reference_inputs, reference_scores=reference_scores,
        artifact_sha256=manifest["artifact_sha256"],
        reference_scope=reference_scope, config=policy,
    )


def evaluate(*, baseline_path, current_inputs, current_scores,
             manifest_path=None):
    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    validate_baseline(baseline)
    if manifest_path:
        manifest = load_manifest(manifest_path)
        name, artifact_hash = manifest["name"], manifest["artifact_sha256"]
        if manifest["feature_order"] != baseline["feature_order"]:
            raise ValueError("Current model features disagree with frozen baseline")
    else:
        name, artifact_hash = baseline["model_id"], baseline["artifact_sha256"]
    return evaluate_window(
        baseline=baseline, current_inputs=current_inputs, current_scores=current_scores,
        expected_model_id=name, expected_artifact_sha256=artifact_hash,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze_cmd = sub.add_parser("freeze", help="Freeze approved DEVELOPMENT reference bins")
    freeze_cmd.add_argument("--manifest", type=Path, required=True)
    freeze_cmd.add_argument("--reference-inputs", type=Path, required=True)
    freeze_cmd.add_argument("--reference-scores", type=Path, required=True)
    freeze_cmd.add_argument("--policy", type=Path, default=ROOT / "legacy_models/validation_policy.json")
    freeze_cmd.add_argument("--output", type=Path, required=True)
    compare_cmd = sub.add_parser("evaluate", help="Evaluate a new scored monitoring window")
    compare_cmd.add_argument("--baseline", type=Path, required=True)
    compare_cmd.add_argument("--current-inputs", type=Path, required=True)
    compare_cmd.add_argument("--current-scores", type=Path, required=True)
    compare_cmd.add_argument("--manifest", type=Path, required=True)
    compare_cmd.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "freeze":
        policy = json.loads(args.policy.read_text(encoding="utf-8"))
        result = freeze(
            manifest_path=args.manifest,
            reference_inputs=args.reference_inputs,
            reference_scores=args.reference_scores,
            policy=policy_config(policy.get("stability")),
        )
    else:
        result = evaluate(
            baseline_path=args.baseline, current_inputs=args.current_inputs,
            current_scores=args.current_scores, manifest_path=args.manifest,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n",
                           encoding="utf-8")
    print(json.dumps({
        "status": "BASELINE_FROZEN" if args.command == "freeze" else result["status"],
        "model_id": result["model_id"],
        "baseline_sha256": result["baseline_sha256"],
        "band": result.get("band"),
        "revalidation_required": result.get("revalidation_required", False),
        "output": str(args.output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
