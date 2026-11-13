"""Score trusted Cox model at its trained time horizon (no target needed)."""
import argparse
import hashlib
import json
from pathlib import Path
import joblib
import numpy as np
import pandas as pd

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", default="legacy_models/credit_cox_python")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    a = ap.parse_args()
    package = Path(a.package)
    manifest = json.loads((package / "manifest.json").read_text())
    if manifest["status"] != "synthetic_demo_only":
        raise ValueError("Unexpected or unapproved model status")
    artifact = package / "model.joblib"
    if sha256(artifact) != manifest["artifact_sha256"]:
        raise ValueError("Artifact checksum mismatch")
    data = pd.read_csv(a.input, dtype={"row_id": "string"})
    features = manifest["feature_order"]
    if data.empty or data.columns.duplicated().any() or not set(["row_id", *features]).issubset(data.columns):
        raise ValueError("Need row_id and all trained features")
    if data.row_id.isna().any() or data.row_id.duplicated().any() or data.row_id.str.strip().eq("").any():
        raise ValueError("row_id cannot be blank or duplicated")
    if "duration" in data or "event" in data:
        raise ValueError("Scoring input must not expose event or duration labels")
    X = data[features].apply(pd.to_numeric, errors="raise")
    if not np.isfinite(X.to_numpy(dtype=float)).all():
        raise ValueError("Invalid feature value")
    # joblib uses pickle; load only locally created, reviewed trusted artifacts.
    model = joblib.load(artifact)
    horizon = float(manifest["horizon"])
    survival = model.predict_survival_function(X, times=[horizon]).iloc[0].to_numpy(dtype=float)
    if len(survival) != len(X) or not np.isfinite(survival).all() or ((survival < 0) | (survival > 1)).any():
        raise ValueError("Invalid survival estimates")
    result = pd.DataFrame({
        "row_id": data.row_id,
        "horizon": horizon,
        "time_unit": manifest["data_contract"]["time_unit"],
        "survival_probability": survival,
        "cumulative_event_probability": 1.0 - survival,
    })
    output = Path(a.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output, index=False, float_format="%.17g")
    print(f"SYNTHETIC MODEL SCORED: {len(result)} rows -> {output}")

if __name__ == "__main__":
    main()
