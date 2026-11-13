"""Bulk soft deletion of models.

Deleting a model hides it from the inventory and stops new work on it. Nothing is
erased: runs, reviews, reports, findings, proposals, monitoring evidence, uploaded
files and audit history stay, because model-risk evidence must be retained. Only
active work (queued/running runs, unfinished upload processing) and authorization
block deletion. Re-uploading the identical bundle restores a deleted model.
"""

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import DateTime, JSON, String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from app.db.tables import (
    Base, ModelRecord, runningRecord, ReviewRecord, ReportRecord,
    ArtifactRecord, FindingRecord, MonitoringRecord, AuditRecord,
)
from app.services.upload_service import InventoryRecord, UploadRecord

MAX_SELECTION = 20
DELETED = "deleted"
ACTIVE_RUN_STATUSES = ("queued", "running")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Selection(Strict):
    model_ids: list[str] = Field(min_length=1, max_length=MAX_SELECTION)

    @field_validator("model_ids")
    @classmethod
    def unique_ids(cls, value):
        if len(set(value)) != len(value) or any(not item.strip() for item in value):
            raise ValueError("Select distinct, nonempty model IDs")
        return value


class DeleteRequest(Selection):
    preview_token: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: UUID


class ModelDeletionRecord(Base):
    __tablename__ = "model_deletion_receipts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(120))
    model_ids: Mapped[list] = mapped_column(JSON)
    preview_token: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class DeletedModel(Base):
    """Tombstone remembering the lifecycle stage a soft-deleted model had."""

    __tablename__ = "deleted_models"
    model_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    previous_status: Mapped[str] = mapped_column(String(30))
    actor_id: Mapped[str] = mapped_column(String(120))
    request_id: Mapped[str] = mapped_column(String(36))
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


def is_deleted(model) -> bool:
    return model is not None and model.lifecycle_status == DELETED


async def restore(session, model, actor_id, reason):
    """Undo a soft delete (used when the identical bundle is uploaded again)."""
    tombstone = await session.get(DeletedModel, model.id)
    model._restoring = True
    try:
        model.lifecycle_status = tombstone.previous_status if tombstone else "development"
    finally:
        model._restoring = False
    if tombstone:
        await session.delete(tombstone)
    session.add(AuditRecord(actor_id=actor_id, resource_id=model.id, action="model_restored",
                            payload={"reason": reason, "lifecycle_status": model.lifecycle_status}))


def require_role(who):
    if who.role not in ("developer", "admin"):
        raise HTTPException(403, "Developer or admin role required")


async def count(session, table, column, identity):
    return await session.scalar(select(func.count()).select_from(table).where(column == identity)) or 0


async def preview(session, ids, who):
    require_role(who)
    records = list(await session.scalars(select(ModelRecord).where(ModelRecord.id.in_(ids))))
    by_id = {row.id: row for row in records}
    if len(by_id) != len(ids) or any(is_deleted(by_id[i]) for i in ids):
        raise HTTPException(404, "One or more selected models are unavailable")
    from app.services.proposal_service import ProposalRecord

    items = []
    for identity in ids:
        model = by_id[identity]
        inventory = await session.get(InventoryRecord, identity)
        upload = await session.get(UploadRecord, inventory.upload_id) if inventory else None
        run_ids = select(runningRecord.id).where(runningRecord.model_id == identity)
        # Informational only: everything counted here is retained, not deleted.
        counts = {
            "runs": await count(session, runningRecord, runningRecord.model_id, identity),
            "reviews": await session.scalar(select(func.count()).select_from(ReviewRecord).where(ReviewRecord.run_id.in_(run_ids))) or 0,
            "reports": await count(session, ReportRecord, ReportRecord.model_id, identity),
            "artifacts": await count(session, ArtifactRecord, ArtifactRecord.model_id, identity),
            "findings": await count(session, FindingRecord, FindingRecord.model_id, identity),
            "monitoring_events": await count(session, MonitoringRecord, MonitoringRecord.model_id, identity),
            "proposals": await count(session, ProposalRecord, ProposalRecord.model_id, identity),
        }
        blockers = []
        if who.role != "admin" and (not upload or upload.actor_id != who.id):
            blockers.append("only the uploading developer or an admin may delete this model")
        active = await session.scalar(select(func.count()).select_from(runningRecord).where(
            runningRecord.model_id == identity, runningRecord.status.in_(ACTIVE_RUN_STATUSES)
        )) or 0
        if active:
            blockers.append(f"active runs: {active} (wait for them to finish)")
        if upload and upload.status not in ("completed", "failed"):
            blockers.append("upload processing is not complete")
        items.append({
            "id": identity, "name": model.name, "version": model.version,
            "dataset_id": model.dataset_id, "lifecycle_status": model.lifecycle_status,
            "bundle_sha256": inventory.bundle_sha256 if inventory else None,
            "related_counts": counts, "blockers": blockers,
        })
    snapshot = {"model_ids": ids, "models": items}
    token = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "model_ids": ids, "models": items, "preview_token": token,
        "can_delete": not any(item["blockers"] for item in items),
        "effect": "Remove the selected models from the inventory and stop new work on them. "
                  "Runs, reports, reviews, evidence, uploaded files and audit history are kept. "
                  "Re-uploading the identical bundle restores a model.",
        "atomic": True, "max_selection": MAX_SELECTION,
    }


async def remove(session, payload, who):
    require_role(who)
    request_id = str(payload.request_id)
    previous = await session.get(ModelDeletionRecord, request_id)
    if previous:
        if previous.actor_id != who.id or previous.model_ids != payload.model_ids or previous.preview_token != payload.preview_token:
            raise HTTPException(409, "Request ID was already used for another selection")
        return previous.result
    current = await preview(session, payload.model_ids, who)
    if current["preview_token"] != payload.preview_token:
        raise HTTPException(409, "Selection changed; request a new preview")
    if not current["can_delete"]:
        raise HTTPException(409, detail={"code": "model_delete_blocked", "models": current["models"]})
    for identity in payload.model_ids:
        model = await session.get(ModelRecord, identity)
        session.add(DeletedModel(model_id=identity, previous_status=model.lifecycle_status,
                                 actor_id=who.id, request_id=request_id))
        session.add(AuditRecord(actor_id=who.id, resource_id=identity, action="model_deleted", payload={
            "request_id": request_id, "name": model.name, "version": model.version,
            "previous_status": model.lifecycle_status,
        }))
        model.lifecycle_status = DELETED
    result = {"request_id": request_id, "deleted": payload.model_ids, "blocked": [], "failed": [],
              "status": "deleted", "retained": ["evidence", "uploaded_files", "audit_history"]}
    session.add(ModelDeletionRecord(id=request_id, actor_id=who.id, model_ids=payload.model_ids,
                                    preview_token=payload.preview_token, result=result))
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return result


async def receipt(session, request_id, who):
    require_role(who)
    saved = await session.get(ModelDeletionRecord, str(request_id))
    if not saved or (saved.actor_id != who.id and who.role != "admin"):
        raise HTTPException(404, "Deletion receipt not found")
    return saved.result
