# Cox proportional hazards — separate synthetic survival demo

**Status: prototype source; no training execution or real-world performance is claimed by this repository commit.**

This package extends the EXL legacy-model workstream with a Cox proportional-hazards (PH) survival estimator. It does **not** replace the existing UCI/ULB binary classification models and should not be treated as a performance improvement over them: survival and binary classifiers predict different quantities.

## Why not directly fit UCI/ULB?

- **UCI credit:** `default payment next month` is a binary outcome, not the observed elapsed time to default with right-censoring information. The six historical bill/payment columns are *not* verified default-event times.
- **ULB fraud:** `Time` is relative transaction time; `Class` identifies whether an individual transaction was fraudulent. There is no per-account onset, exposure start or right-censoring information.
- Creating duration from either dataset would fabricate the event-time target and create misleading results.

## Input contract

One row per independent loan or subject with:

| Column | Meaning |
| --- | --- |
| `row_id` | Unique nonempty subject identifier |
| `duration` | Strictly positive elapsed follow-up time from a defined common origin |
| `event` | 1=observed event, 0=right-censored |
| covariates | Numeric values measured before (or at) origin; no post-outcome leakage |

Choose and document the event definition, origin, competing events, exclusion rules, right-censoring assumptions, and duration unit. For real lending research, appropriate data would track loan origination to default or censoring, with a verifiable observation cutoff and train/test time split. Real mode is intentionally blocked pending governance review.

## Run synthetic demonstration

Use Python 3.11+ inside your project environment:

```bash
python -m pip install -r legacy_models/credit_cox_python/requirements.txt
python legacy_models/credit_cox_python/generate_demo.py --output datasets/synthetic/cox_loans.csv
python legacy_models/credit_cox_python/train.py --data datasets/synthetic/cox_loans.csv --data-kind synthetic --package legacy_models/credit_cox_python --horizon 12 --time-unit months
python -c "import pandas as pd; d=pd.read_csv('datasets/synthetic/cox_loans.csv'); d[['row_id','utilisation','income_to_debt','delinquencies']].head(10).to_csv('datasets/synthetic/cox_scoring_inputs.csv',index=False)"
python legacy_models/credit_cox_python/score.py --input datasets/synthetic/cox_scoring_inputs.csv --output datasets/synthetic/cox_scores.csv
```

The scorer's output is `row_id,horizon,time_unit,survival_probability,cumulative_event_probability`, **not** the binary-model `positive_probability` contract. A backend must explicitly support this different schema; no existing API compatibility is claimed. Save the generated model with Git LFS if committing it, but avoid committing the synthetic CSV or its scored rows.

## Validation limitations

The output reports Harrell's C-index on random development partitions, event counts and a SHA-256 artifact hash. This demonstration is **not** a statistically validated credit-risk model. Before any empirical claim, add proportional-hazards diagnostics (e.g. Schoenfeld residual tests), censoring/competing-risk review, horizon calibration with censoring-adjusted methods, time-dependent discrimination, interval estimates, temporal/external validation, and governance sign-off. Models with PH violations or materially nonproportional hazards may need stratification or time-varying effects. No real regulatory one-year PD is established.

## Isolation

The package is deliberately independent of `legacy_models/shared/engine.py` and its frozen binary-classification splits. Do not register it as a production binary model or compare its C-index directly with UCI/ULB ROC-AUC.
