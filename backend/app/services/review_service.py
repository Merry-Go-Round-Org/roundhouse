"""Human-only version-bound decisions. Decisions never rewrite the evidence."""

from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import update, select
from app.db.tables import (
    ReviewRecord,
    ArtifactRecord,
    runningRecord,
    RunExecutionRecord,
    AuditRecord,
    RunResultRecord,
    ModelRecord,
    ReportRecord,
)
from app.services.artifact_service import artifact_bytes


async def decide(session, review_id, payload, actor):
    review = await session.get(ReviewRecord, review_id)
    if review is None:
        raise HTTPException(404, detail="Review not found")
    if review.status != "pending":
        raise HTTPException(409, detail="Review already decided")
    if (
        actor.role not in ("reviewer", "validator", "approver", "admin")
        or actor.id == review.requested_by
    ):
        raise HTTPException(403, detail="Independent human reviewer required")
    if (payload.artifact_id, payload.artifact_version) != (
        review.artifact_id,
        review.artifact_version,
    ):
        raise HTTPException(409, detail="Review targets a different artifact version")
    artifact = await session.get(ArtifactRecord, review.artifact_id)
    await artifact_bytes(artifact)
    run = await session.get(runningRecord, review.run_id)
    if run.operation == "validate" and actor.role not in ("approver", "reviewer", "admin"):
        raise HTTPException(403, detail="Validation sign-off requires an approver or admin")
    if (
        run.operation == "rebuild"
        and payload.decision != "changes_requested"
        and await parity_blocker(session, review.artifact_id)
    ):
        raise HTTPException(
            409,
            detail={
                "code": "official_parity_not_passed",
                "message": "Gate 2 acceptance requires a passing official parity run on this exact candidate.",
            },
        )
    if run.status != "awaiting_review":
        raise HTTPException(409, detail="Run is not awaiting review")
    result = await session.get(RunResultRecord, run.id)
    if result is None or result.payload.get("checkpoint_id") != review.id:
        raise HTTPException(409, detail="Review is not the current checkpoint")
    hard_blockers, limitations = await review_checks(session, review, run)
    if payload.decision == "accepted" and (hard_blockers or limitations):
        raise HTTPException(409, detail={
            "code": "required_checks_blocked", "hard_blockers": hard_blockers,
            "limitations": limitations,
        })
    if payload.decision == "accepted_with_conditions":
        if hard_blockers:
            raise HTTPException(409, detail={
                "code": "required_checks_failed", "hard_blockers": hard_blockers,
            })
        if run.operation == "validate" and (
            len(payload.conditions) != len(limitations)
            or any(sum(check_id in condition for condition in payload.conditions) != 1
                   for check_id in limitations)
            or any(sum(check_id in condition for check_id in limitations) != 1
                   for condition in payload.conditions)
        ):
            raise HTTPException(422, detail={
                "code": "limitation_acknowledgements_required", "limitations": limitations,
                "message": "Provide one condition naming each limitation check_id",
            })
    if any(not c.strip() for c in payload.conditions):
        raise HTTPException(422, detail="Conditions must not be blank")
    if payload.decision == "accepted_with_conditions" and not payload.conditions:
        raise HTTPException(422, detail="Conditions are required")
    if not payload.rationale.strip():
        raise HTTPException(422, detail="Rationale must not be blank")
    changed = await session.execute(
        update(ReviewRecord)
        .where(ReviewRecord.id == review_id, ReviewRecord.status == "pending")
        .values(
            status="decided",
            reviewer_id=actor.id,
            decision=payload.decision,
            rationale=payload.rationale,
            conditions=payload.conditions,
            decided_at=datetime.now(timezone.utc),
        )
    )
    if changed.rowcount != 1:
        raise HTTPException(409, detail="Review already decided")
    session.add(
        AuditRecord(
            actor_id=actor.id,
            resource_id=review_id,
            action="human_review_decision",
            payload=payload.model_dump(),
        )
    )
    control = await session.get(RunExecutionRecord, run.id)
    # Validation reports end at this gate. Agent workflows resume through a new,
    # explicitly requested continuation using this recorded decision.
    from harness.db import AgentTask

    task = (
        await session.get(AgentTask, control.task_id)
        if control and control.task_id
        else None
    )
    if (
        not task
        or task.status == "completed"
        or (run.operation == "document" and payload.decision == "changes_requested")
    ):
        run.status = (
            "failed" if payload.decision == "changes_requested" else "completed"
        )
        run.stage = (
            "changes_requested"
            if payload.decision == "changes_requested"
            else payload.decision
        )
        run.finished_at = datetime.now(timezone.utc)
    continue_rebuild = False
    if task and task.status == "awaiting_review" and run.operation in ("document", "rebuild"):
        if payload.decision in ("accepted", "accepted_with_conditions"):
            # Nothing is left for the agent once its artifact is accepted, so the run and
            # its paused task finish here instead of waiting for a continuation request
            # that chat cannot make (Flow C retest, issue #32).
            now = datetime.now(timezone.utc)
            run.status, run.stage, run.finished_at = "completed", payload.decision, now
            task.status, task.finished_at = "completed", now
            task.result_text = (
                f"Gate {1 if run.operation == 'document' else 2} {payload.decision.replace('_', ' ')} "
                f"by an independent reviewer. The exported artifact is final."
            )
        elif payload.decision == "changes_requested" and run.operation == "rebuild":
            # The rebuild continues with the reviewer's comments, as for a document.
            from app.services.run_service import queue_continuation

            continue_rebuild = await queue_continuation(session, run, control, review, actor.id)
    model = await session.get(ModelRecord, run.model_id)
    if payload.decision in ("accepted", "accepted_with_conditions"):
        model.lifecycle_status = {
            "document": "documented",
            "rebuild": "rebuilt",
            "validate": "approved",
        }.get(run.operation, model.lifecycle_status)
    elif run.operation == "validate":
        model.lifecycle_status = "revalidation_due"
    await session.commit()
    await session.refresh(review)
    if continue_rebuild:
        import asyncio
        from app.services.run_service import publish_run

        try:
            await asyncio.to_thread(publish_run, run.id)
        except Exception:
            pass  # the queued continuation is saved and recovered by the scheduler
    if run.operation == "document" and payload.decision == "changes_requested":
        from app.schemas.runs import RunCreate
        from app.services.run_service import createRun, publish_run
        from app.config import settings
        import asyncio

        next_run = await createRun(
            session,
            run.model_id,
            RunCreate(
                operation="document",
                model_version=run.model_version,
                dataset_id=run.dataset_id or None,
            ),
        )
        session.add(
            RunExecutionRecord(run_id=next_run.id, actor_id=review.requested_by)
        )
        session.add(
            AuditRecord(
                actor_id=actor.id,
                resource_id=next_run.id,
                action="document_revision_requested",
                payload={"review_id": review.id, "rationale": payload.rationale},
            )
        )
        if (
            not settings.openrouter_api_key
            or not settings.openrouter_api_key.get_secret_value()
        ):
            next_run.status = "failed"
            next_run.stage = "configuration_required"
            next_run.error = "Configure OPENROUTER_API_KEY then regenerate"
            next_run.finished_at = datetime.now(timezone.utc)
        await session.commit()
        if next_run.status == "queued":
            try:
                await asyncio.to_thread(publish_run, next_run.id)
            except Exception:
                pass  # persisted queued work is recovered by the scheduler
    return review


