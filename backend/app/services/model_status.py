"""What the assistant's get_model_status and find_models tools return.

Every value comes from a platform record. The assistant must not derive a model's stage, a
check's meaning or a parity number itself, so this module returns them as recorded, together
with the rules for reading them.
"""

from __future__ import annotations

import math

from sqlalchemy import func, or_, select

from app.db.tables import ModelRecord, ReportRecord, ReviewRecord, runningRecord

RUN_LIMIT = 8
PROPOSAL_LIMIT = 10
REPORT_LIMIT = 3
FIND_LIMIT = 20

# Which review gate a run's review belongs to, by the run's operation.
GATES = {"document": 1, "rebuild": 2, "recalibrate": 2, "validate": 3}

READING_RULES = (
    "model.lifecycle_status is the authoritative lifecycle stage; never infer a stage from "
    "reports or runs. Quote model IDs and versions in full. A report check status is a measured "
    "result: PASS passed; FAIL failed; REVIEW needs a person's judgement, for the reason in its "
    "summary; NOT_ASSESSABLE means the evidence to assess it is missing, which is not a "
    "disagreement or mismatch; NOT_APPLICABLE does not apply. Quote a check's summary rather "
    "than restating it. Parity and reproduction numbers are as recorded; scope says what each "
    "covers. Proposals are not runs: a proposal starts nothing until it is approved and then "
    "executed. A run with revision_requested true was sent back by a reviewer: call it a "
    "revision request continued by the run in continued_by_run_id, never a technical failure; "
    "only a failed run without it failed, for the reason in its error. A run's revision_of "
    "names the review that created it; a run without revision_of was started on its own, even "
    "if it was created around the same time. "
    "gate_1 says whether the current document of this exact version is accepted, and "
    "operations that need Gate 1 check only that. A pending document review with superseded "
    "true belongs to an older document run: it never blocks anything, so do not ask the user "
    "to decide it first. "
    "For a pending review, allowed_decisions lists exactly what its card accepts now. "
    "hard_blockers are failed checks: they allow only changes_requested. limitations are "
    "required checks that are REVIEW or NOT_ASSESSABLE: they rule out plain acceptance, and "
    "accepted_with_conditions then needs one written condition naming each limitation's "
    "check_id. Never promise a decision outside allowed_decisions. "
    "model_inputs.feature_order lists the exact, case-sensitive input names: use only these "
    "for drift simulations. audit_only_columns are in the dataset but are never model inputs. "
    "A drift factor must be greater than 0 and at most 100."
)


def model_view(model: ModelRecord) -> dict:
    return {
        "id": model.id,
        "name": model.name,
        "version": model.version,
        "language": model.language,
        "dataset_id": model.dataset_id,
        "purpose": model.purpose,
        "owner": model.owner,
        "lifecycle_status": model.lifecycle_status,
    }


def run_view(run: runningRecord) -> dict:
    return {
        "id": run.id,
        "model_id": run.model_id,
        "model_version": run.model_version,
        "operation": run.operation,
        "status": run.status,
        "stage": run.stage,
        "error": run.error,
        # A review that asks for changes closes the reviewed run as failed, with no error.
        "revision_requested": run.status == "failed" and run.stage == "changes_requested",
        "created_at": run.created_at,
        "finished_at": run.finished_at,
    }


async def run_links(session, runs) -> dict:
    """For each run: the review that created it (revision_of) and the run that continued it."""
    from app.db.tables import AuditRecord

    ids = [r.id for r in runs]
    if not ids:
        return {}
    audits = list(
        await session.scalars(
            select(AuditRecord).where(
                AuditRecord.action == "document_revision_requested",
                AuditRecord.resource_id.in_(ids),
            )
        )
    )
    reviews = {
        r.id: r
        for r in await session.scalars(
            select(ReviewRecord).where(
                ReviewRecord.id.in_([(a.payload or {}).get("review_id") for a in audits])
            )
        )
    }
    links: dict = {}
    for audit in audits:
        review = reviews.get((audit.payload or {}).get("review_id"))
        if review is None:
            continue
        links.setdefault(audit.resource_id, {})["revision_of"] = {
            "review_id": review.id,
            "reviewed_run_id": review.run_id,
        }
        links.setdefault(review.run_id, {})["continued_by_run_id"] = audit.resource_id
    return links


def _check_view(check: dict) -> dict:
    return {
        key: check.get(key)
        for key in ("check_id", "status", "required", "summary", "evidence_kind")
        if key in check
    }


