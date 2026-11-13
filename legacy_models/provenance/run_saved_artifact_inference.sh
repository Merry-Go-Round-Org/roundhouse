#!/usr/bin/env bash
# Score the SAME SHA-verified, saved XGBoost model on identical development
# fixtures in macOS and Linux/amd64. This does not refit any model.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Run on the macOS host to collect the macOS inference results." >&2
  exit 2
fi
command -v docker >/dev/null || { echo "Docker is required." >&2; exit 2; }
image="exl-legacy-provenance:linux-amd64"
output="datasets/reports/provenance/inference_outputs"
mkdir -p "$output"
# Always rebuild to use the pinned dependency/runtime image from this checkout.
docker build --platform linux/amd64 -t "$image" \
  -f legacy_models/provenance/Dockerfile legacy_models/provenance

for name in credit_xgboost_python fraud_xgboost_python; do
  model="legacy_models/$name"
  input="$model/fixtures/development_inputs.csv"
  if [[ ! -f "$model/model.joblib" || ! -f "$input" ]]; then
    echo "Missing saved model or fixture for $name: run git lfs pull" >&2
    exit 2
  fi
  # No retraining; existing scorers verify the artifact hash.
  python "$model/score.py" --input "$input" --output "$output/${name}_macos.csv"
  docker run --rm --platform linux/amd64 --network none --read-only \
    --cpus 2 --memory 4g --pids-limit 256 \
    --tmpfs /tmp:rw,nosuid,nodev,size=256m \
    --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=$root,dst=/workspace,readonly" \
    --mount "type=bind,src=$root/$output,dst=/output" \
    --entrypoint python "$image" \
    "$model/score.py" --input "$input" --output "/output/${name}_linux_amd64.csv"
done
# Always record the exact 1e-10 numeric result. By default this is a
# governance assessment: a genuine numeric mismatch is REVIEW, not a broken
# pipeline. Set EXL_INFERENCE_STRICT=1 to enforce exact parity as a hard gate.
# Avoid expanding an empty Bash array: macOS includes Bash 3.2 with
# stricter nounset handling for some array expansions.
if [[ "${EXL_INFERENCE_STRICT:-0}" == "1" ]]; then
  python legacy_models/provenance/compare_saved_artifact_inference.py \
    --models-dir legacy_models --predictions-dir "$output" \
    --out datasets/reports/provenance/inference_portability.json --strict
else
  python legacy_models/provenance/compare_saved_artifact_inference.py \
    --models-dir legacy_models --predictions-dir "$output" \
    --out datasets/reports/provenance/inference_portability.json
fi
python - <<'PY'
import json
from pathlib import Path
path = Path('datasets/reports/provenance/inference_portability.json')
data = json.loads(path.read_text())
for name, item in data['models'].items():
    if item['status'] != 'PASS':
        print(f"GOVERNANCE REVIEW: {name} has numeric parity {item['status']}, "
              f"max difference {item['max_absolute_macos_vs_linux']:.17g}, "
              f"float32 ULP distance {item['max_float32_ulp_distance_macos_vs_linux']}.")
print("Saved-artifact inference report written; numeric FAIL values have not been changed.")
PY
echo "Re-training reproducibility remains a separate governance finding."
