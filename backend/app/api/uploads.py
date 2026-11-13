"""Browser upload and immutable dataset discovery API."""

import asyncio
import shutil
from typing import Annotated
from uuid import uuid4
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import ValidationError
from app.db.session import get_session
from app.services.identity import actor
from app.services import dataset_deletion
from app.schemas.uploads import UploadCheck, UploadState
from app.services.upload_service import (
    UploadRecord,
    DatasetRecord,
    InventoryRecord,
    UploadError,
    MAX_BUNDLE,
    upload_root,
    store_stream,
    ingest_dataset,
    check,
    now,
)

router = APIRouter(tags=["Uploads and datasets"])
Session = Annotated[AsyncSession, Depends(get_session)]


def require_upload(who):
    if who.role not in ("developer", "admin", "operator"):
        raise HTTPException(403, detail="Developer role required to upload")


def publish_upload(identity):
    from app.workers.tasks import execute_upload

    execute_upload.apply_async(args=[identity], task_id=identity, retry=False)


@router.post("/models/uploads", response_model=UploadState, status_code=202)
async def upload_model(
    session: Session, bundle: UploadFile = File(...), who=Depends(actor)
):
    require_upload(who)
    if not bundle.filename or not bundle.filename.lower().endswith(".zip"):
        raise HTTPException(422, detail="Select a model ZIP bundle")
    identity = str(uuid4())
    root = upload_root(identity)
    try:
        sha = await store_stream(bundle, root / "bundle.zip", MAX_BUNDLE)
    except UploadError as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(422, detail=str(exc)) from None
    record = UploadRecord(
        id=identity,
        status="queued",
        actor_id=who.id,
        archive_sha256=sha,
        checks=[],
        files=[],
        model_id=None,
        error=None,
    )
    session.add(record)
    await session.commit()
    try:
        await asyncio.to_thread(publish_upload, identity)
    except Exception:
        # A broker failure can be ambiguous. Preserve any already-started work.
        from sqlalchemy import update

        await session.execute(
            update(UploadRecord)
            .where(UploadRecord.id == identity, UploadRecord.status == "queued")
            .values(
                status="failed",
                error="Could not dispatch upload checks; inspect this upload before retrying",
                finished_at=now(),
                checks=[check("dispatch", False, "Worker dispatch unavailable")],
            )
        )
        await session.commit()
        await session.refresh(record)
        if record.status == "failed":
            raise HTTPException(
                503, detail={"message": record.error, "upload_id": identity}
            )
    await session.refresh(record)
    return record