def report_view(report: ReportRecord) -> dict:
    payload = report.payload or {}
    view = {
        "id": report.id,
        "kind": report.kind,
        "run_id": report.run_id,
        "artifact_id": report.artifact_id,
        "created_at": report.created_at,
        "summary": payload.get("summary"),
        "limitations": payload.get("limitations"),
    }
    checks = payload.get("checks")
    if isinstance(checks, list):
        view["checks"] = [_check_view(c) for c in checks if isinstance(c, dict)]
    # Chat receives only aggregate, freshly computed figures from a persisted report.
    # Row-level data, predictions and uploaded historical metrics stay out of this tool.
    saved = payload.get("saved_metrics")
    if (
        report.kind == "validation"
        and isinstance(saved, dict)
        and saved.get("source") == "fresh_backend_execution"
    ):
        fresh = saved.get("fresh_evaluation")
        if isinstance(fresh, dict):
            allowed = (
                "rows", "roc_auc", "ks", "gini",
                "pr_auc_average_precision_sklearn", "brier", "log_loss",
                "calibration_gap_observed_minus_expected",
            )
            metrics = {"source": "fresh_backend_execution"}
            for partition in ("validation", "test"):
                values = fresh.get(partition)
                if isinstance(values, dict):
                    metrics[partition] = {
                        key: value for key in allowed
                        if isinstance(value := values.get(key), (int, float))
                        and not isinstance(value, bool) and math.isfinite(value)
                    }
            view["fresh_metrics"] = metrics
    return view


def parity_view(record) -> dict:
    result = record.result or {}
    return {
        "id": record.id,
        "candidate_model_id": record.candidate_model_id,
        "candidate_version": record.candidate_version,
        "parent_model_id": record.parent_model_id,
        "parent_version": record.parent_version,
        "rebuild_run_id": record.rebuild_run_id,
        "initiator": record.initiator,
        "status": record.status,
        "passed": record.passed,
        "error": record.error,
        "scope": result.get("scope"),
        "n_rows": result.get("n_rows"),
        "n_above_tolerance": result.get("n_above_tolerance"),
        "max_absolute_difference": result.get("max_absolute_difference"),
        "tolerance_absolute": result.get("tolerance_absolute"),
        "limitation": result.get("limitation"),
        "created_at": record.created_at,
    }


def proposal_view(record, conversation_id) -> dict:
    return {
        "id": record.id,
        "type": record.type,
        "operation": (record.args or {}).get("operation"),
        "status": record.status,
        "required_role": record.required_role,
        "model_version": (record.preconditions or {}).get("model_version"),
        "result": record.result,
        "from_this_conversation": bool(conversation_id)
        and record.conversation_id == conversation_id,
        "created_at": record.created_at,
    }


