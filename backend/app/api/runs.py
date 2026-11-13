import json
from datetime import datetime, timezone
from sqlalchemy import select, update
from pydantic import BaseModel
from app.db.tables import (
    runningRecord,
    RunExecutionRecord,
    ReviewRecord,
    RunResultRecord,
    AuditRecord,
)
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_session
from app.schemas.runs import RunCreate, RunRead
from app.services import model_service, run_service
from app.services.identity import Actor, actor

router = APIRouter(tags=["Runs"])

Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/runs/{run_id}/analysis")
async def get_analysis(run_id: str, session: Session):
    from app.services.native_analysis import AnalysisRecord
    record = await session.get(AnalysisRecord, run_id)
    if not record:
        raise HTTPException(404, "AI analysis not found")
    return {"run_id": run_id, "kind": record.kind, "inputs": record.inputs, "result": record.result}


@router.post(
    "/models/{model_id}/runs",
    response_model=RunRead,
    status_code=202,
    responses={
        404: {"description": "Model not found"},
        422: {"description": "Invalid run request"},
        503: {"description": "Execution is unavailable"},
    },
)
async def start_run(
    model_id: str,
    payload: RunCreate,
    session: Session,
    who: Annotated[Actor, Depends(actor)],
):
    if payload.operation == "monitor":
        raise HTTPException(
            409,
            detail={
                "code": "monitoring_input_required",
                "message": "Use /models/{id}/monitoring/batches or /monitoring/simulate with explicit input",
            },
        )
    await run_service.authorize_operation(session, model_id, payload.operation, who)
    try:
        return await run_service.submit_run(session, model_id, payload, actor_id=who.id)
    except run_service.modelNotFoundError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc
    except run_service.runInputError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except run_service.DispatchUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "dispatch_unavailable",
                "run_id": exc.run_id,
                "message": "Dispatch failed. The failed run is saved in history; inspect it before retrying.",
            },
        ) from exc


@router.get("/runs", response_model=list[RunRead])
async def list_runs(
    session: Session,
    model_id: str | None = Query(default=None, min_length=1),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
):

    if model_id is not None:
        model = await model_service.get_model(session, model_id)

        if model is None:
            raise HTTPException(
                status_code=404,
                detail="Model not found",
            )
    return await run_service.listRuns(
        session,
        model_id=model_id,
        offset=offset,
        limit=limit,
    )


@router.get("/runs/{run_id}", response_model=RunRead)
async def get_run(
    run_id: str,
    session: Session,
):
    record = await run_service.getRun(session, run_id)

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Run not Found",
        )

    return record


class ResumeRequest(BaseModel):
    review_id: str


@router.get("/runs/{run_id}/result")
async def run_result(run_id: str, session: Session):
    if await session.get(runningRecord, run_id) is None:
        raise HTTPException(404, detail="Run not found")
    result = await session.get(RunResultRecord, run_id)
    return {"run_id": run_id, "result": result.payload if result else None}


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str, session: Session, limit: int = Query(100, ge=1, le=500)
):
    if await session.get(runningRecord, run_id) is None:
        raise HTTPException(404, detail="Run not found")
    control = await session.get(RunExecutionRecord, run_id)
    ids = [run_id] + ([control.task_id] if control and control.task_id else [])
    records = list(
        await session.scalars(
            select(AuditRecord)
            .where(AuditRecord.resource_id.in_(ids))
            .order_by(AuditRecord.created_at.desc())
            .limit(limit)
        )
    )
    from app.services.public_events import public_event

    return [public_event(r) for r in records]


@router.post("/runs/{run_id}/cancel", response_model=RunRead)
async def cancel_run(
    run_id: str, session: Session, who: Annotated[Actor, Depends(actor)]
):
    run = await session.get(runningRecord, run_id)
    if run is None:
        raise HTTPException(404, detail="Run not found")
    control = await session.get(RunExecutionRecord, run_id)
    if control and control.actor_id != who.id:
        raise HTTPException(403, detail="Only the initiating operator can cancel")
    if run.status not in ("queued", "running"):
        raise HTTPException(409, detail="Only queued/running work can be cancelled")
    if not control:
        control = RunExecutionRecord(run_id=run_id, actor_id=who.id)
        session.add(control)
    control.cancel_requested = True
    # CAS prevents late worker completion from replacing the cancelled result.
    await session.execute(
        update(runningRecord)
        .where(
            runningRecord.id == run_id, runningRecord.status.in_(["queued", "running"])
        )
        .values(
            status="failed",
            stage="cancelled",
            error="Cancelled by operator",
            finished_at=datetime.now(timezone.utc),
        )
    )
    session.add(
        AuditRecord(
            actor_id=who.id, resource_id=run_id, action="cancel_requested", payload={}
        )
    )
    await session.commit()
    await session.refresh(run)
    return run


@router.post("/runs/{run_id}/resume", response_model=RunRead, status_code=202)
async def resume_run(
    run_id: str,
    payload: ResumeRequest,
    session: Session,
    who: Annotated[Actor, Depends(actor)],
):
    import asyncio

    run = await session.get(runningRecord, run_id)
    control = await session.get(RunExecutionRecord, run_id)
    review = await session.get(ReviewRecord, payload.review_id)
    if run is None:
        raise HTTPException(404, detail="Run not found")
    if not control or control.actor_id != who.id:
        raise HTTPException(403, detail="Initiating operator required")
    if not control.task_id or run.status != "awaiting_review":
        raise HTTPException(409, detail="No resumable agent checkpoint")
    result = await session.get(RunResultRecord, run_id)
    if not result or result.payload.get("checkpoint_id") != payload.review_id:
        raise HTTPException(409, detail="Review is not the current checkpoint")
    if not review or review.run_id != run_id or review.status != "decided":
        raise HTTPException(409, detail="A recorded human decision is required")
    if not await run_service.queue_continuation(session, run, control, review, who.id):
        raise HTTPException(409, detail="Run already resumed")
    await session.commit()
    try:
        await asyncio.to_thread(run_service.publish_run, run_id)
    except Exception:
        # Persisted queued continuation is recovered by beat; do not lose the decision.
        raise HTTPException(
            503,
            detail={
                "code": "dispatch_unavailable",
                "run_id": run_id,
                "message": "Continuation saved; recovery will retry.",
            },
        ) from None
    await session.refresh(run)
    return run
