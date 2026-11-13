**EXECUTED FULL-DATA TRAINING on 2026-11-04.** The current `evaluation.json`, `manifest.json`, 100-row fixtures and `model.joblib` are real outputs verified on the uploaded original dataset. See `../TRAINING_REPORT.md` at repository root for actual results.

**EXECUTED FULL-DATA TRAINING on 2026-11-04.** The current `evaluation.json`, `manifest.json`, 100-row fixtures and `model.joblib` are real outputs verified on the uploaded original dataset. See `../../TRAINING_REPORT.md` in the shared legacy_models directory for actual results.

# fraud_xgboost_python — executed model notes

**Status**: fully trained on checksum-verified original data; fitted `model.joblib` and genuine development fixture exist.

## Purpose and population

ULB/Worldline anonymised European card transactions during two days in September 2013; detect the binary published `Class=1` fraud label. This is transaction risk, not borrower default.

## Provenance and terms

ULB Machine Learning Group/Worldline; https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud . Open Database License/database contents licence; attribution and any share-alike on public adapted database are preserved by not redistributing raw data.

## Features, availability, exclusions

Included: V1, V2, V3, V4, V5, V6, V7, V8, V9, V10, V11, V12, V13, V14, V15, V16, V17, V18, V19, V20, V21, V22, V23, V24, V25, V26, V27, V28, Amount. Excluded: Class (outcome), Time (split ordering only). PCA V1–V28 are opaque; Amount is original monetary amount. Do not infer real card/account IDs or demographics..

All features must exist at the prediction moment. Historical source documentation does not prove real-world availability or absence of information leakage in a live bank system. No missing values were reported by earlier independent full-data audit; production scorer rejects unexpected missingness.

## Algorithm, assumptions and split

Pre-fixed regularised gradient boosting with shallow trees and early complexity constraints; no test-led tuning. Train/validation/test split approximately 60/20/20. ULB split keeps relative-Time ties together in ordered contiguous windows; no calendar dates or account-based grouping can be established. Seed 20261104. Use only training data for imputation and scaling. Threshold maximises F1 on validation only and is not a bank-set decision threshold. No separate calibration model or imbalanced data resampling is applied; evaluate calibration explicitly.

## Evaluation, uncertainty and fairness

Metrics are executed and saved in `evaluation.json` and `../../TRAINING_REPORT.md`, including ROC-AUC, average precision PR-AUC, log loss, Brier, Gini, confusion matrix, precision/recall/specificity/F1, validation-selected threshold and train-prevalence baseline; add bootstrap uncertainty and frozen validation calibration bins. Fraud adds alert capacity at 0.1% and recall at FPR <=0.1%. Demographic fairness cannot be assessed on ULB: protected group labels are not published.

## Monitoring, limitations and failures

Monitor input schema, missingness, class prevalence and score drift (PSI heuristic bands only); once outcomes mature, revisit ranking/calibration and threshold/business costs. Check population shift, pipeline version drift, class mapping, data provenance, and retraining stability. No genuine UCI calendar backtest; ULB two-day data are not long-term operational evidence. Never reuse this demonstration for adverse credit decisions, regulatory capital, or fraud intervention without fit-for-purpose review.

## Commands

See parent README. Train using `python train.py` and score using `python score.py --input fixtures/development_inputs.csv --output /tmp/scores.csv`. All outputs must come from the actual dataset and saved artifact.