async def model_status(session, context, model_id=None, run_id=None) -> dict:
    """The status of one model: the conversation's, or the one named by `model_id`."""
    from app.services.parity_service import ParityRunRecord
    from app.services.proposal_service import ProposalRecord, reconcile
    from app.services.upload_service import InventoryRecord

    looked_up = bool(model_id) and model_id != context.model_id
    target = model_id or context.model_id
    requested_run_id = run_id or (None if looked_up else context.run_id)
    scope = {
        "conversation_model_id": context.model_id,
        "conversation_run_id": context.run_id,
        "looked_up_model": looked_up,
        "note": "Proposals can be recorded only for the conversation's model."
        if looked_up
        else "Runs and reports listed are this model's, not only the conversation's run.",
    }
    data: dict = {"scope": scope, "reading_rules": READING_RULES}

    requested_run = await session.get(runningRecord, requested_run_id) if requested_run_id else None
    if requested_run_id and requested_run is None:
        data["requested_run"] = {"id": requested_run_id, "found": False}
    if requested_run is not None and not target:
        target = requested_run.model_id

    if not target:
        data["model_lookup"] = "not_selected"
        data["model"] = None
        data["note"] = (
            "No model is selected in this conversation. This says nothing about which models "
            "exist: use find_models to look one up by name."
        )
        return data
    model = await session.get(ModelRecord, target)
    if model is None or model.lifecycle_status == "deleted":
        data["model_lookup"] = "not_found"
        data["model"] = None
        return data
    data["model_lookup"] = "found"
    data["model"] = model_view(model)

    if requested_run is not None:
        if requested_run.model_id != model.id:
            data["requested_run"] = {"id": requested_run.id, "found": True, "belongs_to_model": False}
        else:
            data["requested_run"] = run_view(requested_run)

    runs = list(
        await session.scalars(
            select(runningRecord)
            .where(runningRecord.model_id == model.id)
            .order_by(runningRecord.created_at.desc())
            .limit(RUN_LIMIT)
        )
    )
    pending = (
        await session.execute(
            select(ReviewRecord, runningRecord)
            .join(runningRecord, ReviewRecord.run_id == runningRecord.id)
            .where(runningRecord.model_id == model.id, ReviewRecord.status == "pending")
            .order_by(ReviewRecord.created_at.desc())
        )
    ).all()
    links = await run_links(session, [*runs, *(run for _, run in pending)])
    data["latest_runs"] = [{**run_view(r), **links.get(r.id, {})} for r in runs]

    from app.services.review_service import review_read
    from app.services.run_service import gate_1_accepted

    inventory = await session.get(InventoryRecord, model.id)
    current_document = inventory.document_run_id if inventory else None
    data["gate_1"] = {
        "model_version": model.version,
        "current_document_run_id": current_document,
        "accepted": await gate_1_accepted(session, model, inventory),
    }
    data["pending_reviews"] = []
    for review, run in pending:
        # The review service's own reading, so chat and the review card always agree.
        card = await review_read(session, review)
        entry = {
            "review_id": review.id,
            "gate": GATES.get(run.operation),
            "run_id": run.id,
            "operation": run.operation,
            "model_version": run.model_version,
            "artifact_id": review.artifact_id,
            "artifact_version": review.artifact_version,
            "hard_blockers": card["hard_blockers"],
            "limitations": card["limitations"],
            "allowed_decisions": card["allowed_decisions"],
            "created_at": review.created_at,
            **links.get(run.id, {}),
        }
        if run.operation == "document":
            entry["superseded"] = bool(current_document) and run.id != current_document
        data["pending_reviews"].append(entry)

    proposals = list(
        await session.scalars(
            select(ProposalRecord)
            .where(ProposalRecord.model_id == model.id)
            .order_by(ProposalRecord.created_at.desc())
            .limit(PROPOSAL_LIMIT)
        )
    )
    for record in proposals:
        await reconcile(session, record)
    data["proposals"] = [proposal_view(p, context.conversation_id) for p in proposals]

    reports = list(
        await session.scalars(
            select(ReportRecord)
            .where(ReportRecord.model_id == model.id)
            .order_by(ReportRecord.created_at.desc())
            .limit(REPORT_LIMIT)
        )
    )
    data["reports"] = [report_view(r) for r in reports]

    parity = await session.scalar(
        select(ParityRunRecord)
        .where(
            or_(
                ParityRunRecord.candidate_model_id == model.id,
                ParityRunRecord.parent_model_id == model.id,
            )
        )
        .order_by(ParityRunRecord.created_at.desc())
        .limit(1)
    )
    data["latest_official_parity"] = parity_view(parity) if parity else None

    data["model_inputs"] = await model_inputs(session, inventory)
    reproduction = (inventory.provenance or {}).get("reproduction") if inventory else None
    data["upload_reproduction"] = (
        {
            key: reproduction.get(key)
            for key in (
                "status",
                "scope",
                "n_rows",
                "max_absolute_difference",
                "absolute_tolerance",
                "reason",
                "limitation",
            )
            if key in reproduction
        }
        if isinstance(reproduction, dict)
        else None
    )
    return data


async def model_inputs(session, inventory) -> dict | None:
    """The model's exact input names, and the dataset columns that are never inputs."""
    from app.services.upload_service import DatasetRecord

    if inventory is None:
        return None
    manifest = inventory.manifest or {}
    dataset = (
        await session.get(DatasetRecord, inventory.dataset_version_id)
        if inventory.dataset_version_id
        else None
    )
    descriptor = (dataset.descriptor or {}) if dataset else {}
    return {
        "feature_order": manifest.get("feature_order"),
        "target_column": descriptor.get("target_column"),
        "audit_only_columns": descriptor.get("audit_only_columns", []),
        "drift_factor_bounds": "greater than 0 and at most 100",
    }


async def find_models(session, query: str) -> dict:
    """Models whose name contains `query`, ignoring case, newest version first."""
    text = query.strip()
    rows = list(
        await session.scalars(
            select(ModelRecord)
            .where(
                func.lower(ModelRecord.name).contains(text.lower()),
                ModelRecord.lifecycle_status != "deleted",
            )
            .order_by(ModelRecord.name, ModelRecord.created_at.desc())
            .limit(FIND_LIMIT + 1)
        )
    )
    exact = [m for m in rows if m.name.lower() == text.lower()]
    matches = exact or rows[:FIND_LIMIT]
    return {
        "query": text,
        "result": "not_found"
        if not matches
        else "one_match"
        if len(matches) == 1
        else "several_matches",
        "truncated": not exact and len(rows) > FIND_LIMIT,
        "models": [
            {k: v for k, v in model_view(m).items() if k != "purpose"} for m in matches
        ],
        "note": "Pass a model's id to get_model_status for its full status. Proposals can be "
        "recorded only for the model selected in this conversation.",
    }
