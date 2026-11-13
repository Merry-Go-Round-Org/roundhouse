# F1 split policy — canonical reuse, deterministic generation, immutable dataset IDs

**Status:** proposed cross-team contract, not a claim that all behavior is implemented.
**Scope:** dataset upload F1 / Issue #31. This document does not change any legacy data, fitted model or existing bundle.

## 1. Supported upload paths

| Inputs | Existing `uci_credit` or `ulb_fraud` | New independent binary dataset |
| --- | --- | --- |
| Source + descriptor (two files) | Reuse exact frozen canonical split after trusted source-hash check | Proposed: generate deterministic stratified split, server issues ID/version |
| Source + descriptor + split (three files) | Accept **only** byte-identical canonical split | Validate complete unique row membership and immutable registration; do not resplit |
| Source changed, ID unchanged | Reject as incompatible with canonical family (no silent split regeneration) | Create a distinct new family/version under explicit server policy |
| Model uploaded | Require pinned source SHA, dataset version ID and split SHA | New model must declare the matching version/split |

**Existing partial implementation:** `backend/app/api/uploads.py` accepts an optional split and `backend/app/services/upload_service.py` reuses UCI's committed CSV or reconstructs ULB's pinned CSV. Generic generation, new family IDs, trusted registry enforcement for explicit uploads, split-aware model binding and split downloads **are not integrated**. The descriptor currently requires `dataset_id`, `split_sha256` and `partition_counts` and restricts IDs to the two historical families.

## 2. Trusted immutable canonical registry

| Field | UCI | ULB |
| --- | --- | --- |
| `dataset_id` | `uci_credit` | `ulb_fraud` |
| Original source SHA-256 | `30c6be3abd8dcfd3e6096c828bad8c2f011238620f5369220bd60cfc82700933` | `76274b691b16a6c49d3f159c883398e03ccd6d1ee12d9d8ee38f4b4b98551a89` |
| Frozen split SHA-256 | `e8feb21a0c57c5d6c8344af11aaaab079064f259e4861faad8f4aef40fe6d6cb` | `5d9edf4df20e866d3198e74893fd2e1cf75f5fc3c5b4e175efd5a4a478b827fa` |
| Train / validation / test | 17,981 / 6,037 / 5,982 | 162,992 / 61,675 / 60,140 |
| IDs | `uci-{original ID:06d}` 1-based | `ulb-{original CSV row_index:06d}` 0-based |
| Source of split | `legacy_models/evidence/uci_split_ids.csv` | `legacy_models/shared/restore_ulb_split.py` |

Validation must compare against **trusted server-side values**, never treat uploader-supplied `sha256`/`split_sha256` as sufficient proof. Explicit split bytes must hash to the pinned canonical digest. On two-file reuse, copy exact committed canonical UCI bytes or exact SHA-verified restored ULB bytes, not a new split or rewritten/reordered CSV. Check source row identity, full coverage, uniqueness, counts, and ULB original row order. Never overwrite frozen split evidence. Reused uploads should remain idempotent.

## 3. New independent binary dataset: `f1-stratified-hash-v1`

This is a **reference rule proposed for implementation**; not an implicit fallback for existing `uci_credit` / `ulb_fraud`, temporal data, group-correlated data, survival analysis, regression, or data with nonbinary targets.

* Require a descriptor-declared stable key column (proposed `row_id_column`), 1:1 unique values, not source row position; IDs are non-empty, restricted to ASCII `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}` for v1. Reject missing, duplicate or malformed IDs and nonbinary/missing targets. Existing legacy generated IDs are exempt from the new generic key rule.
* An i.i.d. classification-only policy requires **at least five examples of each class**, with labels exactly 0/1. If the descriptor includes group/time semantics, fail closed and require an explicit split or a separately reviewed grouping/time policy.
* Fix `algorithm = "f1-stratified-hash-v1"`, `seed = 20261110`, target ratios `train=0.60`, `validation=0.20`, `test=0.20`. Sort within each class by `SHA256(UTF8("f1-stratified-hash-v1\\n" + decimal(seed) + "\\n" + row_id))`, breaking ties by row ID.
* For each class with n rows, compute integer quotas from [6n/10, 2n/10, 2n/10]; allocate remainders by largest remainder, with deterministic ties `train`, then `validation`, then `test`. Assign ranked rows in that order to each partition. The same stable rows and configuration yield the same memberships irrespective of source-record order.
* Emit normalized split CSV bytes as UTF-8, no BOM, LF newlines, header `row_id,split\n`, sorted by row ID using bytewise ASCII sort. Store `split_sha256` of these bytes, partition counts and `assignment_sha256` for a separately canonicalized user-provided split. Never substitute an assignment fingerprint for the uploaded file's byte SHA.
* Changing seed, strategy, split configuration, or membership **always** creates a new immutable dataset version. A new version cannot retroactively change which split an existing model uses.