def stored_blockers(review):
    # Checkpoints saved before official parity existed carry this placeholder; Gate 2
    # now checks parity itself when the decision is made.
    return [b for b in review.blockers or [] if b != "official_parity_pending"]


async def review_checks(session, review, run):
    """Classify stored evidence, including checkpoints created before the rule change."""
    blockers = stored_blockers(review)
    if run.operation != "validate":
        return blockers, []
    report = await session.scalar(
        select(ReportRecord).where(ReportRecord.run_id == run.id)
        .order_by(ReportRecord.created_at.desc())
    )
    if report is None or not report.payload.get("checks"):
        return blockers, []
    hard = [c["check_id"] for c in report.payload["checks"] if c["status"] == "FAIL"]
    limitations = [c["check_id"] for c in report.payload["checks"]
                   if c.get("required") and c["status"] in ("REVIEW", "NOT_ASSESSABLE")]
    unclassified = [item for item in blockers if item not in hard and item not in limitations]
    return list(dict.fromkeys([*hard, *unclassified])), list(dict.fromkeys(limitations))


async def parity_blocker(session, artifact_id, candidate=None):
    """Gate 2 blocker for a rebuild checkpoint, or None once parity has passed."""
    from app.services.parity_service import candidate_for_artifact, passed_parity

    candidate = candidate or await candidate_for_artifact(session, artifact_id)
    from app.services.upload_service import InventoryRecord
    inventory = await session.get(InventoryRecord, candidate) if candidate else None
    if inventory and inventory.manifest.get("derived_from", {}).get("kind") == "recalibration":
        approved = await session.scalar(select(ReviewRecord.id).join(runningRecord, ReviewRecord.run_id == runningRecord.id).where(
            runningRecord.model_id == candidate, runningRecord.model_version == inventory.model_version,
            runningRecord.operation == "validate", ReviewRecord.status == "decided",
            ReviewRecord.decision.in_(("accepted", "accepted_with_conditions"))))
        return None if approved else "recalibration_validation_required"
    if await passed_parity(session, candidate):
        return None
    return "official_parity_not_passed"


