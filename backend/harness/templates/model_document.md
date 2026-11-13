# Model document: MODEL_NAME

| Field | Value |
| --- | --- |
| Model name | |
| Model version | |
| Language | |
| Domain and task | |
| Package files read | |
| Old documentation read | |
| Status | Agent draft, pending human review |

## 1. Model overview

> Guide: Model name, version, owner, developer, implementation language, algorithm and lifecycle status. One short paragraph a reviewer can read first.

## 2. Purpose and use

> Guide: The business problem, the prediction target and its positive class, intended users, the population the model applies to, and its operating boundaries: where it must not be used.

## 3. Data and preprocessing

> Guide: The exact source file, version and checksum; the input variables in scoring order; the target and positive label; missing-value handling; transformations; and the train, validation and test split with its sizes. Say which columns are excluded and why, including audit-only attributes. Describe loading and preprocessing precisely enough for a rebuild in another language to match it.

## 4. Methodology

> Guide: The algorithm and why it fits the purpose, its parameters and settings, the random seed, the assumptions it makes, and alternatives considered if any are recorded.

## 5. Implementation and reproducibility

> Guide: The scoring and training entry points and how to run them, the dependencies and their versions, the environment, and the artifact file with its checksum. Give the commands a third party would run to reproduce the predictions.

## 6. Development testing

> Guide: The tests and evaluations on record, the sample each used, and the results with their metrics, benchmarks and uncertainty. Report only numbers found in package files or produced by running code in the sandbox, and say which.

## 7. Limitations and mitigants

> Guide: Known weaknesses, risks the model does not capture, data limitations, restrictions on use, adjustments, and proposed safeguards.

## 8. Monitoring and changes

> Guide: The measures to monitor, such as score and feature PSI, calibration and group metrics; review triggers; who is responsible; and the version and change history on record.

## 9. Evidence and review

> Guide: List the evidence files read, with what each supports. Fill the two tables below. Write "None found." under a table that has no rows.

### Conflicts with the old documentation

| Old documentation says | Evidence shows | Source |
| --- | --- | --- |

### Missing information

| Item | Reason |
| --- | --- |
