"""Stored canonical reports and hash-verified artifact downloads."""

from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_session
from app.db.tables import (
    ModelRecord,
    ReportRecord,
    ArtifactRecord,
    runningRecord,
    ReviewRecord,
    PackageRecord,
)
from app.schemas.reports import ReportRead
from app.services.report_service import report_read
from app.services.artifact_service import artifact_bytes

router = APIRouter(tags=["Reports"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/models/{model_id}/reports", response_model=list[ReportRead])
async def list_reports(
    model_id: str,
    session: Session,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    if await session.get(ModelRecord, model_id) is None:
        raise HTTPException(404, detail="Model not found")
    records = await session.scalars(
        select(ReportRecord)
        .where(ReportRecord.model_id == model_id)
        .order_by(ReportRecord.created_at.desc(), ReportRecord.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return [await detailed_report(session, r) for r in records]


@router.get("/reports/{report_id}", response_model=ReportRead)
async def get_report(report_id: str, session: Session):
    record = await session.get(ReportRecord, report_id)
    if record is None:
        raise HTTPException(404, detail="Report not found")
    return await detailed_report(session, record)


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: str, session: Session):
    artifact = await session.get(ArtifactRecord, artifact_id)
    if artifact is None:
        raise HTTPException(404, detail="Artifact not found")
    data = await artifact_bytes(artifact)
    return Response(
        data,
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def detailed_report(session, record):
    from datetime import timezone

    artifact = await session.get(ArtifactRecord, record.artifact_id)
    run = await session.get(runningRecord, record.run_id)
    review = await session.scalar(
        select(ReviewRecord)
        .where(ReviewRecord.artifact_id == artifact.id)
        .order_by(ReviewRecord.created_at.desc())
    )
    result = report_read(record)
    result.update(
        model_version=run.model_version,
        artifact_version=artifact.version,
        artifact_sha256=artifact.sha256,
        created_at=record.created_at.replace(tzinfo=timezone.utc),
        review_status=review.status if review else None,
        review_id=review.id if review else None,
        review_resolution=(
            {"decision": review.decision, "rationale": review.rationale,
             "conditions": review.conditions, "reviewer_id": review.reviewer_id}
            if review and review.status == "decided" else None
        ),
    )
    return result


@router.get("/reports/{report_id}/markdown")
async def report_markdown(report_id: str, session: Session):
    record = await session.get(ReportRecord, report_id)
    if record is None:
        raise HTTPException(404, detail="Report not found")
    # Text export is a rendering of canonical stored evidence, never a new decision.
    lines = [
        f"# {record.kind.title()} report",
        f"Model: {record.model_id}",
        f"Run: {record.run_id}",
        "",
    ]
    for check in record.payload.get("checks", []):
        lines += [
            f"## {check['check_id']}",
            f"Status: {check['status']}",
            check["summary"],
            "",
        ]
    review = await session.scalar(
        select(ReviewRecord).where(ReviewRecord.artifact_id == record.artifact_id)
        .order_by(ReviewRecord.created_at.desc())
    )
    if review and review.status == "decided":
        lines += ["## Human decision", f"Decision: {review.decision}",
                  f"Rationale: {review.rationale}"]
        lines += [f"Condition: {condition}" for condition in review.conditions or []]
        lines.append("")
    import json

    narrative = record.payload.get("agent_review", {}).get("exports", {}).get("validation_narrative.md")
    if narrative:
        lines += ["## Independent agent review", narrative, ""]

    lines += [
        "## Canonical evidence",
        "```json",
        json.dumps(record.payload, indent=2),
        "```",
    ]
    return Response(
        "\n".join(lines),
        media_type="text/markdown",
        headers={
            "Content-Disposition": 'attachment; filename="validation-report.md"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.get("/models/{model_id}/files")
async def model_files(model_id: str, session: Session):
    package = await session.scalar(
        select(PackageRecord).where(PackageRecord.model_id == model_id)
    )
    if package is None:
        raise HTTPException(404, detail="Model package not found")
    from urllib.parse import quote

    return [
        dict(
            path=path.removeprefix("package/"),
            sha256=sha,
            download_url=f"/api/v1/models/{model_id}/files/{quote(path.removeprefix('package/'), safe='/')}",
        )
        for path, sha in package.file_hashes.items()
        if path.startswith("package/")
        and path.endswith((".md", ".txt", ".json", ".py", ".R"))
    ]


@router.get("/models/{model_id}/files/{path:path}")
async def model_file(model_id: str, path: str, session: Session):
    from app.services.package_service import verify_snapshot, checked_file
    import asyncio

    package = await session.scalar(
        select(PackageRecord).where(PackageRecord.model_id == model_id)
    )
    if (
        package is None
        or "package/" + path not in package.file_hashes
        or not path.endswith((".md", ".txt", ".json", ".py", ".R"))
    ):
        raise HTTPException(404, detail="Readable package file not found")
    root = await asyncio.to_thread(verify_snapshot, package.sha256, package.file_hashes)
    source = checked_file(root, "package/" + path)
    if source.stat().st_size > 2_000_000:
        raise HTTPException(413, detail="File exceeds preview limit")
    return Response(
        await asyncio.to_thread(source.read_bytes),
        media_type="text/plain",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
