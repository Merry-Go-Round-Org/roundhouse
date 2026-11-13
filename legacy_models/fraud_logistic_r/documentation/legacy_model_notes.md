# fraud_logistic_r — research model notes

Status: BLOCKED / UNEXECUTED. Source code is provided, but `model.rds` and verified reference predictions are absent pending the raw dataset and dependencies. Never present this package as a fitted model.

## Purpose and intended use
Anonymised ULB/Worldline card-transaction fraud classification over a two-day observation period. This is not default credit risk.
Target is Class, positive class 1. Population is the original dataset, not all Irish borrowers or transactions.

## Provenance and lineage
Source: https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud. Provider ULB Machine Learning Group and Worldline; published ODbL/DbCL terms. No raw redistribution. Expected SHA-256: 76274b691b16a6c49d3f159c883398e03ccd6d1ee12d9d8ee38f4b4b98551a89. Row and class counts from separate source audit: 284,807 and 492; missingness 0. Verify again on training host.

## Features, scoring-time availability and exclusions
Inputs: V1, V2, V3, V4, V5, V6, V7, V8, V9, V10, V11, V12, V13, V14, V15, V16, V17, V18, V19, V20, V21, V22, V23, V24, V25, V26, V27, V28, Amount. Time is used for split ordering only; V1–V28 are anonymised PCA transformations with unknown original meanings and original PCA fitting history. Amount is a monetary quantity, not a card identifier. No demographic or transaction network labels are inferable.

## Model, split and evaluation policy
R binomial GLM with train-only scaling, unregularised logit, validated convergence. Export fitted coefficients for separate scoring-equivalent Python replay. Seed 20261104. Order Time into contiguous train/validation/test (~60/20/20), keep tied times within a split. No account grouping possible. Use validation exclusively for threshold selection; do not tune using final test.

Report average precision (non-interpolated sklearn), ROC-AUC, Gini, log loss, Brier, observed-versus-predicted calibration, precision, recall, specificity, F1, confusion matrix, baseline and uncertainty where defensible. Primary evaluation is average precision, with 0.1% alert-budget precision and recall at FPR <=0.1%. Protected-demographic fairness is not assessable.

No executed model metrics, artifact hash, R/Python parity or full-data model output currently exists. Score and artifact reload must be verified after training. Monitor PSI and input-schema changes, event prevalence, discrimination/calibration and group metrics after outcomes mature; PSI bands are heuristics, not regulations.

## Run
From repository root run frozen split preparation, then `Rscript legacy_models/fraud_logistic_r/train.R --domain ulb --package legacy_models/fraud_logistic_r`. Score from package directory: `Rscript score.R --input fixtures/development_inputs.csv --output /tmp/predictions.csv`. Scoring will error until a genuine trained artifact exists.
