"""Local operator package registration and development scoring. No arbitrary uploads."""

import asyncio
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, field_serializer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.db.session import get_session
from app.db.tables import PackageRecord, ScoringRecord
from app.services.package_service import register_package, PackageError, checked_file
import hashlib
import re
from app.services.legacy_scoring import create_command, ScoringError

router = APIRouter(
    tags=["Legacy packages"],
    responses={
        404: {
            "description": "Model, package, scoring run or prediction file not found"
        },
        409: {"description": "Result not ready or artifact integrity check failed"},
        422: {"description": "Invalid input or package registration mismatch"},
        503: {"description": "Scoring configuration or dispatch unavailable"},
    },
)
Session = Annotated[AsyncSession, Depends(get_session)]


class PackageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package_name: str


class PackageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    model_id: str
    name: str
    version: str
    language: str
    dataset_id: str
    sha256: str
    input_sha256: str
    n_rows: int


class ScoreRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    package_id: str
    status: Literal["queued", "running", "completed", "failed"]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    result: dict | None

    @field_serializer("created_at", "started_at", "finished_at")
    def utc_timestamp(self, value):
        # SQLite returns naive timestamps; all stored scoring timestamps are UTC.
        return (
            value.replace(tzinfo=timezone.utc).isoformat()
            if value is not None and value.tzinfo is None
            else value.isoformat()
            if value
            else None
        )


@router.post("/models/{model_id}/packages", response_model=PackageRead, status_code=201)
async def register(model_id: str, payload: PackageCreate, session: Session):
    if not settings.allow_legacy_registration:
        raise HTTPException(410, detail="Use POST /models/uploads")
    try:
        return await register_package(session, model_id, payload.package_name)
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except (PackageError, KeyError, TypeError, ValueError) as exc:
        await session.rollback()
        raise HTTPException(
            422,
            detail=str(exc)
            if isinstance(exc, PackageError)
            else "Malformed package manifest or schema",
        ) from exc


@router.get("/models/{model_id}/packages", response_model=list[PackageRead])
async def list_packages(model_id: str, session: Session):
    from app.db.tables import ModelRecord

    if await session.get(ModelRecord, model_id) is None:
        raise HTTPException(404, detail="Model not found")
    return list(
        await session.scalars(
            select(PackageRecord).where(PackageRecord.model_id == model_id)
        )
    )


@router.get("/packages/{package_id}", response_model=PackageRead)
async def get_package(package_id: str, session: Session):
    record = await session.get(PackageRecord, package_id)
    if record is None:
        raise HTTPException(404, detail="Package not found")
    return record


@router.post(
    "/packages/{package_id}/scoring-runs", response_model=ScoreRead, status_code=202
)
async def start_scoring(package_id: str, session: Session):
    from app.workers.tasks import execute_scoring

    package = await session.get(PackageRecord, package_id)
    if package is None:
        raise HTTPException(404, detail="Package not found")
    if not package.dataset_id:
        raise HTTPException(
            409,
            detail={
                "code": "not_applicable",
                "message": "Synthetic survival demonstration supports documentation only",
            },
        )
    try:
        create_command(
            "preflight", settings.scoring_image, settings.storage_path, package.language
        )
    except ScoringError as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    record = ScoringRecord(id=str(uuid4()), package_id=package_id)
    session.add(record)
    await session.commit()
    try:
        await asyncio.to_thread(
            execute_scoring.apply_async,
            args=[record.id],
            task_id=record.id,
            retry=False,
        )
    except Exception:
        changed = await session.execute(
            update(ScoringRecord)
            .where(ScoringRecord.id == record.id, ScoringRecord.status == "queued")
            .values(
                status="failed",
                error="Scoring dispatch failed",
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        if changed.rowcount:
            raise HTTPException(
                503, detail={"code": "dispatch_unavailable", "run_id": record.id}
            ) from None
    await session.refresh(record)
    return record


@router.get("/scoring-runs/{execution_id}", response_model=ScoreRead)
async def get_scoring(execution_id: str, session: Session):
    record = await session.get(ScoringRecord, execution_id)
    if record is None:
        raise HTTPException(404, detail="Scoring run not found")
    return record


@router.get("/packages/{package_id}/scoring-runs", response_model=list[ScoreRead])
async def list_scoring(
    package_id: str,
    session: Session,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    """Newest first. Restore this history on page load; do not rely on browser memory."""
    if await session.get(PackageRecord, package_id) is None:
        raise HTTPException(404, detail="Package not found")
    return list(
        await session.scalars(
            select(ScoringRecord)
            .where(ScoringRecord.package_id == package_id)
            .order_by(ScoringRecord.created_at.desc(), ScoringRecord.id.desc())
            .offset(offset)
            .limit(limit)
        )
    )


def prediction_bytes(record):
    """Read bounded, hash-verified bytes; never serve an arbitrary stored path."""
    if not re.fullmatch(r"[a-f0-9-]{36}", record.id):
        raise HTTPException(409, detail="Invalid prediction reference")
    relative = f"scoring/{record.id}/predictions.csv"
    result = record.result or {}
    if (
        result.get("prediction_reference") != relative
        or result.get("package_id") != record.package_id
    ):
        raise HTTPException(
            409, detail="Prediction evidence does not match the scoring run"
        )
    try:
        path = checked_file(settings.storage_path, relative)
        with path.open("rb") as stream:
            content = stream.read(2_000_001)
    except (PackageError, OSError):
        raise HTTPException(404, detail="Prediction file unavailable") from None
    if len(content) > 2_000_000 or hashlib.sha256(content).hexdigest() != result.get(
        "predictions_sha256"
    ):
        raise HTTPException(409, detail="Prediction integrity check failed")
    return content


@router.get(
    "/scoring-runs/{execution_id}/predictions",
    response_class=Response,
    responses={
        200: {
            "description": "Verified predictions CSV attachment",
            "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def download_predictions(execution_id: str, session: Session):
    record = await session.get(ScoringRecord, execution_id)
    if record is None:
        raise HTTPException(404, detail="Scoring run not found")
    if record.status != "completed":
        raise HTTPException(
            409, detail="Predictions are available only for completed scoring runs"
        )
    content = await asyncio.to_thread(prediction_bytes, record)
    return Response(
        content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="predictions-{record.id}.csv"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
