# Model Risk Factory: usage guide

The Model Risk Factory takes legacy credit and fraud models through their lifecycle: it records them in an inventory, writes their documentation, rebuilds R models in Python, validates them and monitors them. AI agents do the repetitive work. People make every decision.

This is a prototype built on public data (the UCI credit default dataset and the ULB credit card fraud dataset). Nothing it produces is a production approval or a regulatory finding.

Parts marked *To be confirmed* are not settled yet. Do not guess them.

## Roles and signing in

Everyone signs in with a username and password. An administrator creates accounts and gives each one a role:

| Role | Typical work |
| --- | --- |
| Developer | Uploads datasets and models, starts document and rebuild runs |
| Validator | Reviews documents, rebuilds and validation reports |
| Approver | Makes the final decision on a model's use |

Who may decide at each review gate:

| Gate | Who may decide |
| --- | --- |
| 1 (model document) and 2 (rebuild) | A reviewer, validator, approver or administrator |
| 3 (validation report) | An approver or administrator (and the legacy reviewer role) |

Who may approve and execute an operation proposal is a separate rule: a document or rebuild proposal needs a developer, a validation proposal needs a validator, and an administrator may act on any of them. Approving a proposal is never a gate decision.

Two rules always apply:

- Nobody can review their own work: the reviewer of an item cannot be the person who started the run that produced it.
- Every action is recorded with the signed-in person's identity.

## The lifecycle at a glance

1. **Upload a dataset**, then **upload a model** that uses it.
2. The platform checks the upload and **creates the inventory record** automatically.
3. An agent **writes the model document**. A person reviews it (gate 1).
4. For an R model, an agent **rebuilds it in Python** and runs official parity on it. A person can rerun parity and reviews the rebuild (gate 2).
5. The platform **generates the validation report**. A person signs it off (gate 3).
6. The platform **monitors** the model and asks for re-validation when the data drifts.

A model's lifecycle stage moves on its own as these steps finish: Uploaded → Documented → Rebuilt (R models only) → Validated → Approved → Monitored. After a monitoring breach it becomes *Re-validation due*.

Every long step runs as a **run** with a status: queued, running, awaiting review, completed or failed. A step has only succeeded when its status is completed.

## 1. Datasets

Datasets belong to the platform. Models point to them, and the platform decides which part of a dataset each run may use.

A dataset upload has three files:

- the original public data file, unchanged;
- `dataset.json`, which describes the dataset: its ID, source, licence, version, checksum, target column, positive class and which columns are for audit only;
- a split file that puts every row in the train, validation or test partition.

The platform checks the checksum, the columns and the split, then stores the dataset as a fixed version. Uploading different content under the same ID creates a new version; it never overwrites the old one.

**Agents only ever get the development data.** The test partition is never given to an agent.

Upload a dataset on the **Datasets** page with **Upload dataset**. It needs a developer or administrator account.

Exact upload file format rules and upload error messages: *To be confirmed.*

## 2. Uploading a model

A model is uploaded as one zip file, the model bundle. It holds the model's manifest, input and output schema, scoring code, training code, saved model file, dependency list, small development fixtures with reference predictions, saved evaluation results and any old documentation.

Upload the dataset a model uses before the model itself.

Upload one model on the **Inventory** page with **Upload model**. It needs a developer or administrator account.

To bring in a whole legacy archive, open **Archive intake** and use **Upload archive**. The platform surveys the archive and writes a Gate 0 intake plan for you to review on that page. Datasets are still uploaded separately on **Datasets**.

The platform then runs these checks:

| Check | If it fails |
| --- | --- |
| The zip unpacks safely | Upload rejected |
| The manifest and schema follow the model contract | Upload rejected |
| File checksums match the manifest | Upload rejected |
| The model's dataset exists and has the model's input columns | Upload rejected |
| The name and version are new, or identical to an existing upload | Upload rejected |
| Reproduction: the model, run in an isolated sandbox on its development fixture, gives its saved reference predictions | Recorded and shown, and the model is flagged; the upload still succeeds |

A rejected upload creates nothing. Uploading the same model name with a new version adds a version to that model.

Model code only ever runs inside an isolated sandbox with no network access.

