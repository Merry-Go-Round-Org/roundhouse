# fraud_xgboost_python

**Status: TRAINED AND VERIFIED** — full-data Python execution on 2026-11-04.

See [model notes](documentation/legacy_model_notes.md), [evaluation](evaluation.json), [manifest](manifest.json), [schema](schema.json), and [release report](../TRAINING_REPORT.md).

Score without retraining: `python score.py --input fixtures/development_inputs.csv --output /tmp/scores.csv`.

The saved model artifact is executable Python serialization; verify its SHA-256 and load only in a trusted isolated process. No original full dataset or private final-test labels are distributed here.
