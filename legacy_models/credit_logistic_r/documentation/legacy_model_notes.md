# credit_logistic_r — research model notes

Status: BLOCKED / UNEXECUTED. Source code is provided, but `model.rds` and verified reference predictions are absent pending the raw dataset and dependencies. Never present this package as a fitted model.

## Purpose and intended use
Historical Taiwanese credit card default-next-month classification after September 2005. This is not 12-month regulatory PD or an Irish production population.
Target is default payment next month, positive class 1. Population is the original dataset, not all Irish borrowers or transactions.

## Provenance and lineage
Source: https://archive.ics.uci.edu/dataset/350/default+of+credit+card+clients. Provider I-Cheng Yeh, UCI, CC BY 4.0; raw file version/hash discrepancy with prior dataset audit requires investigation. Expected SHA-256: 30c6be3abd8dcfd3e6096c828bad8c2f011238620f5369220bd60cfc82700933. Row and class counts from separate source audit: 30,000 and 6,636; missingness 0. Verify again on training host.

## Features, scoring-time availability and exclusions
Inputs: LIMIT_BAL, EDUCATION, MARRIAGE, PAY_0, PAY_2, PAY_3, PAY_4, PAY_5, PAY_6, BILL_AMT1, BILL_AMT2, BILL_AMT3, BILL_AMT4, BILL_AMT5, BILL_AMT6, PAY_AMT1, PAY_AMT2, PAY_AMT3, PAY_AMT4, PAY_AMT5, PAY_AMT6. SEX and AGE are held out for fairness audit; ID and outcome never enter predictors. Payment histories are only available for scoring after the relevant September snapshot; before then they could leak future information. EDUCATION and MARRIAGE retain raw codes.

## Model, split and evaluation policy
R binomial GLM with train-only scaling, unregularised logit, validated convergence. Export fitted coefficients for separate scoring-equivalent Python replay. Seed 20261104. Group identical predictive profiles into nonoverlapping train/validation/test (~60/20/20). Not calendar backtesting. Use validation exclusively for threshold selection; do not tune using final test.

Report average precision (non-interpolated sklearn), ROC-AUC, Gini, log loss, Brier, observed-versus-predicted calibration, precision, recall, specificity, F1, confusion matrix, baseline and uncertainty where defensible. UCI SEX and AGE require group outcome counts and uncertainty. Do not infer legal clearance from excluding predictor fields or from group rates.

No executed model metrics, artifact hash, R/Python parity or full-data model output currently exists. Score and artifact reload must be verified after training. Monitor PSI and input-schema changes, event prevalence, discrimination/calibration and group metrics after outcomes mature; PSI bands are heuristics, not regulations.

## Run
From repository root run frozen split preparation, then `Rscript legacy_models/credit_logistic_r/train.R --domain uci --package legacy_models/credit_logistic_r`. Score from package directory: `Rscript score.R --input fixtures/development_inputs.csv --output /tmp/predictions.csv`. Scoring will error until a genuine trained artifact exists.
