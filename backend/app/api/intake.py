"""AI Native archive endpoints; legacy bundle endpoints remain available."""

import asyncio
import shutil
import zipfile
from typing import Annotated
from uuid import uuid4
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_session
from app.services.identity import actor
from app.services import intake_service as service
from app.services.upload_service import store_stream, MAX_BUNDLE
from app.schemas.ai_native import ArchiveRead

router = APIRouter(prefix="/intake", tags=["AI archive intake"])
Session = Annotated[AsyncSession, Depends(get_session)]


async def view(session, record):
    from app.services.upload_service import InventoryRecord

    result = {c.name: getattr(record, c.name) for c in record.__table__.columns}
    result["plan_sha256"] = service.plan_hash(record.plan) if record.plan else None
    inventories = await session.scalars(
        select(InventoryRecord).where(
            InventoryRecord.provenance["archive_id"].as_string() == record.id
        )
    )
    result["models"] = [
        {
            "model_id": inventory.model_id,
            "name": inventory.name,
            "model_version": inventory.model_version,
            "document_run_id": inventory.document_run_id,
            "reproduction": inventory.provenance.get("reproduction"),
        }
        for inventory in inventories
    ]
    return result


@router.get("/capabilities")
async def capabilities():
    return {"harness_profiles": service.capabilities(), "gate_0_role": "developer"}


@router.post("/archives", status_code=202, response_model=ArchiveRead)
async def upload(session: Session, archive: UploadFile = File(...), who=Depends(actor)):
    if who.role not in ("developer", "admin"):
        raise HTTPException(403, "Developer or admin role required")
    service.require_intake()
    identity = str(uuid4())
    folder = service.root(identity)
    try:
        sha = await store_stream(archive, folder / "archive.zip", MAX_BUNDLE)
        files = await asyncio.to_thread(
            service.unpack, folder / "archive.zip", folder / "files"
        )
        from app.services.upload_service import DatasetRecord

        datasets = list(await session.scalars(select(DatasetRecord)))
        protected = {
            sha
            for dataset in datasets
            for sha in (
                dataset.sha256,
                dataset.statistics.get("partition_sha256", {}).get("test"),
            )
            if sha
        }
        if any(file["sha256"] in protected for file in files):
            raise ValueError(
                "Archive includes a protected dataset source or final-test partition"
            )
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(422, "Invalid or unsafe archive") from exc
    record = service.ArchiveRecord(
        id=identity, actor_id=who.id, sha256=sha, status="queued", files=files
    )
    session.add(record)
    await session.commit()
    from app.workers.tasks import execute_intake

    try:
        await asyncio.to_thread(execute_intake.delay, identity)
    except Exception:
        record.status, record.error = "failed", "Archive dispatch unavailable"
        await session.commit()
        raise HTTPException(
            503, detail={"archive_id": identity, "message": record.error}
        ) from None
    return await view(session, record)


@router.get("/archives", response_model=list[ArchiveRead])
async def listing(
    session: Session, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
    who=Depends(actor),
):
    statement = select(service.ArchiveRecord)
    if who.role != "admin":
        statement = statement.where(service.ArchiveRecord.actor_id == who.id)
    rows = await session.scalars(
        statement.order_by(service.ArchiveRecord.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return [await view(session, row) for row in rows]


@router.get("/archives/{identity}", response_model=ArchiveRead)
async def retrieve(identity: str, session: Session, who=Depends(actor)):
    record = await session.get(service.ArchiveRecord, identity)
    if not record or (record.actor_id != who.id and who.role != "admin"):
        raise HTTPException(404, "Archive not found")
    return await view(session, record)


@router.post("/archives/{identity}/cancel", response_model=ArchiveRead)
async def cancel(identity: str, session: Session, who=Depends(actor)):
    from sqlalchemy import update

    record = await session.get(service.ArchiveRecord, identity)
    if not record:
        raise HTTPException(404, "Archive not found")
    if who.id != record.actor_id and who.role != "admin":
        raise HTTPException(403, "Archive owner required")
    changed = await session.execute(
        update(service.ArchiveRecord)
        .where(
            service.ArchiveRecord.id == identity,
            service.ArchiveRecord.status.in_(("queued", "running", "awaiting_review")),
        )
        .values(status="cancelled")
    )
    if changed.rowcount != 1:
        raise HTTPException(409, "Archive cannot be cancelled in its current state")
    await session.commit()
    await session.refresh(record)
    return await view(session, record)


@router.get("/archives/{identity}/file")
async def download_file(
    identity: str, path: str, session: Session, candidate_id: str | None = None
):
    from fastapi.responses import Response
    from app.services.package_service import checked_file, digest

    record = await session.get(service.ArchiveRecord, identity)
    if not record:
        raise HTTPException(404, "Archive not found")
    if candidate_id is not None:
        candidate = next(
            (
                c
                for c in (record.plan or {}).get("candidates", [])
                if c["candidate_id"] == candidate_id
            ),
            None,
        )
        if (
            not candidate
            or path != "score.py"
            or not candidate["scoring"].get("wrapper")
        ):
            raise HTTPException(404, "Reviewed wrapper not found")
        folder = service.root(identity) / "wrappers" / candidate_id
        file = checked_file(folder, "score.py")
        scoring = candidate["scoring"]
        if scoring.get("wrapper_files"):
            # Intake binds the complete wrapper folder, including helper modules,
            # with a hash of its path -> file-hash map, not score.py's file hash.
            hashes = {
                name: digest(checked_file(folder, name))
                for name in scoring["wrapper_files"]
            }
            if service.plan_hash(hashes) != scoring["wrapper_sha256"]:
                raise HTTPException(409, "File changed since intake")
            expected = hashes.get("score.py")
        else:
            # Older saved plans bound a single wrapper file directly.
            expected = scoring["wrapper_sha256"]
    else:
        expected = next((f["sha256"] for f in record.files if f["path"] == path), None)
        if not expected:
            raise HTTPException(404, "Archive file not found")
        file = checked_file(service.root(identity) / "files", path)
    if digest(file) != expected:
        raise HTTPException(409, "File changed since intake")
    return Response(
        file.read_bytes(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="intake-file"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/archives/{identity}/decision", response_model=ArchiveRead)
async def decide(
    identity: str, body: service.GateZero, session: Session, who=Depends(actor)
):
    record = await service.confirm(session, identity, body, who)
    from app.workers.tasks import execute_intake_bundles

    try:
        await asyncio.to_thread(execute_intake_bundles.delay, identity)
    except Exception:
        pass  # The recovery task dispatches confirmed plans.
    return await view(session, record)


@router.post("/archives/{identity}/retry", status_code=202, response_model=ArchiveRead)
async def retry(identity: str, session: Session, who=Depends(actor)):
    from sqlalchemy import update

    record = await session.get(service.ArchiveRecord, identity)
    if not record:
        raise HTTPException(404, "Archive not found")
    if who.role not in ("developer", "admin") or who.id != record.actor_id:
        raise HTTPException(403, "The uploading developer must retry intake")
    target = "confirmed" if record.decision else "queued"
    changed = await session.execute(
        update(service.ArchiveRecord)
        .where(
            service.ArchiveRecord.id == identity,
            service.ArchiveRecord.status == "failed",
        )
        .values(status=target, error=None)
    )
    if changed.rowcount != 1:
        raise HTTPException(409, "Only a failed archive can be retried")
    await session.commit()
    from app.db.session import SessionFactory

    await service.recover(SessionFactory)
    await session.refresh(record)
    return await view(session, record)
