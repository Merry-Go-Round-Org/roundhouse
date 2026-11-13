"""Monitoring batches and declared simulations; every calculation is a persisted run."""

import asyncio
import hashlib
import shutil
from uuid import uuid4
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, ConfigDict, Field
from app.config import settings
from app.db.session import get_session
from app.db.tables import (
    ModelRecord,
    MonitoringRecord,
    PackageRecord,
    runningRecord,
    RunExecutionRecord,
)
from app.services.identity import Actor, actor
from app.services.monitoring_service import MonitorJob

router = APIRouter(tags=["Monitoring"])
Session = Annotated[AsyncSession, Depends(get_session)]
Who = Annotated[Actor, Depends(actor)]


@router.get("/models/{model_id}/monitoring")
async def get_monitoring(
    model_id: str,
    session: Session,
    who: Who,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    if await session.get(ModelRecord, model_id) is None:
        raise HTTPException(404, detail="Model not found")
    return list(
        await session.scalars(
            select(MonitoringRecord)
            .where(MonitoringRecord.model_id == model_id)
            .order_by(MonitoringRecord.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    )


@router.get("/monitoring")
async def portfolio_monitoring(
    session: Session,
    who: Who,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    models = await session.scalars(
        select(ModelRecord)
        .where(ModelRecord.lifecycle_status != "deleted")
        .order_by(ModelRecord.id)
        .offset(offset)
        .limit(limit)
    )
    result = []
    for model in models:
        event = await session.scalar(
            select(MonitoringRecord)
            .where(MonitoringRecord.model_id == model.id)
            .order_by(MonitoringRecord.created_at.desc())
        )
        result.append(
            {
                "model_id": model.id,
                "model_name": model.name,
                "lifecycle_status": model.lifecycle_status,
                "latest": event.payload if event else None,
                "last_run": event.created_at if event else None,
            }
        )
    return result


class SimulationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feature: str = Field(min_length=1, max_length=100)
    factor: float = Field(gt=0, le=100, allow_inf_nan=False)
    window_label: str = Field(
        default="Declared validation-partition drift simulation", min_length=1, max_length=120
    )


async def check_model(session, identity):
    from app.services.upload_service import InventoryRecord

    model = await session.get(ModelRecord, identity)
    package = await session.scalar(
        select(PackageRecord).where(PackageRecord.model_id == identity)
    )
    inventory = await session.get(InventoryRecord, identity)
    if model is None or model.lifecycle_status == "deleted":
        raise HTTPException(404, detail="Model not found")
    if not inventory or not package:
        raise HTTPException(
            409,
            detail="Monitoring requires an uploaded model and verified dataset version",
        )
    if inventory.manifest.get("task") != "binary_classification":
        raise HTTPException(
            409,
            detail={
                "code": "not_applicable",
                "message": "PSI monitoring is not applicable to this model task",
            },
        )
    return model, inventory


async def dispatch(session, model, who, payload, identity=None):
    from app.services.run_service import publish_run

    identity = identity or str(uuid4())
    run = runningRecord(
        id=identity,
        model_id=model.id,
        model_version=model.version,
        dataset_id=model.dataset_id,
        operation="monitor",
        status="queued",
    )
    session.add(run)
    await session.flush()
    session.add(RunExecutionRecord(run_id=identity, actor_id=who.id))
    session.add(MonitorJob(run_id=identity, payload=payload))
    await session.commit()
    try:
        await asyncio.to_thread(publish_run, identity)
    except Exception:
        raise HTTPException(
            503,
            detail={
                "code": "dispatch_unavailable",
                "run_id": identity,
                "message": "Monitoring job saved; recovery will retry",
            },
        ) from None
    await session.refresh(run)
    return run


@router.post("/models/{model_id}/monitoring/simulate", status_code=202)
async def simulate(
    model_id: str, payload: SimulationRequest, session: Session, who: Who
):
    from app.services.run_service import authorize_operation

    await authorize_operation(session, model_id, "monitor", who)
    model, inventory = await check_model(session, model_id)
    if payload.feature not in inventory.manifest["feature_order"]:
        raise HTTPException(422, detail="Shift feature must be a declared model input")
    return await dispatch(
        session,
        model,
        who,
        {
            "mode": "simulated",
            "window_label": payload.window_label,
            "shift": {"feature": payload.feature, "factor": payload.factor},
        },
    )


@router.post("/models/{model_id}/monitoring/batches", status_code=202)
async def upload_batch(
    model_id: str,
    session: Session,
    who: Who,
    batch: UploadFile = File(...),
    window_label: str = Form(...),
):
    from app.services.run_service import authorize_operation

    await authorize_operation(session, model_id, "monitor", who)
    model, inventory = await check_model(session, model_id)
    if not window_label.strip() or len(window_label) > 120:
        raise HTTPException(
            422, detail="Window label is required and must be at most 120 characters"
        )
    identity = str(uuid4())
    root = settings.storage_path / "monitoring" / identity
    root.mkdir(parents=True)
    target = root / "uploaded.csv"
    size = 0
    hash = hashlib.sha256()
    try:
        with target.open("wb") as stream:
            while block := await batch.read(1024 * 1024):
                size += len(block)
                if size > 20_000_000:
                    raise HTTPException(
                        413, detail="Monitoring batch limit is 20 MB and 20000 rows"
                    )
                stream.write(block)
                hash.update(block)
        from app.services.package_service import read_inputs, PackageError

        try:
            ids = await asyncio.to_thread(
                read_inputs, target, inventory.manifest["feature_order"], 20000
            )
            if len(ids) > 20000:
                raise PackageError("Monitoring batch exceeds 20000 rows")
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, detail=str(exc)) from None
        return await dispatch(
            session,
            model,
            who,
            {
                "mode": "uploaded",
                "window_label": window_label,
                "input_sha256": hash.hexdigest(),
            },
            identity,
        )
    except Exception:
        # Keep persisted jobs' input even when publication is ambiguous.
        if await session.get(MonitorJob, identity) is None:
            shutil.rmtree(root, ignore_errors=True)
        raise
    finally:
        await batch.close()


@router.post("/models/{model_id}/monitoring", status_code=410)
async def retired_aggregate_input(model_id: str, who: Who):
    raise HTTPException(
        410,
        detail={
            "code": "aggregate_monitoring_replaced",
            "message": "Use /monitoring/batches or /monitoring/simulate. Monitoring now uses the shared PSI engine with a verified development baseline.",
        },
    )
