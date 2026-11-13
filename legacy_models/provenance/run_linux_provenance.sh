#!/usr/bin/env bash
# Full split provenance using complementary macOS + Linux/amd64 replays.
# Neither single-platform replay is expected to reproduce both XGBoost models.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"

command -v docker >/dev/null || { echo 'Docker is required.' >&2; exit 2; }
for required in 'datasets/uci/default of credit card clients.xls' 'datasets/ulb/creditcard.csv' \
                'legacy_models/credit_logistic_r/model.rds' 'legacy_models/fraud_xgboost_python/model.joblib' \
                'legacy_models/evidence/uci_split_ids.csv'; do
    if [[ ! -f "$required" ]]; then
        echo "Missing $required; run git lfs pull and python download_datasets.py uci ulb" >&2
        exit 2
    fi
done

# Re-create the independent macOS report first, with the updated environment
# fingerprint, and no incorrect assumption that all eight must reproduce here.
report_dir='datasets/reports/provenance'
mkdir -p "$report_dir"
if [[ ! -f "$report_dir/split_provenance_macos.json" ]]; then
    echo "Missing $report_dir/split_provenance_macos.json" >&2
    echo "First run the verifier on the Mac with --replay-python (without --strict)" >&2
    exit 2
fi

image='exl-legacy-provenance:linux-amd64'
docker build --platform linux/amd64 -t "$image" \
    -f legacy_models/provenance/Dockerfile legacy_models/provenance

# Allow *individual* refit mismatches but require the verifier to finish,
# write the complete Linux report, and subsequently pass strict merging.
docker run --rm --platform linux/amd64 --network none --read-only \
    --cpus 2 --memory 6g --pids-limit 256 \
    --tmpfs /tmp:rw,nosuid,nodev,size=1g \
    --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=$root,dst=/workspace,readonly" \
    --mount "type=bind,src=$root/$report_dir,dst=/output" \
    "$image" \
    --models-dir legacy_models --data-root datasets \
    --out /output/split_provenance_linux.json --replay-python

# Fail closed unless every model reproduced in at least one distinct, verified
# environment. Never replace/alter either input audit.
python legacy_models/shared/merge_split_provenance.py \
    --mac-report "$report_dir/split_provenance_macos.json" \
    --linux-report "$report_dir/split_provenance_linux.json" \
    --models-dir legacy_models --data-root datasets \
    --out "$report_dir/split_provenance.json" --strict

python standardize_exl_models.py --models-dir legacy_models \
    --out datasets/reports/standardized --strict
printf '\nCross-platform provenance report verified; individual OS divergences remain documented.\n'
