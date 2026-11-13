"""Bulk soft deletion of uploaded dataset versions; catalog fixtures remain.

A deleted version disappears from the dataset catalogue and cannot be used by new
model uploads. Files, statistics and audit history stay so that existing evidence
remains reproducible. A version is blocked only while a model that is not itself
deleted still uses it. Uploading the identical version again restores it.
"""

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field, field_validator
from sqlalchemy import DateTime, JSON, String, select
from sqlalchemy.orm import Mapped, mapped_column

from app.db.tables import Base, ModelRecord, AuditRecord
from app.services.model_deletion import Strict, MAX_SELECTION, DELETED, require_role
from app.services.upload_service import DatasetRecord, InventoryRecord


class Selection(Strict):
    dataset_version_ids: list[str] = Field(min_length=1, max_length=MAX_SELECTION)

    @field_validator("dataset_version_ids")
    @classmethod
    def unique_ids(cls, value):
        if len(set(value)) != len(value) or any(not item.strip() for item in value):
            raise ValueError("Select distinct, nonempty dataset version IDs")
        return value


class DeleteRequest(Selection):
    preview_token: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: UUID


class DatasetDeletionRecord(Base):
    __tablename__ = "dataset_deletion_receipts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(120))
    dataset_version_ids: Mapped[list] = mapped_column(JSON)
    preview_token: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class DeletedDatasetVersion(Base):
    """Tombstone for a soft-deleted uploaded dataset version."""

    __tablename__ = "deleted_dataset_versions"
    version_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(120))
    request_id: Mapped[str] = mapped_column(String(36))
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


def visible_datasets():
    """SELECT for dataset versions that have not been soft-deleted."""
    return select(DatasetRecord).where(
        DatasetRecord.id.not_in(select(DeletedDatasetVersion.version_id))
    )


async def is_deleted(session, version_id) -> bool:
    return await session.get(DeletedDatasetVersion, version_id) is not None


async def restore(session, record, actor_id, reason):
    tombstone = await session.get(DeletedDatasetVersion, record.id)
    if tombstone:
        await session.delete(tombstone)
        session.add(AuditRecord(actor_id=actor_id, resource_id=record.id, action="dataset_restored",
                                payload={"reason": reason}))


async def preview(session, ids, who):
    require_role(who)
    records = list(await session.scalars(visible_datasets().where(DatasetRecord.id.in_(ids))))
    by_id = {row.id: row for row in records}
    if len(by_id) != len(ids):
        raise HTTPException(404, "One or more uploaded dataset versions are unavailable")
    items = []
    for identity in ids:
        record = by_id[identity]
        owner = await session.scalar(select(AuditRecord.actor_id).where(
            AuditRecord.resource_id == identity, AuditRecord.action == "dataset_uploaded"
        ).order_by(AuditRecord.created_at.desc()))
        models = list(await session.scalars(
            select(ModelRecord.id)
            .join(InventoryRecord, InventoryRecord.model_id == ModelRecord.id)
            .where(InventoryRecord.dataset_version_id == identity, ModelRecord.lifecycle_status != DELETED)
        ))
        blockers = {"registered_models": sorted(set(models))}
        if who.role != "admin" and owner != who.id:
            blockers["authorization"] = ["only the uploading developer or an admin may delete this dataset version"]
        items.append({
            "version_id": identity, "id": record.dataset_id, "name": record.descriptor.get("name"),
            "version": record.version, "sha256": record.sha256, "split_sha256": record.split_sha256,
            "blockers": {key: value for key, value in blockers.items() if value},
        })
    snapshot = {"dataset_version_ids": ids, "datasets": items}
    token = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "dataset_version_ids": ids, "datasets": items, "preview_token": token,
        "can_delete": not any(item["blockers"] for item in items),
        "effect": "Remove the selected uploaded dataset versions from the catalogue so new uploads cannot use them. "
                  "Files, existing evidence and audit history are kept. Versions still used by a model are blocked; "
                  "delete those models first.",
        "atomic": True, "max_selection": MAX_SELECTION,
    }


async def remove(session, payload, who):
    require_role(who)
    request_id = str(payload.request_id)
    previous = await session.get(DatasetDeletionRecord, request_id)
    if previous:
        if previous.actor_id != who.id or previous.dataset_version_ids != payload.dataset_version_ids or previous.preview_token != payload.preview_token:
            raise HTTPException(409, "Request ID was already used for another selection")
        return previous.result
    current = await preview(session, payload.dataset_version_ids, who)
    if current["preview_token"] != payload.preview_token:
        raise HTTPException(409, "Selection changed; request a new preview")
    if not current["can_delete"]:
        raise HTTPException(409, detail={"code": "dataset_delete_blocked", "datasets": current["datasets"]})
    for identity in payload.dataset_version_ids:
        record = await session.get(DatasetRecord, identity)
        session.add(DeletedDatasetVersion(version_id=identity, actor_id=who.id, request_id=request_id))
        session.add(AuditRecord(actor_id=who.id, resource_id=identity, action="dataset_deleted", payload={
            "request_id": request_id, "dataset_id": record.dataset_id, "version": record.version,
            "sha256": record.sha256, "split_sha256": record.split_sha256,
        }))
    result = {"request_id": request_id, "deleted": payload.dataset_version_ids, "blocked": [], "failed": [],
              "status": "deleted", "retained": ["dataset_files", "evidence", "audit_history"]}
    session.add(DatasetDeletionRecord(id=request_id, actor_id=who.id, dataset_version_ids=payload.dataset_version_ids,
                                      preview_token=payload.preview_token, result=result))
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return result


async def receipt(session, request_id, who):
    require_role(who)
    saved = await session.get(DatasetDeletionRecord, str(request_id))
    if not saved or (saved.actor_id != who.id and who.role != "admin"):
        raise HTTPException(404, "Deletion receipt not found")
    return saved.result
