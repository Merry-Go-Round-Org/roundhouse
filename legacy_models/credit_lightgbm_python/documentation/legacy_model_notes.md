**EXECUTED FULL-DATA TRAINING on 2026-11-04.** The current `evaluation.json`, `manifest.json`, 100-row fixtures and `model.joblib` are real outputs verified on the uploaded original dataset. See `../TRAINING_REPORT.md` at repository root for actual results.

**EXECUTED FULL-DATA TRAINING on 2026-11-04.** The current `evaluation.json`, `manifest.json`, 100-row fixtures and `model.joblib` are real outputs verified on the uploaded original dataset. See `../../TRAINING_REPORT.md` in the shared legacy_models directory for actual results.

# credit_lightgbm_python — executed model notes

**Status**: fully trained on checksum-verified original data; fitted `model.joblib` and genuine development fixture exist.

## Purpose and population

UCI Taiwanese credit card client sample. Predict binary next-month default after September 2005 using April–September account/payment history; this is **not** a regulatory one-year PD.

## Provenance and terms

I-Cheng Yeh, UCI Machine Learning Repository DOI 10.24432/C55S3H, CC BY 4.0, https://archive.ics.uci.edu/dataset/350/default+of+credit+card+clients . This uploaded file matches the downloader SHA. The earlier audit used a differing XLS byte hash; the exact original version here is recorded in the manifest.

## Features, availability, exclusions

Included: LIMIT_BAL, EDUCATION, MARRIAGE, PAY_0, PAY_2, PAY_3, PAY_4, PAY_5, PAY_6, BILL_AMT1, BILL_AMT2, BILL_AMT3, BILL_AMT4, BILL_AMT5, BILL_AMT6, PAY_AMT1, PAY_AMT2, PAY_AMT3, PAY_AMT4, PAY_AMT5, PAY_AMT6. Excluded: ID (identifier), SEX and AGE (audit-only protected attributes), target; payment/bill history after scoring date would be leakage.

All features must exist at the prediction moment. Historical source documentation does not prove real-world availability or absence of information leakage in a live bank system. No missing values were reported by earlier independent full-data audit; production scorer rejects unexpected missingness.

## Algorithm, assumptions and split

Pre-fixed regularised gradient boosting with shallow trees and early complexity constraints; no test-led tuning. Train/validation/test split approximately 60/20/20. UCI split groups identical predictive profiles; it is *not* calendar-based backtesting. Seed 20261104. Use only training data for imputation and scaling. Threshold maximises F1 on validation only and is not a bank-set decision threshold. No separate calibration model or imbalanced data resampling is applied; evaluate calibration explicitly.

## Evaluation, uncertainty and fairness

Metrics are executed and saved in `evaluation.json` and `../../TRAINING_REPORT.md`, including ROC-AUC, average precision PR-AUC, log loss, Brier, Gini, confusion matrix, precision/recall/specificity/F1, validation-selected threshold and train-prevalence baseline; add bootstrap uncertainty and frozen validation calibration bins. Fraud adds alert capacity at 0.1% and recall at FPR <=0.1%. UCI SEX and AGE groups evaluated outside predictor set with sample sizes, outcome counts and Wilson uncertainty; unstable small groups marked, not a legal fairness test.

## Monitoring, limitations and failures

Monitor input schema, missingness, class prevalence and score drift (PSI heuristic bands only); once outcomes mature, revisit ranking/calibration and threshold/business costs. Check population shift, pipeline version drift, class mapping, data provenance, and retraining stability. No genuine UCI calendar backtest; ULB two-day data are not long-term operational evidence. Never reuse this demonstration for adverse credit decisions, regulatory capital, or fraud intervention without fit-for-purpose review.

## Commands

See parent README. Train using `python train.py` and score using `python score.py --input fixtures/development_inputs.csv --output /tmp/scores.csv`. All outputs must come from the actual dataset and saved artifact.
