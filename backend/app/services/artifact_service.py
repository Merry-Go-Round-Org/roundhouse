"""Versioned artifacts; opaque IDs and verified bytes, never arbitrary download paths."""

import asyncio
import hashlib
from uuid import uuid4
from fastapi import HTTPException
from app.config import settings
from app.db.tables import ArtifactRecord
from app.services.package_service import checked_file, PackageError


async def save_artifact(session, run, content: bytes, filename: str, media_type: str):
    if len(content) > (64_000_000 if media_type == "application/zip" else 2_000_000):
        raise ValueError("Artifact exceeds 2 MB limit")
    identity = str(uuid4())
    digest = hashlib.sha256(content).hexdigest()

    def write():
        path = settings.storage_path / "artifacts" / identity
        path.mkdir(parents=True, exist_ok=False)
        (path / "content").write_bytes(content)

    await asyncio.to_thread(write)
    record = ArtifactRecord(
        id=identity,
        model_id=run.model_id,
        run_id=run.id,
        version=digest,
        filename=filename,
        media_type=media_type,
        sha256=digest,
        size=len(content),
    )
    session.add(record)
    await session.flush()
    return record


async def artifact_bytes(record):
    def read():
        try:
            path = checked_file(settings.storage_path, f"artifacts/{record.id}/content")
            with path.open("rb") as stream:
                data = stream.read(
                    64_000_001 if record.media_type == "application/zip" else 2_000_001
                )
        except (OSError, PackageError):
            raise HTTPException(404, detail="Artifact file unavailable") from None
        if (
            len(data) != record.size
            or hashlib.sha256(data).hexdigest() != record.sha256
        ):
            raise HTTPException(409, detail="Artifact integrity check failed")
        return data

    return await asyncio.to_thread(read)
