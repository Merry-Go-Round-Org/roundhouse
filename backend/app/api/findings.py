from typing import Annotated
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_session
from app.db.tables import FindingRecord, AuditRecord, FindingAssignment
from app.schemas.reports import FindingUpdate
from app.services.identity import reviewer, Actor

router = APIRouter(prefix="/findings", tags=["Findings"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("")
async def list_findings(
    session: Session,
    model_id: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    query = select(FindingRecord)
    if model_id:
        query = query.where(FindingRecord.model_id == model_id)
    records = list(
        await session.scalars(
            query.order_by(FindingRecord.id).offset(offset).limit(limit)
        )
    )
    return [await finding_view(session, r) for r in records]


@router.patch("/{finding_id}")
async def update_finding(
    finding_id: str,
    payload: FindingUpdate,
    session: Session,
    who: Annotated[Actor, Depends(reviewer)],
):
    record = await session.get(FindingRecord, finding_id)
    if record is None:
        raise HTTPException(404, detail="Finding not found")
    if not payload.comment.strip():
        raise HTTPException(422, detail="Comment required")
    if payload.status is not None:
        record.status = payload.status
    if {"owner_id", "due_date"} & payload.model_fields_set:
        from app.db.auth_tables import UserRecord

        if payload.owner_id:
            owner = await session.get(UserRecord, payload.owner_id)
            if not owner or not owner.active:
                raise HTTPException(
                    422, detail="Finding owner must be an active account"
                )
        assignment = await session.get(FindingAssignment, finding_id)
        if assignment is None:
            assignment = FindingAssignment(finding_id=finding_id)
            session.add(assignment)
        if "owner_id" in payload.model_fields_set:
            assignment.owner_id = payload.owner_id
        if "due_date" in payload.model_fields_set:
            assignment.due_date = (
                (
                    payload.due_date.replace(tzinfo=timezone.utc)
                    if payload.due_date.tzinfo is None
                    else payload.due_date.astimezone(timezone.utc)
                )
                if payload.due_date
                else None
            )
    record.comments = [
        *record.comments,
        {
            "actor_id": who.id,
            "comment": payload.comment,
            "at": datetime.now(timezone.utc).isoformat(),
        },
    ]
    session.add(
        AuditRecord(
            actor_id=who.id,
            resource_id=finding_id,
            action="finding_updated",
            payload=payload.model_dump(mode="json"),
        )
    )
    await session.commit()
    return await finding_view(session, record)


async def finding_view(session, record):
    assignment = await session.get(FindingAssignment, record.id)
    return {
        **{
            key: getattr(record, key)
            for key in (
                "id",
                "model_id",
                "report_id",
                "check_id",
                "severity",
                "status",
                "summary",
                "comments",
            )
        },
        "owner_id": assignment.owner_id if assignment else None,
        "due_date": assignment.due_date.replace(tzinfo=timezone.utc).isoformat()
        if assignment and assignment.due_date
        else None,
    }


@router.get("/owners")
async def finding_owners(session: Session, who: Annotated[Actor, Depends(reviewer)]):
    from app.db.auth_tables import UserRecord

    return [
        {"id": u.id, "username": u.username, "role": u.role}
        for u in await session.scalars(
            select(UserRecord).where(UserRecord.active.is_(True))
        )
    ]