def utc(value):
    return (
        value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value
    )


async def review_read(session, review):
    run = await session.get(runningRecord, review.run_id)
    model = await session.get(ModelRecord, run.model_id)
    resolution = None
    if review.status == "decided":
        opened = await session.scalar(
            select(AuditRecord)
            .where(
                AuditRecord.resource_id == review.id,
                AuditRecord.actor_id == review.reviewer_id,
                AuditRecord.action == "review_opened",
            )
            .order_by(AuditRecord.created_at)
        )
        elapsed = (
            max(0, (utc(review.decided_at) - utc(opened.created_at)).total_seconds())
            if opened
            else None
        )
        resolution = dict(
            artifact_id=review.artifact_id,
            artifact_version=review.artifact_version,
            decision=review.decision,
            rationale=review.rationale,
            conditions=review.conditions,
            reviewer_id=review.reviewer_id,
            decided_at=utc(review.decided_at),
            review_elapsed_seconds=elapsed,
            timing_limitation="Elapsed from explicit review open; not measured active effort",
        )
    hard_blockers, limitations = await review_checks(session, review, run)
    blockers = [*hard_blockers, *limitations]
    candidate = None
    input_contract = None
    if run.operation == "rebuild":
        from app.services.input_contract import gate_2_evidence
        from app.services.parity_service import candidate_for_artifact

        candidate = await candidate_for_artifact(session, review.artifact_id)
        # Saved rebuild probe result for display only; it does not change Gate 2 eligibility.
        input_contract = await gate_2_evidence(session, candidate)
        blocker = await parity_blocker(session, review.artifact_id, candidate)
        if blocker and blocker not in blockers:
            blockers.append(blocker)
            hard_blockers.append(blocker)
    return dict(
        id=review.id,
        model_id=model.id,
        model_name=model.name,
        model_version=run.model_version,
        run_id=run.id,
        gate={"document": 1, "rebuild": 2, "validate": 3}.get(run.operation, 3),
        status=review.status,
        artifact_id=review.artifact_id,
        artifact_version=review.artifact_version,
        created_at=utc(review.created_at),
        resolution=resolution,
        blockers=blockers,
        approval_available=not blockers,
        hard_blockers=hard_blockers,
        limitations=limitations,
        allowed_decisions=(
            ["changes_requested"] if hard_blockers else
            ["changes_requested", "accepted_with_conditions"] if limitations else
            ["changes_requested", "accepted", "accepted_with_conditions"]
        ),
        candidate_model_id=candidate,
        input_contract=input_contract,
        requested_by=review.requested_by,
    )
