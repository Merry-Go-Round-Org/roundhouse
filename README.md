# Roundhouse

**Agentic model risk management, grounded in deterministic evidence and human approval.**

Roundhouse is a research-oriented **Agentic Model Risk Factory** created for **EXL × UCD Data & AI Hackathon 2026 — Challenge 04**. It provides an integrated workspace for importing legacy machine-learning models, understanding undocumented code, producing model documentation, rebuilding R models in Python, checking reproducibility and parity, validating outputs, and monitoring drift. An integrated AI assistant helps analyze and navigate the workflow, but does not approve models or alter official metrics.

> **Our principle:** Agents investigate and propose. Trusted services calculate. Authorized humans decide.

[Read the architecture diagram and technical explanation](ARCHITECTURE.md).

> **Publication status — 13 November 2026:** The public `Merry-Go-Round-Org/roundhouse` repository was empty when this documentation was prepared. These two Markdown files are a release-documentation deliverable created from a code/documentation audit of the existing private workspace at source commit `6f0e94dd37b8699617fcddce68437a7168327524`. **The instructions below apply once the application source has been migrated.** If `backend/`, `frontend/`, or `legacy_models/` is missing from a fresh Roundhouse clone, the application has not yet been published there and cannot be installed from that clone. This documentation is not evidence of a deployed service or complete release acceptance.

## Table of contents