**Why this is not UCI/ULB resplitting:** UCI originally uses grouped predictor profiles; ULB uses time order. A new stratified hash split on either original source would leak historical compatibility and invalidate current evidence.

## 4. Server-issued ID and immutable version

* Reserve `uci_credit` and `ulb_fraud` permanently. A new family receives server-generated `dataset_id = "ds_" + uuid4().hex` (35 characters, fitting the existing 40-character DB field), separate from `version_id = str(uuid4())` (existing `DatasetRecord.id`).
* `dataset_id` identifies a family, `version_id` identifies exactly one immutable uploaded source plus split and descriptor; keep the existing `version` string as human metadata. The server must validate family ownership when adding versions. The client must not choose reserved IDs or silently repurpose an existing ID.
* Exact retry: return existing `version_id` if source/descriptor/split are identical, otherwise 409 conflict for a reused family+version. Use idempotency keys or transactional unique constraints to handle concurrent duplicates. Do not guess identity from upload filename.
* Backend `DatasetDescriptor` must be extended for new datasets without losing legacy `dataset_id` and required fields. For generated splits, `partition_counts` and `split_sha256` are **server outputs**, not required client inputs; otherwise two-file new-dataset creation cannot work.
* Frontend catalogue, model types, inventory and browser APIs must accept new `ds_...` IDs in addition to reserved historical IDs. Avoid hard-coding only UCI/ULB in `DatasetId` and `DATASET_CATALOG`.

## 5. Proposed persisted contract (agree with Aditya/Yuki)

```json
{
  "id": "ds_<32 lowercase hex>",
  "version_id": "<immutable UUID>",
  "status": "completed",
  "dataset": {
    "id": "ds_<32 lowercase hex>",
    "version_id": "<immutable UUID>",
    "version": "v1",
    "sha256": "<source SHA-256>",
    "split_sha256": "<on-disk split bytes SHA-256>",
    "split_provenance": {
      "origin": "auto_generated",
      "algorithm": "f1-stratified-hash-v1",
      "seed": 20261110,
      "ratios": {"train": 0.6, "validation": 0.2, "test": 0.2},
      "assignment_sha256": "<canonical membership SHA-256>"
    },
    "statistics": {
      "partition_counts": {"train": 0, "validation": 0, "test": 0}
    }
  }
}
```

`origin` values: `uploaded`, `canonical_reused`, `auto_generated`. For canonical reuse set algorithm/seed to null: historical methodology is external to the reuse operation. A **proposed**, not existing, authenticated endpoint is `GET /api/v1/datasets/{version_id}/split` with immutable version-scoped download, hash check and appropriate access control. The API status currently resolves synchronously; asynchronous job IDs/polling require separate agreement. Restrict exposure of held-out test labels, partition contents and sensitive source rows in agent-facing responses.

## 6. Model compatibility gate (P0)

A model must bind to `dataset_version_id` **and** `dataset_id`, exact original source SHA-256, exact split SHA-256, expected target and positive class. Do not use newest record matching only `dataset_id + source_sha256`. Reject ambiguous version linkage or incompatible split even if sample development fixture IDs happen to coincide. Validate full provenance and Gate 3 as a separate step.

The eight historical UCI/ULB logistic/LightGBM/XGBoost Python/R binary models retain their frozen memberships. `credit_cox_python` uses synthetic survival data and is outside binary F1 split generation.

## 7. Regression fixture matrix

| Case | Expected |
| --- | --- |
| UCI frozen CSV SHA, exact 30,000 ID order, original partition counts | PASS |
| ULB restored 284,807-ID CSV, exact trusted SHA, partition counts | PASS |
| Canonical no-split decision with matching trusted source | REUSE |
| Canonical source mismatch, or altered explicit canonical split | REJECT |
| Uploaded split with duplicate, missing, extra, overlapping or invalid partitions | REJECT |
| Generated new split, same seed and rows in any order | SAME BYTES/SHA |
| Different seed, same independent rows | NEW SPLIT |
| Generated new split, insufficient class, missing/duplicate ID, nonbinary label, group/time | REJECT |
| New dataset ID generation | `ds_<32-hex UUID>`, reserved IDs untouched |
| Actual backend two-file canonical and three-file explicit upload | **Pending integration/E2E** |
| Actual backend new dataset generation, persistence, download and model binding | **Pending implementation/integration/E2E** |

Reference implementation: `legacy_models/shared/f1_split_contract.py` and executable regression tests: `legacy_models/tests/test_f1_split_contract.py`. These deliberately have **no production ingestion side effects** until the cross-team API agreement.

## 8. Team ownership

* Wit: canonical source+split SHA registry, deterministic policy and unit fixtures, legacy-model compatibility and Gate 3 expectations.
* Aditya: descriptor/API/DB changes, idempotency, version pin, strict canonical enforcement, protected download, integration tests.
* Justin: sandbox scoring/parity/Gate 3 against explicitly selected immutable version.
* Yuki: user-visible policy, new dataset-ID support, provenance and downloads once endpoints are agreed.