@router.get("/models/uploads", response_model=list[UploadState])
async def uploads(
    session: Session,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    who=Depends(actor),
):
    statement = (
        select(UploadRecord)
        .where(UploadRecord.actor_id == who.id)
        .order_by(UploadRecord.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    records = list(await session.scalars(statement))
    return [await upload_view(session, record) for record in records]


@router.post("/models/uploads/{identity}/retry", response_model=UploadState, status_code=202)
async def retry_upload(identity: str, session: Session, who=Depends(actor)):
    from sqlalchemy import update
    from app.services.package_service import digest

    require_upload(who)
    record = await session.get(UploadRecord, identity)
    if not record or (record.actor_id != who.id and who.role != "admin"):
        raise HTTPException(404, detail="Upload not found")
    if record.status != "failed":
        raise HTTPException(409, detail="Only a failed upload can be retried")
    existing = await session.scalar(
        select(InventoryRecord).where(InventoryRecord.upload_id == identity)
    )
    if existing:
        raise HTTPException(409, detail="Model already registered; inspect inventory")
    archive = upload_root(identity) / "bundle.zip"
    if not archive.is_file() or digest(archive) != record.archive_sha256:
        raise HTTPException(409, detail="Stored archive is unavailable or changed")
    changed = await session.execute(
        update(UploadRecord)
        .where(UploadRecord.id == identity, UploadRecord.status == "failed")
        .values(status="queued", error=None, checks=[], files=[], started_at=None, finished_at=None)
    )
    if changed.rowcount != 1:
        await session.rollback()
        raise HTTPException(409, detail="Upload state changed; reload before retrying")
    await session.commit()
    try:
        await asyncio.to_thread(publish_upload, identity)
    except Exception:
        await session.execute(
            update(UploadRecord)
            .where(UploadRecord.id == identity, UploadRecord.status == "queued")
            .values(status="failed", error="Could not dispatch upload checks; inspect status before retrying", finished_at=now())
        )
        await session.commit()
        raise HTTPException(503, detail={"upload_id": identity, "message": "Worker dispatch unavailable"}) from None
    await session.refresh(record)
    return await upload_view(session, record)


async def upload_view(session, record):
    state = UploadState.model_validate(record)
    if record.status == "completed" and record.model_id and not record.checks:
        inventory = await session.get(InventoryRecord, record.model_id)
        if (
            inventory
            and inventory.upload_id == record.id
            and inventory.provenance.get("archive_id")
            and inventory.provenance.get("gate_0")
        ):
            state.checks = [
                UploadCheck(**check("gate_0_registration", reason="Confirmed intake bundle registered"))
            ]
    return state


@router.get("/models/uploads/{identity}", response_model=UploadState)
async def upload_status(identity: str, session: Session, who=Depends(actor)):
    record = await session.get(UploadRecord, identity)
    if not record or (record.actor_id != who.id and who.role != "admin"):
        raise HTTPException(404, detail="Upload not found")
    return await upload_view(session, record)


async def dataset_view(session, record):
    count = await session.scalar(
        select(func.count())
        .select_from(InventoryRecord)
        .where(InventoryRecord.dataset_version_id == record.id)
    )
    d = record.descriptor
    return {
        "id": record.dataset_id,
        "version_id": record.id,
        "name": d["name"],
        "filename": d["source_filename"],
        "target_description": f"{d['target_column']}: positive class {d['positive_class']}",
        "limitations": d.get("notes", []),
        "version": record.version,
        "sha256": record.sha256,
        "split_sha256": record.split_sha256,
        "verified_upload": True,
        "descriptor": d,
        "columns": record.columns,
        "statistics": record.statistics,
        "model_count": count,
        "created_at": record.created_at,
    }


@router.post("/datasets", status_code=201)
async def upload_dataset(
    session: Session,
    source: UploadFile = File(...),
    descriptor: UploadFile = File(...),
    split: UploadFile | None = File(None),
    who=Depends(actor),
):
    require_upload(who)
    try:
        record = await ingest_dataset(session, source, descriptor, split, who.id)
    except (UploadError, ValidationError) as exc:
        raise HTTPException(422, detail=str(exc)) from None
    return {
        "id": record.dataset_id,
        "version_id": record.id,
        "status": "completed",
        "checks": [
            check("source_sha256"),
            check("descriptor_columns"),
            check("split_sha256"),
            check("complete_disjoint_partition_membership"),
            check("immutable_version"),
        ],
        "dataset": await dataset_view(session, record),
    }


@router.get("/datasets")
async def datasets(session: Session, who=Depends(actor)):
    records = list(
        await session.scalars(
            dataset_deletion.visible_datasets().order_by(DatasetRecord.created_at.desc())
        )
    )
    latest = {}
    for record in records:
        latest.setdefault(record.dataset_id, record)
    return [await dataset_view(session, r) for r in latest.values()]


@router.post("/datasets/deletions/preview")
async def preview_dataset_deletion(
    payload: dataset_deletion.Selection, session: Session, who=Depends(actor)
):
    return await dataset_deletion.preview(session, payload.dataset_version_ids, who)


@router.post("/datasets/deletions")
async def delete_datasets(
    payload: dataset_deletion.DeleteRequest, session: Session, who=Depends(actor)
):
    return await dataset_deletion.remove(session, payload, who)


@router.get("/datasets/deletions/{request_id}")
async def dataset_deletion_receipt(request_id: str, session: Session, who=Depends(actor)):
    return await dataset_deletion.receipt(session, request_id, who)


@router.get("/datasets/{identity}")
async def dataset_detail(identity: str, session: Session, who=Depends(actor)):
    record = await session.get(DatasetRecord, identity)
    if record is not None and await dataset_deletion.is_deleted(session, record.id):
        record = None
    if record is None:
        record = await session.scalar(
            dataset_deletion.visible_datasets()
            .where(DatasetRecord.dataset_id == identity)
            .order_by(DatasetRecord.created_at.desc())
        )
    if record is None:
        raise HTTPException(404, detail="Uploaded dataset not found")
    view = await dataset_view(session, record)
    view["models"] = [
        r.model_id
        for r in await session.scalars(
            select(InventoryRecord).where(
                InventoryRecord.dataset_version_id == record.id
            )
        )
    ]
    return view