- [1. Overview and features](#1-overview-and-features)
- [2. What the system contains](#2-what-the-system-contains)
- [3. Model-risk lifecycle](#3-model-risk-lifecycle)
- [4. Prerequisites](#4-prerequisites)
- [5. Installation and configuration](#5-installation-and-configuration)
- [6. Starting the application](#6-starting-the-application)
- [7. Guided walkthrough: from dataset to monitoring](#7-guided-walkthrough-from-dataset-to-monitoring)
- [8. Complete UI user manual](#8-complete-ui-user-manual)
- [9. API and automation reference](#9-api-and-automation-reference)
- [10. Developer and test commands](#10-developer-and-test-commands)
- [11. Troubleshooting](#11-troubleshooting)
- [12. Security, limitations and release criteria](#12-security-limitations-and-release-criteria)

## 1. Overview and features

### Why Roundhouse?

Many organizations still rely on models built years ago, with partial documents, uncertain dependencies, legacy code, and incomplete approval histories. Replacing them safely requires more than producing a new script: the historical behavior must be measured, assumptions and data lineage must be exposed, validation must be independent, and material decisions must remain with accountable people.

Roundhouse combines these operations in one inspectable, evidence-backed lifecycle:

1. **Dataset governance:** ingest original public dataset bytes, a descriptor, and verified train/validation/test membership; track immutable dataset versions and hashes.
2. **Legacy discovery:** ingest prepared ZIP model bundles or use an agent-assisted archive-intake flow that identifies candidates from messy source archives.
3. **Model portfolio:** organize registered models, versions, lineage, original source, integrity checks, lifecycle stages, and audit history.
4. **Documentation:** let an agent write structured model-risk documentation, with source references, missing information, and an independent human gate.
5. **Modernization:** rebuild eligible R models in Python and verify output equivalence through one official parity implementation shared by agents and human operators.
6. **Validation:** score frozen evaluation partitions in a restricted container, compute official metrics in a separate trusted calculation step, and produce an independent validator-agent narrative.
7. **Human governance:** preserve decisions, conditions, version-bound review checkpoints, and independent role approvals rather than auto-approving agent work.
8. **Monitoring:** compute PSI against a frozen training reference, inspect amber/red drift, and evaluate triage/investigation proposals before authorized execution.
9. **Agent assistance:** use durable streaming chat for guidance, state inspection, and explicit operation proposals, with saved conversation recovery.
10. **Auditability:** retain run results, validation reports, monitored windows, findings, proposals, immutable artifact references, and lifecycle history.

### Technology stack

| Component | Technology | Purpose |
| --- | --- | --- |
| Frontend | React 19, TypeScript, Vite, Vitest, Playwright | Authenticated workspace, model detail, review, evidence and operator workflows |
| Backend | Python, FastAPI, Pydantic, SQLAlchemy | Control plane, API contracts, authorization and persistent domain logic |
| Database | SQLite with `aiosqlite` | Local demonstration records: users, sessions, runs, models, reports, proposals, audit |
| Background processing | Redis, Celery, Celery Beat | Queued document/rebuild/validation/scoring/intake and recovery tasks |
| AI layer | Python agent harness, OpenRouter, configured Qwen model | Analyze archives, write documents, rebuild models, narrate metrics, investigate drift, chat |
| Sandboxed execution | Docker, immutable image digest | Isolated agent tooling and execution of uploaded R/Python scoring code |
| Model workstream | Python, R, scikit-learn, LightGBM, XGBoost, Cox demonstrator | Legacy ML artifacts, fixtures, reproducibility, metrics and drift checks |

For the actual process connections, trust boundaries, and data-flow diagrams, see [ARCHITECTURE.md](ARCHITECTURE.md).

## 2. What the system contains

### Expected source layout after migration

```text
roundhouse/
├── README.md                   # project overview, setup, user manual
├── ARCHITECTURE.md             # technical diagram and design details
├── backend/
│   ├── app/
│   │   ├── api/                # authenticated HTTP endpoints
│   │   ├── db/                 # SQLAlchemy models and sessions
│   │   ├── schemas/            # API payload validation
│   │   ├── services/           # ingestion, provenance, parity, validation, reporting
│   │   └── workers/            # Celery jobs and recovery
│   ├── contracts/              # harness interface
│   ├── harness/                # agent runtime, prompts, sandbox, tooling
│   ├── tests/                  # API/service/harness regression tests
│   ├── .env.example
│   └── pyproject.toml
├── frontend/
│   ├── public/
│   ├── src/                    # React live workspace
│   ├── e2e/                    # browser tests
│   ├── .env.example
│   └── package.json
├── legacy_models/
│   ├── dataset_descriptors/
│   ├── evidence/
│   ├── shared/
│   ├── integration/
│   ├── reproduction/
│   ├── tests/
│   └── <individual model directories>
├── download_datasets.py
├── validate_all_models.py
└── <other required source utilities>
```

**Documentation consolidation:** `README.md` and `ARCHITECTURE.md` are the only **top-level public documentation** requested for Roundhouse. Source files such as `backend/harness/templates/model_document.md`, `backend/harness/templates/validation_narrative.md`, and `backend/harness/guides/usage_guide.md` are **runtime prompt assets** loaded by Python; they must be preserved unless their loaders are refactored and tested. Do not blindly delete every `.md` file from the application.

### Included legacy model portfolio

Nine illustrative models are present in the original workstream:

| Domain | Model directory | Model type |
| --- | --- | --- |
| Credit default | `credit_logistic_r` | R logistic regression, especially useful for the rebuild demonstration |
| Credit default | `credit_logistic_python` | Python logistic regression |
| Credit default | `credit_lightgbm_python` | Python LightGBM |
| Credit default | `credit_xgboost_python` | Python XGBoost |
| Card fraud | `fraud_logistic_r` | R logistic regression |
| Card fraud | `fraud_logistic_python` | Python logistic regression |
| Card fraud | `fraud_lightgbm_python` | Python LightGBM |
| Card fraud | `fraud_xgboost_python` | Python XGBoost |
| Synthetic survival demo | `credit_cox_python` | Synthetic Cox proportional-hazards example; binary scoring/validation/monitoring not applicable |

The eight credit/fraud binary models are tied to publicly available UCI and ULB datasets and explicit development fixtures. Model binaries and fixtures should be disclosed and reviewed for distribution before the public code migration. The synthetic Cox model is **not** a claim of real-world credit-risk assessment.

### Dataset catalogue

| Dataset | Logical dataset ID | Rows | Train | Validation | Protected test |
| --- | --- | ---: | ---: | ---: | ---: |
| UCI Default of Credit Card Clients | `uci_credit` | 30,000 | 17,981 | 6,037 | 5,982 |
| ULB Credit Card Fraud Detection | `ulb_fraud` | 284,807 | 162,992 | 61,675 | 60,140 |

- UCI source: <https://archive.ics.uci.edu/dataset/350/default+of+credit+card+clients>
- ULB source: <https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud>

Datasets are downloaded and verified **locally**. Original data files are not supposed to be included in the Git repository. Check the source providers' applicable licensing terms before redistribution or publication of adapted data.

## 3. Model-risk lifecycle

```text
DATASET                    MODEL INTAKE                      EVIDENCE & GOVERNANCE
  |                              |                                    |
  +--> Source + descriptor       +--> Structured ZIP ----------------+--> Inventory
  |    + frozen split            |                                    |     |
  |                              +--> Unstructured archive           |     v
  |                                   Survey -> Intake -> Gate 0 -----+  Documentation
  |                                                                     |
  +-----------------------------------------------------------------> Gate 1
                                                                        |
                                                                 R -> Python rebuild
                                                                        |
                                                                 Official parity
                                                                        |
                                                                      Gate 2
                                                                        |
                                                                 Fresh validation
                                                                  + agent narrative
                                                                        |
                                                                      Gate 3
                                                                        |
                                                                 PSI monitoring
                                                                        |
                                                                  Agent proposals
                                                                        |
                                                                Human authorization

Cross-cutting: authenticated chat · findings · saved reports · history · dashboard
```

### Review gates and responsibilities

| Checkpoint | What is reviewed | Required principle |
| --- | --- | --- |
| **Gate 0** | Agent-produced unstructured archive candidate plan | Authorized developer/admin confirms selected files, dataset mapping, source provenance, plan hash, rationale |
| **Gate 1** | Generated model document and its source evidence | Independent eligible reviewer accepts, requests changes, or accepts with conditions |
| **Gate 2** | Rebuilt candidate and official parity evidence | Independent eligible reviewer checks exact model/candidate version and passing parity |
| **Gate 3** | Fresh validation report, checks, narrative and limitations | Independent approver reviews hard blockers, conditions, and measured evidence |
| **Agent proposal decision** | Typed action such as revalidation or drift disposition | Authorized user approves/rejects, then explicitly executes saved arguments if valid |

A successful job is **not** a successful review. A parity PASS is **not** a Gate 3 PASS. An LLM recommendation is **not** a human decision. Reviews must be linked to the current artifact ID and version; stale reviews and self-review (including admin self-review) are rejected.

### Validation status vocabulary

- **PASS** — a defined check met the specified policy.
- **FAIL** — a required check did not pass, or a measured condition violated the policy.
- **REVIEW** — interpretation or historical evidence requires an accountable decision.
- **NOT_ASSESSABLE** — necessary data or evidence is unavailable, so the check cannot truthfully be passed.
- **NOT_APPLICABLE** — the check does not apply to the given model or task (for example, synthetic Cox in a binary classifier workflow).

A hard FAIL cannot be overridden through ordinary conditional acceptance. REVIEW and NOT_ASSESSABLE limitations can support **Accepted with conditions** only under the stored Gate 3 policy and with explicit human acknowledgments. The report must retain its original statuses.

## 4. Prerequisites

**For a local demonstration after the code migration:**

- Python **3.12** recommended (backend package declares Python 3.11+).
- Node.js **22** and npm.
- Redis service, accessible on a known local port.
- Docker Desktop or Engine with access to the team's immutable pinned sandbox image.
- Git, free local disk space, and permission to run local processes.
- Optional: OpenRouter API key for live paid LLM tasks.
- Optional: R plus the relevant model packages for fitting/reproducing historical R models outside the app.

The frozen sandbox image referenced by the original backend environment example is hosted on **private GHCR**. Public access to Roundhouse source does **not** automatically grant access to this container package. You can run some API/frontend and unit-test workflows without it, but **real scoring, parity and agent-container demonstrations require a functioning authorized sandbox**. A compatible replacement image must be explicitly reviewed and immutable; simply substituting an arbitrary image is unsafe and invalidates some comparisons.

## 5. Installation and configuration

### 5.1 Obtain the migrated source

```bash
git clone https://github.com/Merry-Go-Round-Org/roundhouse.git
cd roundhouse

test -d backend && test -d frontend && test -d legacy_models
```

If the final test fails, the repository still lacks the application code. The earlier private workspace is the audited source, and the team must complete its migration before following the remaining commands.

### 5.2 Backend environment, dependencies and fresh database

From the project root:

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
python -m app.bootstrap --demo
```

The demo bootstrap creates separate accounts for **admin, developer, validator and approver**, each with a random password. The fresh credentials are saved locally at:

```text
backend/storage/local-demo-credentials.txt
```

This file has deliberately restricted permissions and must not be published. If you prefer to create a chosen administrator rather than demo accounts, run `python -m app.bootstrap` without `--demo`, supply the requested administrator username and a password of at least 12 characters, and create further authorized users through the admin UI/API.

**The backend database is generated locally, not migrated from the old workspace.** Keep `backend/app/db/` and `backend/app/bootstrap.py`; do **not** carry over `backend/storage/app.db`, local passwords, logs, uploads, or agent artifacts into the public repository. The application initializes SQLAlchemy tables and runtime directories on startup. The setup does not provide a general production schema-migration system.

### 5.3 Suggested local backend configuration

Edit **`backend/.env`** (never commit it). The `.env.example` file in the migrated source is authoritative for the frozen image and supported defaults. The important options are:

```dotenv
APP_NAME="Model Risk Factory API"
API_PREFIX=/api/v1
CORS_ORIGINS=["http://127.0.0.1:5173","http://localhost:5173"]
STORAGE_DIR=storage
REDIS_URL=redis://localhost:6379/0
AI_NATIVE_ENABLED=true
ALLOW_LOCAL_DEMO_AUTH=false
ALLOW_LEGACY_REGISTRATION=false
AUTH_COOKIE_SECURE=false
# Optional for offline API-only tasks; required for real agent work:
OPENROUTER_API_KEY=
# For Docker-backed scoring, set the exact immutable digest in .env.example:
# SCORING_IMAGE=ghcr.io/...@sha256:<approved digest>
```

The local defaults are deliberately **not** a public-hosting policy. For a remote HTTPS installation, use secure cookies, a trusted HTTPS origin, secure secret management, and an independently reviewed runtime deployment. Do not set CORS to wildcard or disable the origin guard to resolve login problems.

**OpenRouter:** only configure `OPENROUTER_API_KEY` in the trusted backend environment. Restart both API and worker after changing it. Provider-backed work can incur real cost; the harness has per-task and cumulative budget controls but these are not free provider credits.

**Docker:** authorized members can log in with a GitHub token that has `read:packages` access, then pull the **exact** image identified by the configured `SCORING_IMAGE`:

```bash
docker login ghcr.io -u YOUR_GITHUB_USERNAME
# Copy the approved immutable image reference from backend/.env.example
docker pull ghcr.io/merry-go-round-org/sandbox@sha256:8083453253c3267caaa0fe36b43a287b9a986d5784148ad40714cd9d3fc2d80e
docker info
```

The above digest is the one found during the source audit. An authorized operator should still confirm the appropriate current release digest and supported target architecture. Do not publish a GHCR token.

### 5.4 Frontend dependencies and browser proxy

From the project root:

```bash
cd frontend
npm ci
cp .env.example .env.local
```

The frontend uses a Vite same-origin `/api` proxy, with a server-side target such as:

```dotenv
BACKEND_API_ORIGIN=http://127.0.0.1:8000
VITE_ENABLE_PROPOSED_UPLOADS=true
```

The browser should open **`http://127.0.0.1:5173`**, then access the backend through `/api/v1`. Avoid configuring the browser to send login/session credentials directly across unrelated origins. Port `5174` is not interchangeable with `5173` for trusted-origin checking unless explicitly configured.

## 6. Starting the application

Use **separate terminals**, first creating the database and installing dependencies as described above.

### Terminal A — Redis

```bash
redis-server --bind 127.0.0.1 --port 6379
```

### Terminal B — FastAPI backend

```bash
cd backend
source .venv/bin/activate
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### Terminal C — Celery worker

```bash
cd backend
source .venv/bin/activate
python -m celery -A app.workers.celery_app:celery_app worker --loglevel=info --pool=solo
```

### Terminal D — recovery scheduler

```bash
cd backend
source .venv/bin/activate
python -m celery -A app.workers.celery_app:celery_app beat --loglevel=info --schedule storage/celerybeat-schedule
```

### Terminal E — React frontend

```bash
cd frontend
npm run dev
```

The worker's solo pool avoids certain macOS process-pool problems; start with **one API instance and one worker** for the local SQLite environment. Start the API before the worker to ensure tables exist. Do not launch duplicate processes against the same local state without a deployment design.

### Browser addresses and checks

| Purpose | URL |
| --- | --- |
| Live Roundhouse workspace | <http://127.0.0.1:5173> |
| Separate sample-only UI | <http://127.0.0.1:5173/demo> |
| API liveness/configuration | <http://127.0.0.1:8000/health> |
| FastAPI interactive documentation | <http://127.0.0.1:8000/docs> |
| OpenAPI JSON | <http://127.0.0.1:8000/openapi.json> |
| HTTP SSE transport diagnostic, authenticated | `/api/v1/diagnostics/stream` |

Check API availability with:

```bash
curl -s http://127.0.0.1:8000/health
```

A healthy response does **not** confirm Redis, OpenRouter, Docker, worker dispatch, or real end-to-end operation. Log in with a generated developer/admin account and verify that authenticated pages load before proceeding.

## 7. Guided walkthrough: from dataset to monitoring

This manual walkthrough uses **UCI Default of Credit Card Clients** and the **R logistic regression legacy model**. It is the clearest path to demonstrating the separation between source ingestion, agent documentation, model modernization, deterministic evidence, and human review.

### 7.1 Prepare dataset source and frozen split

Run from the project root:

```bash
python download_datasets.py uci ulb
python legacy_models/integration/prepare_dataset_uploads.py --with-source
python legacy_models/integration/prepare_dataset_uploads.py --verify-only --with-source
```

For UCI, the staging directory will contain approximately:

```text
datasets/dataset_uploads/uci/
├── default of credit card clients.xls
├── dataset.json
└── uci_split_ids.csv
```

For ULB, a parallel directory provides `creditcard.csv`, `dataset.json`, and `ulb_split_ids.csv`. The staging script checks original data and canonical split SHA-256 values; the resulting `build_report.json` is a **local preparation report**, not proof of backend acceptance.

In the **Datasets** page, choose **Add dataset** and supply the source and descriptor, with the split file if desired. **The split field is optional for the two recognized, verified canonical dataset families.** When omitted, the backend reuses the exact UCI split or reconstructs the exact pinned ULB split. This preserves model compatibility; it does not create a new random allocation.

**Important:** the audited implementation only recognizes the dataset IDs **`uci_credit`** and **`ulb_fraud`**. A proposed general feature to generate stratified splits and assign server-generated IDs to arbitrary new datasets was not yet integrated. Do not describe arbitrary dataset upload as supported.

Successful verification checks include original source hash, descriptor columns, split hash, complete unique row membership, partition counts, and immutable dataset version. A changed source under an existing immutable version is rejected.

### 7.2 Build model upload ZIPs

From the project root:

```bash
python legacy_models/integration/build_upload_bundles.py
python legacy_models/integration/build_upload_bundles.py --verify-only
```

This produces model ZIPs in `datasets/upload_bundles/`. The R demonstration archive is usually named `credit_logistic_r.zip`. A structured ZIP contains `manifest.json` at the **archive root** and the associated schema, artifact, scoring code, documentation when present, and development fixtures. The builder records checksums and provenance; the ZIPs themselves should remain ignored rather than committed to Git.

### 7.3 Structured model upload (recommended first demonstration)

1. Sign in as a **developer**.
2. Open **Inventory → Upload model**.
3. Select the prepared `credit_logistic_r.zip` and submit.
4. Inspect the queued/running/completed upload record and its per-check results.
5. Open the resulting inventory model and inspect its source language, dataset version, metadata provenance, preserved files, and lifecycle.

The upload workflow does not train the model or declare it validated. The **reproduction** check confirms compatibility with known development outputs; it is not the later R-to-Python official parity result and not an independent evaluation on protected test rows.

### 7.4 Alternative: unstructured archive intake (Gate 0)

For a messy historical archive without a valid manifest, use **Archive intake → Upload archive** instead:

1. Upload the ZIP containing model weights, code, and whatever notes are available.
2. The system safely inspects the archive and queues **survey** and **intake** agents.
3. Open the saved plan containing candidate models, inferred fields, source references, dataset mapping, proposed scoring wrapper, hashes, and warnings.
4. Inspect the evidence and decide which candidates are genuine.
5. As a developer/admin, provide the required rationale and **Confirm Gate 0 plan**, bound to the exact plan hash.
6. The backend registers only accepted candidates, builds vetted bundles, and queues documentation.

Gate 0 is **human confirmation of an intake proposal**, not approval for deployment. A candidate can be recorded as incomplete or not executable rather than forcing a false green outcome. Do not upload personal data or confidential business data into the public demonstration.

### 7.5 Review generated documentation (Gate 1)

After model registration, open its **Documentation** tab. Inspect the agent-written model document, original notes and code citations, fields marked unknown, assumptions, evidence, and saved versions. The document structure is checked automatically; factual correctness must still be independently reviewed.

An eligible reviewer who **did not initiate** the run opens **Review queue**. Depending on the review state, they may choose **Accepted**, **Accepted with conditions**, or **Changes requested**, supplying rationale and any required conditions. Requested changes result in a new reviewable version or continuation; old evidence remains preserved.

### 7.6 Rebuild the R model and check official parity (Gate 2)

For `credit_logistic_r`, open **Rebuild & parity** and request a Python modernization. The agent uses a restricted Docker workspace to produce a candidate, which is recorded separately from the original package.

The platform's official **`parity-v1`** service then scores the candidate on **100 fixed development-fixture rows** and compares each `positive_probability` with the parent model's reference output. Parity passes only when all absolute differences are at most **`1e-8`** (relative tolerance zero). The same official backend function serves both agent-initiated and person-initiated parity.

A **separate eligible reviewer** makes the Gate 2 decision on that exact candidate version. A successful agent-run self-check does not replace the official parity record. A parity pass on 100 development rows is **behavioral compatibility evidence**, not a guarantee of predictive quality, dataset lineage, or regulatory compliance.

### 7.7 Run independent validation and Gate 3

An authorized validator initiates validation. The backend verifies the package and fixed dataset version, scores the intended partitions in isolated containers, and calculates official performance/calibration/fairness/stability evidence **outside the agent**. A separate validator agent reads a trusted metrics bundle and writes an explanatory narrative and potential findings without changing the calculations.

The report preserves **PASS / FAIL / REVIEW / NOT_ASSESSABLE / NOT_APPLICABLE** statuses. The validation and human-review UI includes stored reports, underlying checks, source references, and narrative. Gate 3 requires an **independent approver** who may only make the decisions allowed by the stored blockers/limitations. Required hard FAILs prevent acceptance; genuine limitations may require explicit conditions without changing their status.

The UCI protected test partition contains **5,982 rows**. Protected test data/labels are not agent development inputs. Do not bypass Gate 3 or directly edit SQLite to create a fake approved state for a demonstration.

### 7.8 Monitor the approved model

Once the backend has recorded valid Gate 3 approval, open **Monitoring**. Two demonstrated input paths exist:

- **Uploaded batch:** a permitted feature-only CSV with row identities and a monitoring window label.
- **Simulated window:** a backend-controlled, explicitly labelled shift of a feature in validation-partition data, never a claim of production measurements.

The system records **Population Stability Index (PSI)** against a frozen training reference. As defined by the project policy, **green** is below 0.10, **red** is above 0.25, and intermediate values are treated as amber. These are heuristic drift thresholds, not authoritative regulatory rules.

An amber signal may trigger triage and a red signal investigation. Agents can suggest bounded actions through the **Agent proposals** page; only an authorized person may approve/reject the proposed action and execute its stored arguments. Revalidation due is not the same as automatic model promotion.

### 7.9 Review history and evidence

Use the **History**, **Review queue**, **Validation**, **Monitoring**, **Agent proposals**, and **Dashboard** views to inspect retained evidence. When reporting a result, identify the exact model version, dataset version, run ID, report/artifact ID, reviewer, decision, and any limitations. Present actual provider cost and elapsed times only when recorded; never infer savings from the presence of automated features.

## 8. Complete UI user manual

### Live navigation

The current React source declares these workspace pages:

| Page | What it shows / what to do |
| --- | --- |
| **Workspaces** | Select a saved model context and navigate to its details. |
| **Inventory** | Search/filter model records, open Overview, upload structured model ZIPs, preview eligible soft deletions. |
| **Datasets** | Inspect registered dataset versions, verify ingestion, upload source/descriptor/split, review deletion impact. |
| **Archive intake** | Track unstructured ZIPs, review source provenance, accept/exclude candidates at Gate 0. |
| **Review queue** | Inspect pending/decided reviews and submit eligible Gates 1–3 decisions. |
| **Monitoring** | Review portfolio drift, run history, and permitted monitoring data windows. |
| **Dashboard** | Review persisted model counts and workflow summaries. |
| **Agent proposals** | See suggested actions, required role, preconditions, and explicit decisions/executions. |
| **Accounts** | Admin-only user listing, account creation and disabling. |

Within a model, the tabs are **Overview**, **Documentation**, **Rebuild & parity**, **Validation**, **Monitoring**, and **History**. The current in-memory `/demo` is **separate from the live authenticated workspace**. Its example data should not be used as evidence of real uploads, validation, or stored approvals.

### Account roles and permissions

| Role | Typical actions |
| --- | --- |
| **Developer** | Upload datasets/models/archives, confirm eligible Gate 0 plans, request documentation and rebuild, inspect relevant models. |
| **Validator** | Independently inspect eligible reviews, request validation, assign/review applicable findings. |
| **Approver** | Independently sign off Gate 3 and permitted proposals/reviews. |
| **Admin** | Manage users and perform authorized administration, but cannot self-review. |

Server-side role checks and exact saved review eligibility are authoritative. A disabled control may be correct when prerequisites, actor independence, or stale-version conditions are not met; if behavior is unexpected, inspect the saved review blockers rather than forcing a decision.

### Persistent chat assistant

The floating chat assistant uses authenticated **Server-Sent Events (SSE) over HTTP**, not WebSockets. It can explain the workflow, retrieve supported model/dataset information, interpret saved evidence and suggest an operation through a proposal or confirmation card.

- Select the correct model context before asking about a particular model.
- Separate explanatory text from an actual platform action: **chat text never approves a Gate**.
- Read a proposal card's **model ID, version, operation, preconditions and required role** before approving.
- Use **Reject** for proposals with unsupported scope, unknown data, or incorrect identity.
- An interrupted stream is not proof that its server-side work was cancelled. Restore the saved conversation/turn and reconcile state before retrying.
- The assistant must not manufacture a model count or numerical metric if the trusted API did not supply it.

### Reviews and conditional acceptance

A reviewer should open the exact pending record, verify artifact ID/version and source evidence, inspect hard blockers and limitations, and record rationale. **Changes requested** should specify actionable feedback. **Accepted with conditions** requires each condition to be explicit and recorded. Review decisions are version-bound; a fresh candidate or updated report requires rechecking the current record. Gate 3 cannot be accepted in any form with an unresolved required hard FAIL.

### Bulk deletion and recovery

The backend's model/dataset deletion contracts use **preview → confirmation → atomic soft deletion**. A selection with any blocker fails as a whole, without partial deletion. Selected model IDs and dataset **version** IDs are passed to the server. Developer deletion is limited to the user's eligible uploads; administrators can act within the server policy.

Soft-deleted records disappear from active portfolio views, but **underlying model-risk audit evidence and uploaded files are retained**. Re-uploading identical eligible content may restore a record and creates an audit event. This operation is **not secure erasure**. Do not delete `storage/app.db` or individual upload folders to imitate the product workflow.

### User management

The audited authentication code supports authenticated sign-in/sign-out, an admin-only user listing, creation and disabling. **Re-enabling a disabled account was still an unresolved backend/frontend feature** in the audited implementation; do not claim it exists unless verified in a later commit. Separate developer, validator and approver accounts are important for demonstrating genuine review independence.

## 9. API and automation reference

FastAPI publishes the authoritative schema at **`http://127.0.0.1:8000/openapi.json`** and interactive docs at **`http://127.0.0.1:8000/docs`**. Most application endpoints have the `/api/v1` prefix and require an authenticated cookie session.

| Feature | Method and example relative endpoint | Meaning |
| --- | --- | --- |
| Authentication | `POST /auth/login`, `GET /auth/me`, `POST /auth/logout` | Create/check/revoke sessions. |
| User administration | `GET /auth/users`, `POST /auth/users`, `POST /auth/users/{id}/disable` | Admin-scoped accounts. |
| Dataset registration | `POST /datasets`, `GET /datasets`, `GET /datasets/{id}` | Multipart source/descriptor/optional split and version inspection. |
| Model ZIP upload | `POST /models/uploads`, `GET /models/uploads/{id}` | Asynchronous bundle ingestion and checks. |
| Model portfolio | `GET /models`, `GET /models/{id}`, `GET /models/{id}/history` | Saved inventory and historical state. |
| Unstructured intake | `POST /intake/archives`, `GET /intake/archives/{id}`, `POST /intake/archives/{id}/decision` | Survey/intake plan and human Gate 0. |
| Model jobs | `POST /models/{id}/runs`, `GET /runs/{id}`, `POST /runs/{id}/resume` | Document/rebuild/validate and continuation. |
| Parity | `POST /models/{id}/versions/{version}/parity`, `GET /models/{id}/parity-runs` | Official recorded comparison. |
| Reports/artifacts | `GET /models/{id}/reports`, `GET /reports/{id}/markdown`, `GET /artifacts/{id}/download` | Source/metrics and version-bound downloads. |
| Human reviews | `GET /reviews`, `GET /reviews/{id}`, `POST /reviews/{id}/decision` | Independent human governance. |
| Findings | `GET /findings`, `PATCH /findings/{id}` | Persisted reviewer-assigned findings. |
| Monitoring | `POST /models/{id}/monitoring/batches`, `POST /models/{id}/monitoring/simulate` | Drift batch or declared simulation. |
| Proposals | `GET /proposals/actions`, `POST /proposals/{id}/decision`, `POST /proposals/{id}/execute` | Typed, human-authorized actions. |
| Chat | `POST /conversations`, `POST /conversations/{id}/messages`, `GET /conversations/{id}/turns` | Persistent conversations with replayable SSE. |
| Dashboard | `GET /dashboard` | Persisted portfolio summaries. |

**Important API rules:**

- HTTP `202 Accepted` means the work was queued/persisted, not completed.
- Treat a `401` as an authentication failure; a `403` as a permissions or trusted-origin failure; a `409` as a state/version/prerequisite conflict; and a `422` as an invalid contract or source.
- Use saved job records and retry/recovery endpoints. Do not blindly re-POST after an uncertain network result.
- For uploads and review decisions, use exact immutable IDs, not display names.
- The OpenAPI document is the source of truth for request payloads, response fields and versioned behavior; this table is an orientation aid.

### Example authenticated API check

A browser login is easiest because the app handles cookies and CSRF/origin constraints. For a simple unauthenticated liveness check:

```bash
curl -i http://127.0.0.1:8000/health
curl -I http://127.0.0.1:8000/openapi.json
```

To inspect private endpoints, use a valid browser session or configure a local HTTP client to retain the returned cookie and send the expected trusted origin. Do **not** create public scripts that hard-code a demo password.

## 10. Developer and test commands

### Backend and harness

Run from the `backend/` directory after activating `.venv`:

```bash
python -m pytest tests -q
python -m pytest tests/agent_harness -q
python -m harness.dev.cli
```

The scripted harness CLI demonstrates task management without real OpenRouter/Docker billing. Real agent and Docker tests are separately opt-in and require the expected environment and authorization. Never interpret a mocked provider result as a complete live-provider acceptance run.

### Frontend

Run from `frontend/`:

```bash
npm run build
npm test
npx playwright install chrome
npm run test:browser
```

The browser tests include mocked and environment-dependent cases. Distinguish contract/UI test passes from a real deployed browser+API+worker+LLM+Docker lifecycle.

### Legacy model evidence and packaging

Run from the repository root:

```bash
python -m unittest discover -s legacy_models/tests -p 'test_*.py' -v
python legacy_models/integration/build_upload_bundles.py
python legacy_models/integration/build_upload_bundles.py --verify-only
python validate_all_models.py --models-dir legacy_models --policy legacy_models/validation_policy.json --out datasets/reports/validation
```

The default standalone validator reviews saved/model-associated evidence without executing every uploaded scorer. Its output is **not** proof of a human Gate 3 decision. Running R and Python scorers under the full isolated integration environment requires separate opt-in commands and appropriate dependencies.

### Release verification checklist

Before a public release, verify at least:

1. A **fresh clone** contains real application code and passes the documented installation steps.
2. Git contains **no secrets, original customer data, private test labels, local SQLite DB, runtime logs or generated credentials**, including in publication history.
3. The frontend production build, frontend unit tests, backend/harness tests, and model-bundle integrity tests pass on the release commit.
4. The Redis queue, Docker scoring image, single API/worker process and browser same-origin authentication work together in a new isolated workspace.
5. Both canonical dataset paths, structured model upload, unstructured Gate 0, documentation/Gate 1, rebuild/parity/Gate 2, fresh validation/Gate 3, monitoring and action approval have independently recorded results.
6. Any failed, partial, blocked or not-assessable test is documented rather than counted as a pass.
7. Model-risk limitations and policy ownership are explicit; no auto-approval is claimed.
8. The source commit, image digest, dataset/split hashes, versions, test dates and known blockers are recorded in the release notes or verified artifacts.

## 11. Troubleshooting

| Observation | Investigation and corrective action |
| --- | --- |
| **403: Untrusted request origin** at login | Match the actual browser scheme, hostname and port in `CORS_ORIGINS`. `localhost:5173` and `127.0.0.1:5173` differ. Restart FastAPI after changing `.env`; do not use wildcard origins. |
| **401 Unauthorized** on model/agent routes | Sign into the **live** workspace, not the in-memory `/demo`; confirm the session cookie and proxy. |
| **429 Too many sign-in attempts** | Repeated wrong credentials triggered a temporary lockout; check your local generated password file rather than guessing. |
| API health passes, uploads stay queued | Health is only a liveness check. Inspect Redis, the Celery worker, upload/run status, and worker logs. |
| Model uploaded but automatic document not available | Live agent work needs `OPENROUTER_API_KEY`, a running worker and correct provider model access. Restart services after changing configuration, then start an explicit supported new run. |
| Docker error or container cannot start | Start Docker, check permissions, pull the immutable image digest and confirm platform architecture. A missing private GHCR entitlement cannot be solved by changing model-risk policy. |
| Dataset SHA or split mismatch | Use the original provider bytes and known canonical split, verify source and descriptor, do not modify hashes to force acceptance. |
| Missing automatic split for new dataset | Arbitrary dataset families/generation are not integrated. Use the two supported canonical dataset IDs with matching descriptors. |
| Model upload does not appear in inventory | Poll its persisted upload status and read validation errors. A queued upload is not a completed inventory record. |
| Gate 1/2/3 submit button disabled | Inspect role, independent reviewer identity, mandatory rationale/conditions, current artifact version, official parity and validation blockers. |
| Gate 2 blocked | Verify an official `parity-v1` PASS was saved for the exact rebuilt candidate. Development reproduction is not official parity. |
| Gate 3 blocked | Inspect required hard FAILs and actual limitations; there is no safe database shortcut for a review. |
| Agent exceeds budget or provider fails | Read saved run state, cost/usage fields, output/turn limits and provider availability. Do not silently raise budget limits. |
| SSE disconnects or a write outcome is unknown | Restore the conversation/turn or saved operation receipt before retrying. Disconnect does not necessarily cancel work. |
| Deletion reports a blocker | Use the server impact preview; record dependencies and role requirements. Soft-delete preserves audit evidence and does not erase files. |

## 12. Security, limitations and release criteria

### Runtime files excluded from the repository

**Never publish** these files or their contents:

```text
backend/.env
backend/storage/app.db
backend/storage/*.sqlite*
backend/storage/local-demo-credentials.txt
backend/storage/imports/
backend/storage/datasets/
backend/storage/artifacts/
backend/storage/logs/
frontend/.env.local
datasets/                  # local source files and generated upload bundles
tmp/                       # temporary processing files
output/                    # generated artifacts
```

The **source code for database models, account bootstrap and artifact storage** is tracked; only runtime state is excluded. SQLite credentials are generated afresh when a user initializes their own installation. Do not transplant the older team's database into a public repository to make a demo appear pre-populated.

### Security boundaries

- Authenticated user sessions are stored server-side. Passwords are Argon2-hashed; the browser uses an HTTP-only cookie.
- The backend validates origins for cookie-authenticated writes; permissions are server-enforced.
- Agent tools and model scoring run in restricted Docker environments with network access disabled, resource limits and controlled mounts.
- Private provider keys stay in trusted API/worker configuration and are not sent into the agent container, browser, or Celery job payloads.
- Protected test labels are separated from agent-visible development data.
- Model versions, artifact hashes and human review checkpoints are recorded independently.
- Model/dataset deletion is **soft deletion**; retained files and evidence are not necessarily erased.

These controls are part of a prototype architecture. They do **not** establish production penetration-test certification, regulatory compliance, public multi-tenancy readiness, or formal exactly-once distributed execution.

### Scientific and governance limitations

- **UCI target:** next-month default rather than one-year probability of default, so it is not a regulatory PD model by itself.
- **ULB target:** real card-fraud labels and strong class imbalance; demographic group fairness cannot be inferred where protected-attribute labels are absent.
- **Legacy fit provenance:** current frozen data split hashes do not reconstruct every historical training membership attestation.
- **XGBoost portability:** numerical differences have been observed across environments for some artifacts and must remain visible in release evidence.
- **Cox model:** only a synthetic survival-analysis demonstrator, not an ordinary binary credit/fraud validation model.
- **Parity:** closeness of 100 fixed development predictions does not imply robust out-of-sample performance.
- **PSI:** marginal shift heuristic, not proof of causality or financial loss.
- **Governance thresholds:** owner-approved acceptance and fairness criteria are necessary for real deployment; absence of criteria must be declared, not silently passed.

### Recorded acceptance state

The last consolidated **99-case human/chat-flow log dated 11 November 2026** recorded:

| Status | Count |
| --- | ---: |
| Pass | 70 |
| Fail | 5 |
| Partial | 9 |
| Blocked | 2 |
| Inconclusive | 1 |
| Unverified | 12 |
| **Total** | **99** |

Some older reported passes lacked retained independent artifacts. These figures **do not mean 99/99 tests passed**, and they must not be carried forward as fresh release acceptance after migration or code changes.

### Attribution and intended use

Roundhouse was produced for the EXL × UCD hackathon as a demonstration of agentic ML governance. Workstreams were led by **Wit** (legacy models and deterministic evidence), **Aditya** (backend and integrations), **Justin** (agent harness and sandbox), and **Yuki** (frontend and interaction workflows). Third-party data, packages, code and model artifacts retain their applicable terms; any repository-level license should be selected by the team only after verifying rights to redistribute all included materials.

**Not intended for real lending, fraud adjudication, or regulated deployment without independent validation, legal review, security assessment, and approved governance controls.**

---

**Technical architecture:** [ARCHITECTURE.md](ARCHITECTURE.md) · **Runtime API reference:** `/docs` and `/openapi.json` on a running backend · **Release readiness:** depends on successful source migration and independent test evidence.