**The Cox survival model** is a synthetic demonstration. It is uploaded and documented like the others and labelled *synthetic demo*. Scoring, rebuild, validation and monitoring do not apply to it and show *Not applicable*.

## 3. The inventory

Nobody types inventory records. The platform builds each record from the uploaded manifest and keeps it up to date as the model moves through its lifecycle.

| Part of the record | Comes from |
| --- | --- |
| Name, version, language, model type | The manifest |
| Purpose, intended use, target and population | The manifest and the dataset description |
| Owner | The manifest |
| Risk tier and its rationale | The manifest |
| Data, features, preprocessing and split | The manifest |
| Limitations | The manifest |
| Documents and reports | Linked as they are produced |
| Validation dates, status and next due date | The validation step |
| Open findings | The validation report |
| Approval | Review decisions |
| Monitoring status | The monitoring step |

A field with no source shows **Not provided in upload**. The platform never fills it with a guess. The model's Overview page shows where each field came from.

## 4. Model documents

Every uploaded model gets a document written by an agent. The run starts automatically after the upload. It can also be started again with *Regenerate document*.

- The agent reads the model's code and files in an isolated sandbox and fills a fixed template with nine sections: model overview; purpose and use; data and preprocessing; methodology; implementation and reproducibility; development testing; limitations and mitigants; monitoring and changes; evidence and review.
- Old documentation in the bundle is treated as possibly out of date. The agent checks it against the code and evidence. A claim it cannot check is marked *(old documentation, not verified)*. Where the old documentation is wrong, the document says so in a conflicts table.
- Every fact cites the file it came from. Unknown facts are written as *Unknown* with a reason, and listed in a missing-information table.
- The document is Markdown. Each new draft is a new version.

Each draft goes to **gate 1**. If the reviewer asks for changes, their comments start a new draft.

## 5. Rebuild and parity

Only R models are rebuilt. The agent rewrites the model in Python.

1. A person starts the rebuild on the model's Rebuild & parity page.
2. In an isolated sandbox, with development data only, the agent writes the Python version. It may compare its work with the legacy model while it debugs. That is an *agent self-check*, not a parity result.
3. The agent exports a candidate. It becomes a new Python version of the model in the inventory, linked to the R model it came from.
4. The agent runs **parity** on the candidate. With each parity run, the platform also checks the candidate's **input contract**: it must score clean development rows, and reject input with a missing row ID, a repeated row ID or an extra target column. If parity or an input check fails, the agent fixes the candidate, exports it again and reruns parity.
5. The rebuild goes to **gate 2**. The reviewer can rerun parity with the **Run parity** button. If the reviewer asks for changes, the agent continues the rebuild with their comments. Gate 2 also shows the input checks saved with the agent's last parity run; a person's parity rerun does not repeat them. Accepting gate 1 or gate 2 completes the run: the agent has nothing left to do.

Parity is one platform check. The agent and a person run the same check, and both runs are recorded, labelled by who started them. Parity scores the candidate alone, in a fresh container, on the development data. It compares each row's probability with the legacy model's reference predictions. It passes when every absolute difference is at most 1e-8. The same candidate always gives the same result.

Gate 2 can be accepted only when the exact candidate under review has a passing parity run. Requesting changes never needs parity.

## 6. Validation reports

Validation reports are produced by fixed code, not by an AI agent, so the same inputs always give the same numbers.

A report covers:

- **Performance:** how well the model separates good and bad outcomes (AUC, Gini, KS), with uncertainty.
- **Calibration:** whether predicted rates match observed rates.
- **Stability:** whether the data has shifted, measured by PSI on the score and on each feature. Below 0.10 is stable, 0.10 to 0.25 needs attention, above 0.25 is a significant shift. These bands are industry convention, not regulation.
- **Fairness:** outcome and error rates across groups. The credit dataset has sex and age for this. The fraud dataset has no such attributes, so fairness is *not assessable* for fraud models.
- **Parity**, for a rebuilt model: the recorded parity result for that exact candidate.
- **Findings**, each with a severity, owner and status.

Each check has one of five statuses:

