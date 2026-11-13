from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.tables import ModelRecord, ReviewRecord, runningRecord
from app.schemas.runs import RunCreate


class modelNotFoundError(Exception):
    ## if the model doesn't exist
    pass


class runInputError(Exception):
    ## if the request doesn't match the registered model
    pass


async def gate_1_accepted(session, model, inventory) -> bool:
    """Whether this exact model version's current document passed Gate 1."""
    if inventory is None or not inventory.document_run_id:
        return False
    # A new document run replaces the draft awaiting Gate 1 approval.
    # Check its decision rather than whichever review row SQLite returns first.
    accepted = await session.scalar(
        select(ReviewRecord.id)
        .join(runningRecord, ReviewRecord.run_id == runningRecord.id)
        .where(
            ReviewRecord.run_id == inventory.document_run_id,
            ReviewRecord.status == "decided",
            ReviewRecord.decision.in_(("accepted", "accepted_with_conditions")),
            runningRecord.model_id == model.id,
            runningRecord.model_version == model.version,
            runningRecord.operation == "document",
        )
        .limit(1)
    )
    return accepted is not None


async def createRun(
    session: AsyncSession,
    model_id: str,
    payload: RunCreate,
) -> runningRecord:
    model = await session.get(ModelRecord, model_id)
    if model is None or model.lifecycle_status == "deleted":
        raise modelNotFoundError("Model not Found.")

    if payload.model_version != model.version:
        raise runInputError("Requested version didn't matcht the registered model")

    if (payload.dataset_id or "") != model.dataset_id:
        raise runInputError("requested dataset doesn't match the registered model")

    record = runningRecord(
        model_id=model.id,
        model_version=model.version,
        dataset_id=model.dataset_id,
        operation=payload.operation,
        status="queued",
        stage=None,
        error=None,
    )

    session.add(record)

    # send INSERT
    await session.flush()
    if payload.operation == "document":
        from app.services.upload_service import InventoryRecord

        inventory = await session.get(InventoryRecord, model_id)
        if inventory:
            inventory.document_run_id = record.id
    # print(record)  ## lookout for print statement
    return record


async def getRun(
    session: AsyncSession,
    run_id: str,
) -> runningRecord | None:
    return await session.get(runningRecord, run_id)


async def listRuns(
    session: AsyncSession,
    *,
    model_id: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> list[runningRecord]:
    if offset < 0:
        raise runInputError("Offset must be zeroor greater")
    if not 1 <= limit <= 100:
        raise runInputError("Limit must be between 1 and 100")

    statement = select(runningRecord)

    if model_id is not None:
        statement = statement.where(runningRecord.model_id == model_id)

    statement = (
        statement.order_by(
            runningRecord.created_at.desc(),
            runningRecord.id.desc(),
        )
        .offset(offset)
        .limit(limit)
    )

    result = await session.scalars(statement)

    # print(result)  ## lookout for print statement
    return list(result)


class DispatchUnavailable(Exception):
    def __init__(self, run_id):
        self.run_id = run_id


def publish_run(run_id: str):
    from app.workers.tasks import execute_run

    execute_run.apply_async(args=[run_id], task_id=run_id, retry=False)


async def submit_run(
    session: AsyncSession, model_id: str, payload: RunCreate, actor_id="local-operator"
):
    import asyncio
    from datetime import datetime, timezone
    from sqlalchemy import update

    record = await createRun(session, model_id, payload)
    run_id = record.id
    from app.db.tables import RunExecutionRecord

    session.add(RunExecutionRecord(run_id=run_id, actor_id=actor_id))
    # Commit before publishing so even a very fast worker can find the record.
    await session.commit()
    try:
        await asyncio.to_thread(publish_run, run_id)
    except Exception:
        # A publish error can be ambiguous: never overwrite a worker's claim.
        changed = await session.execute(
            update(runningRecord)
            .where(runningRecord.id == run_id, runningRecord.status == "queued")
            .values(
                status="failed",
                stage="dispatch_failed",
                finished_at=datetime.now(timezone.utc),
                error="Could not dispatch execution. Check the queue service.",
            )
        )
        await session.commit()
        if changed.rowcount:
            raise DispatchUnavailable(run_id) from None
    await session.refresh(record)
    return record


async def authorize_operation(session, model_id, operation, who):
    from fastapi import HTTPException
    from app.config import settings
    from app.services.upload_service import InventoryRecord

    model = await session.get(ModelRecord, model_id)
    if model is None or model.lifecycle_status == "deleted":
        raise HTTPException(404, detail="Model not found")
    roles = {
        "document": {"developer", "admin"},
        "rebuild": {"developer", "admin"},
        "validate": {"validator", "admin"},
        "monitor": {"validator", "developer", "admin"},
    }
    if who.role not in roles.get(operation, set()) and not (
        settings.allow_local_demo_auth and who.role in ("operator", "reviewer")
    ):
        raise HTTPException(403, detail="Role is not permitted to start this operation")
    inventory = await session.get(InventoryRecord, model_id)
    if not inventory:
        return
    if (
        inventory.manifest.get("task") != "binary_classification"
        and operation != "document"
    ):
        raise HTTPException(
            409,
            detail={
                "code": "not_applicable",
                "message": "Synthetic survival demo supports documentation only",
            },
        )
    if operation == "rebuild" and model.language != "R":
        raise HTTPException(409, detail="Rebuild requires an R model")
    if operation in ("rebuild", "validate"):
        if not await gate_1_accepted(session, model, inventory):
            raise HTTPException(
                409,
                detail={
                    "code": "gate_1_required",
                    "message": "Accept a generated document before this operation",
                },
            )
    if operation == "monitor":
        current_validation_run_id = await session.scalar(
            select(runningRecord.id)
            .where(
                runningRecord.model_id == model_id,
                runningRecord.model_version == model.version,
                runningRecord.operation == "validate",
            )
            .order_by(runningRecord.created_at.desc(), runningRecord.id.desc())
            .limit(1)
        )
        accepted = await session.scalar(
            select(ReviewRecord.id)
            .where(
                ReviewRecord.run_id == current_validation_run_id,
                ReviewRecord.status == "decided",
                ReviewRecord.decision.in_(("accepted", "accepted_with_conditions")),
            )
            .limit(1)
        )
        if not accepted:
            raise HTTPException(
                409,
                detail={
                    "code": "gate_3_required",
                    "message": "Validation sign-off required before monitoring",
                },
            )


async def queue_continuation(session, run, control, review, actor_id) -> bool:
    """Queue an agent run paused at `review`'s checkpoint to continue with its decision.

    Returns False if the run is no longer waiting. The caller commits, then publishes the
    run; a published failure is recovered by the scheduler from the saved queued state.
    """
    import json
    from sqlalchemy import update
    from app.db.tables import AuditRecord

    changed = await session.execute(
        update(runningRecord)
        .where(runningRecord.id == run.id, runningRecord.status == "awaiting_review")
        .values(
            status="queued",
            stage="resuming",
            error=None,
            finished_at=None,
            created_at=run.created_at,
        )
    )
    if changed.rowcount != 1:
        return False
    control.parent_task_id = control.task_id
    control.resolution = {
        "pending_id": review.id,
        "outcome": "rejected" if review.decision == "changes_requested" else "approved",
        "detail": review.rationale + " Conditions: " + json.dumps(review.conditions),
    }
    control.cancel_requested = False
    session.add(
        AuditRecord(
            actor_id=actor_id,
            resource_id=run.id,
            action="resume_requested",
            payload={"review_id": review.id},
        )
    )
    return True
