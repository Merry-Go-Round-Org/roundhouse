"""Upload-derived model inventory and versioned provenance."""

from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_session
from app.schemas.models import ModelCreate, ModelRead
from app.services import model_service
from app.services import model_deletion
from app.services.identity import Actor, actor
import app.services.parity_service  # noqa: F401  registers the parity_runs table

router = APIRouter(prefix="/models", tags=["Models"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post("/deletions/preview")
async def preview_model_deletion(
    payload: model_deletion.Selection, session: Session,
    who: Annotated[Actor, Depends(actor)],
):
    """Preview an exact selection and its deletion blockers."""
    return await model_deletion.preview(session, payload.model_ids, who)


@router.post("/deletions")
async def archive_models(
    payload: model_deletion.DeleteRequest, session: Session,
    who: Annotated[Actor, Depends(actor)],
):
    """Atomically delete eligible model records and managed payloads."""
    return await model_deletion.remove(session, payload, who)


@router.get("/deletions/{request_id}")
async def model_deletion_receipt(
    request_id: str, session: Session, who: Annotated[Actor, Depends(actor)],
):
    return await model_deletion.receipt(session, request_id, who)


@router.post("", response_model=ModelRead, status_code=201)
async def create_model(payload: ModelCreate, session: Session):
    from app.config import settings

    if not settings.allow_legacy_registration:
        raise HTTPException(
            410,
            detail="Use POST /models/uploads; inventory comes from uploaded manifests",
        )
    return await model_service.create_model(session, payload)


@router.get("", response_model=list[ModelRead])
async def list_models(
    session: Session, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    return [
        await enrich(session, m)
        for m in await model_service.list_models(session, offset, limit)
    ]


@router.get("/{model_id}", response_model=ModelRead)
async def get_model(model_id: str, session: Session):
    record = await model_service.get_model(session, model_id)
    if record is None:
        raise HTTPException(404, detail="Model not found")
    return await enrich(session, record)


async def enrich(session, record):
    from app.services.upload_service import InventoryRecord
    from app.db.tables import (
        ReportRecord,
        ReviewRecord,
        runningRecord,
        FindingRecord,
        MonitoringRecord,
    )
    from sqlalchemy import select, func

    details = await session.get(InventoryRecord, record.id)
    out = ModelRead.model_validate(record).model_dump()
    out["dataset_id"] = record.dataset_id or None
    # Keep existing React readers working for binary models; the full workflow
    # stage is carried in generated inventory rather than changing this old enum.
    out["lifecycle_status"] = (
        "retired"
        if record.lifecycle_status == "retired"
        else "active"
        if record.lifecycle_status in ("active", "approved", "monitored")
        else "development"
    )
    if not details:
        out["inventory"] = {"lifecycle_stage": record.lifecycle_status}
        return out
    manifest = details.manifest
    provenance = {
        key: {
            "value": manifest.get(key),
            "source": "upload" if key in manifest else "not_provided",
        }
        for key in (
            "purpose",
            "intended_use",
            "model_owner",
            "risk_tier",
            "tier_rationale",
            "limitations",
            "feature_order",
            "algorithm",
            "task",
            "validation_frequency",
        )
    }
    reports = list(
        await session.scalars(
            select(ReportRecord)
            .where(ReportRecord.model_id == record.id)
            .order_by(ReportRecord.created_at.desc())
        )
    )
    reviews = list(
        await session.scalars(
            select(ReviewRecord)
            .join(runningRecord, ReviewRecord.run_id == runningRecord.id)
            .where(runningRecord.model_id == record.id)
            .order_by(ReviewRecord.created_at.desc())
        )
    )
    validations = [r for r in reports if r.kind == "validation"]
    monitoring = await session.scalar(
        select(MonitoringRecord)
        .where(MonitoringRecord.model_id == record.id)
        .order_by(MonitoringRecord.created_at.desc())
    )
    out["inventory"] = {
        "lifecycle_stage": "uploaded"
        if record.lifecycle_status == "development"
        else record.lifecycle_status,
        "manifest": manifest,
        "provenance": provenance,
        "upload_id": details.upload_id,
        "dataset_version_id": details.dataset_version_id,
        "bundle_sha256": details.bundle_sha256,
        "document_run_id": details.document_run_id,
        "synthetic_demo": bool(manifest.get("synthetic_demo")),
        "applicability": {
            o: (
                "applicable"
                if o == "document" or manifest.get("task") == "binary_classification"
                else "NOT_APPLICABLE"
            )
            for o in ("document", "rebuild", "validate", "monitor")
        },
        "reports": [
            {
                "id": r.id,
                "kind": r.kind,
                "artifact_id": r.artifact_id,
                "created_at": r.created_at,
            }
            for r in reports
        ],
        "last_validation_date": validations[0].created_at if validations else None,
        "validation_frequency": manifest.get("validation_frequency"),
        "next_validation_due": None,
        "validation_schedule_source": "not_provided"
        if not manifest.get("validation_frequency")
        else "upload",
        "validation_status": validations[0].payload.get("summary")
        if validations
        else None,
        "approval": {
            "review_id": reviews[0].id,
            "decision": reviews[0].decision,
            "reviewer_id": reviews[0].reviewer_id,
            "conditions": reviews[0].conditions,
        }
        if reviews
        else None,
        "open_findings": await session.scalar(
            select(func.count())
            .select_from(FindingRecord)
            .where(FindingRecord.model_id == record.id, FindingRecord.status == "open")
        ),
        "monitoring": monitoring.payload if monitoring else None,
    }
    return out


@router.get("/{model_id}/history")
async def history(
    model_id: str,
    session: Session,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    from app.db.tables import AuditRecord, runningRecord, ReviewRecord
    from sqlalchemy import select

    if not await model_service.get_model(session, model_id):
        raise HTTPException(404, detail="Model not found")
    ids = [model_id] + list(
        await session.scalars(
            select(runningRecord.id).where(runningRecord.model_id == model_id)
        )
    )
    ids += list(
        await session.scalars(
            select(ReviewRecord.id).where(ReviewRecord.run_id.in_(ids))
        )
    )
    records = list(
        await session.scalars(
            select(AuditRecord)
            .where(AuditRecord.resource_id.in_(ids))
            .order_by(AuditRecord.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    )

    from app.services.public_events import public_event

    return [public_event(r) for r in records]


@router.post("/{model_id}/versions/{version}/parity", status_code=201)
async def official_parity(
    model_id: str,
    version: str,
    session: Session,
    who: Annotated[Actor, Depends(actor)],
):
    """Run official parity on a rebuild candidate: the same check the agent runs."""
    from app.db.session import SessionFactory
    from app.services.parity_service import ParityError, parity_read, run_parity

    record = await model_service.get_model(session, model_id)
    if record is None:
        raise HTTPException(404, detail="Model not found")
    if record.version != version:
        raise HTTPException(409, detail="Model version does not match")
    try:
        result = await run_parity(
            SessionFactory, model_id, initiator="human", actor_id=who.id
        )
    except ParityError as exc:
        raise HTTPException(
            409, detail={"code": "parity_not_applicable", "message": str(exc)}
        ) from None
    return parity_read(result)


@router.get("/{model_id}/parity-runs")
async def list_parity_runs(
    model_id: str,
    session: Session,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    """Parity runs on this candidate, or on every candidate of this parent model, newest
    first, from agents and people alike."""
    from sqlalchemy import or_, select
    from app.services.parity_service import ParityRunRecord, parity_read

    rows = await session.scalars(
        select(ParityRunRecord)
        .where(
            or_(
                ParityRunRecord.candidate_model_id == model_id,
                ParityRunRecord.parent_model_id == model_id,
            )
        )
        .order_by(ParityRunRecord.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return [parity_read(r) for r in rows]