| Status | Meaning |
| --- | --- |
| PASS | The check passed |
| FAIL | The check failed |
| REVIEW | A person must assess it |
| NOT_ASSESSABLE | The evidence is missing or the check could not run. This is never a pass |
| NOT_APPLICABLE | The check does not apply to this model |

Each report goes to **gate 3** for sign-off. A technical PASS is not an approval.

## 7. Reviews

All three gates work the same way. A reviewer decides on a review card: in the assistant panel, **Open review cards** shows the selected model's pending reviews, and the **Review queue** page lists all of them. Who may decide at each gate is in *Roles and signing in*.

| Gate | What is reviewed | If changes are requested |
| --- | --- | --- |
| 1 | A model document draft | A new draft is written with the comments |
| 2 | A rebuild candidate and its parity result | The rebuild continues with the comments |
| 3 | A validation report | Its findings stay open and the model is not approved |

A decision is one of: accepted, changes requested, or accepted with conditions. A written rationale is always required. The decision updates the inventory record. The review card shows which decisions are available.

At gate 3, the report's checks decide what is allowed:

- A **failed** check blocks acceptance in any form. The report can only be sent back with changes requested.
- A required check that is **REVIEW** or **NOT_ASSESSABLE** is a **limitation**: the evidence needs judgement or cannot exist, as with a legacy model whose original training rows are unknown. A limitation blocks plain acceptance. An approver may accept the report **with conditions**, acknowledging each limitation in writing. The checks keep their status in the report.

Changes requested closes the reviewed run as a revision request, not as a technical failure, and a follow-up run continues the work.

A rebuild or validation run needs an accepted Gate 1 document for that exact model version: the model's current document, which is its latest document run. An older document draft that is still waiting for review is superseded and blocks nothing. A rebuilt candidate is a new model version, so it needs its own accepted Gate 1 document before it can be validated.

Only the signed-in reviewer can make a decision. The assistant can prepare one, but the reviewer must confirm it on a card. Nothing typed in chat counts as approval.

## 8. Monitoring

Monitoring checks whether new data still looks like the data the model was built on.

- **Upload a batch:** a CSV file with the model's input columns, on the **Monitoring** page.
- **Simulate drift:** the platform shifts the test data in a declared way. Every result from it is labelled *simulated*. A simulation names one of the model's input features, exactly as spelled in its feature list (case matters), a factor greater than 0 and at most 100 (0 is not allowed), and a window label. Dataset columns the model does not use, such as the audit-only sex and age columns of the credit dataset, and the target column, are not input features.

The platform scores the batch in a sandbox and calculates PSI with the same bands as the validation report. A red result marks the model *Re-validation due* and raises an alert.

## 9. The assistant

The assistant panel is on every page. A conversation is tied to the model you opened it from; that is the *selected* model. The assistant acts as you, with exactly your access.

What it can do:

- **Explain the platform**, from this guide.
- **Read records:** a model's lifecycle stage, full version, latest runs with their status and errors, pending review gates, proposals, reports with each check's status, the latest official parity and the upload reproduction. It reads the selected model by default, and can find other models by name.
- **Request an operation** (document, rebuild or validate) for the selected model. This records a **proposal** and shows its card in the chat. Nothing starts yet.
- **Request a monitoring simulation** for the selected model. It shows a confirmation card; confirming it starts the simulation.

How a proposal works, all inside the chat:

1. The assistant records the proposal. Its card names the role that may act on it.
2. A person with that role approves or rejects it on the card. Approving does not start anything.
3. The approved proposal is executed from the same card, as a separate step. Only then does a run start.
4. The card then shows the run's status: running, waiting for review, completed or failed with its reason. A failed run is never retried automatically; ask the assistant for a new proposal to try again.

Typing "yes" in the chat approves nothing. If the assistant is unsure whether a request was recorded, it checks the existing proposals instead of sending the request again.

What it cannot do: upload datasets, models, archives or monitoring batches, decide Gate 0 intake, make review gate decisions from typed text, manage accounts or edit findings. Use **Datasets → Upload dataset**, **Inventory → Upload model**, **Archive intake → Upload archive**, the **Monitoring** page for CSV batches, **Open review cards** in the assistant panel (or the **Review queue** page) for gate decisions, and **Accounts** (administrators only).

The assistant cannot see test data.
