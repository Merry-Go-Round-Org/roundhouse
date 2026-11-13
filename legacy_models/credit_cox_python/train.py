"""Train a right-censored Cox proportional hazards research DEMO.

Input must contain row_id, duration (>0), event (1=event, 0=censored),
and selected covariates. Do NOT pass UCI default binary labels or ULB
transaction Time as survival outcomes; neither contains a valid duration.
"""
import argparse
import hashlib
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from lifelines import CoxPHFitter
from lifelines.utils import concordance_index
from sklearn.model_selection import train_test_split

SEED = 20261104

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def validated_input(path, features):
    df = pd.read_csv(path, dtype={"row_id": "string"})
    required = ["row_id", "duration", "event", *features]
    if len(features) != len(set(features)) or not features or any(f in ("row_id", "duration", "event") for f in features):
        raise ValueError("Specify distinct covariate names, excluding row_id/duration/event")
    if df.columns.duplicated().any() or not set(required).issubset(df.columns):
        raise ValueError(f"Input needs columns {required}; UCI/ULB are binary classification datasets, NOT survival datasets")
    if df.empty or df.row_id.isna().any() or df.row_id.duplicated().any() or df.row_id.str.strip().eq("").any():
        raise ValueError("row_id must be nonempty, unique, and non-null")
    for column in required[1:]:
        df[column] = pd.to_numeric(df[column], errors="raise")
    if not np.isfinite(df[required[1:]].to_numpy(dtype=float)).all():
        raise ValueError("Missing or nonfinite duration/event/features")
    if (df.duration <= 0).any() or not set(df.event.unique()).issubset({0, 1}):
        raise ValueError("duration must be positive; event must be 0/1")
    if df.event.sum() < 20 or (df.event == 0).sum() < 20:
        raise ValueError("Need at least 20 events and 20 censored records for this demo")
    return df

def cindex(model, rows, features):
    risks = model.predict_partial_hazard(rows[features]).to_numpy().reshape(-1)
    return float(concordance_index(rows.duration, -risks, rows.event))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="A real time-to-event dataset, or explicitly synthetic demo")
    ap.add_argument("--package", default="legacy_models/credit_cox_python")
    ap.add_argument("--features", default="utilisation,income_to_debt,delinquencies")
    ap.add_argument("--data-kind", choices=["synthetic", "real"], required=True)
    ap.add_argument("--horizon", type=float, default=12.0, help="Same time unit as input duration, e.g. months")
    ap.add_argument("--time-unit", default="months")
    args = ap.parse_args()
    features = [x.strip() for x in args.features.split(",") if x.strip()]
    if not np.isfinite(args.horizon) or args.horizon <= 0:
        raise ValueError("horizon must be positive and finite")
    data = validated_input(args.data, features)
    if args.data_kind == "real":
        raise ValueError("Real-data mode intentionally gated pending documented origin, event, censoring, and observation-window review")
    tr, remain = train_test_split(np.arange(len(data)), test_size=0.4, stratify=data.event, random_state=SEED)
    val, test = train_test_split(remain, test_size=0.5, stratify=data.event.iloc[remain], random_state=SEED + 1)
    if any(data.event.iloc[ix].sum() < 5 for ix in (tr, val, test)):
        raise ValueError("Not enough events in every split")
    model = CoxPHFitter(penalizer=0.05)
    model.fit(data.iloc[tr][["duration", "event", *features]], duration_col="duration", event_col="event", show_progress=False)
    package = Path(args.package)
    package.mkdir(parents=True, exist_ok=True)
    artifact = package / "model.joblib"
    joblib.dump(model, artifact)
    restored = joblib.load(artifact)
    if not np.allclose(model.predict_partial_hazard(data.iloc[val][features]),
                       restored.predict_partial_hazard(data.iloc[val][features]), atol=1e-12, rtol=0):
        raise RuntimeError("Reload parity failure")
    metrics = {
        "status": "synthetic_demo_only",
        "model_family": "cox_proportional_hazards",
        "method": "penalized_partial_likelihood_breslow_baseline",
        "data_kind": "synthetic",
        "dataset_sha256": sha256(args.data),
        "seed": SEED,
        "time_unit": args.time_unit,
        "horizon": args.horizon,
        "event_definition": "synthetically generated event; 0 is right-censored",
        "split": {"train": len(tr), "validation": len(val), "test": len(test)},
        "events": {"train": int(data.event.iloc[tr].sum()), "validation": int(data.event.iloc[val].sum()), "test": int(data.event.iloc[test].sum())},
        "c_index": {"train": cindex(restored, data.iloc[tr], features),
                    "validation": cindex(restored, data.iloc[val], features),
                    "test": cindex(restored, data.iloc[test], features)},
        "caveats": [
            "Synthetic demo only, not evidence of real credit-default performance",
            "C-index assesses ordering, not probability calibration",
            "Proportional hazards and censoring assumptions not yet tested",
            "Random stratified split is a demo convenience, not out-of-time validation",
            "Horizon event risk is NOT an approved 12-month regulatory PD"
        ],
    }
    manifest = {
        "model_id": "credit_cox_python",
        "model_version": "0.1.0-demo",
        "domain": "credit",
        "algorithm": "cox_ph",
        "status": "synthetic_demo_only",
        "positive_class": 1,
        "prediction_type": "cumulative_event_probability_at_horizon",
        "artifact_paths": ["model.joblib"],
        "artifact_sha256": sha256(artifact),
        "feature_order": features,
        "data_contract": {"id": "row_id", "duration": "duration", "event": "event", "time_unit": args.time_unit},
        "horizon": args.horizon,
        "metrics_reference": "evaluation.json",
        "integration": "separate survival-model interface; not binary positive_probability",
    }
    (package / "evaluation.json").write_text(json.dumps(metrics, indent=2) + "\n")
    (package / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"model": manifest["model_id"], "status": manifest["status"], "c_index": metrics["c_index"], "artifact_sha256": manifest["artifact_sha256"]}, indent=2))

if __name__ == "__main__":
    main()
