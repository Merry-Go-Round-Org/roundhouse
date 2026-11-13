"""Authenticated review queue with immutable artifact bindings and explicit gates."""

from typing import Annotated, Literal
from datetime import timezone
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_session
from app.db.tables import ReviewRecord, AuditRecord
from app.schemas.reviews import ReviewDecision
from app.services.identity import reviewer, Actor, actor
from app.services.review_service import decide, review_read

router = APIRouter(prefix="/reviews", tags=["Reviews"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("")
async def list_reviews(
    session: Session,
    who: Annotated[Actor, Depends(actor)],
    status: Literal["pending", "decided"] = "pending",
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    records = await session.scalars(
        select(ReviewRecord)
        .where(ReviewRecord.status == status)
        .order_by(ReviewRecord.created_at.desc(), ReviewRecord.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return [await review_read(session, record) for record in records]


@router.get("/{review_id}")
async def get_review(
    review_id: str, session: Session, who: Annotated[Actor, Depends(actor)]
):
    record = await session.get(ReviewRecord, review_id)
    if record is None:
        raise HTTPException(404, detail="Review not found")
    return await review_read(session, record)


@router.post("/{review_id}/open")
async def open_review(
    review_id: str, session: Session, who: Annotated[Actor, Depends(reviewer)]
):
    record = await session.get(ReviewRecord, review_id)
    if record is None:
        raise HTTPException(404, detail="Review not found")
    if record.status != "pending":
        raise HTTPException(409, detail="Review already decided")
    first = await session.scalar(
        select(AuditRecord).where(
            AuditRecord.resource_id == review_id,
            AuditRecord.actor_id == who.id,
            AuditRecord.action == "review_opened",
        )
    )
    if first is None:
        first = AuditRecord(
            actor_id=who.id, resource_id=review_id, action="review_opened", payload={}
        )
        session.add(first)
        await session.commit()
        await session.refresh(first)
    return {
        "review_id": review_id,
        "opened_at": first.created_at.replace(tzinfo=timezone.utc),
        "measurement": "elapsed time since explicit open; not active review effort",
    }


@router.post("/{review_id}/decision")
async def decide_review(
    review_id: str,
    payload: ReviewDecision,
    session: Session,
    who: Annotated[Actor, Depends(reviewer)],
):
    return await review_read(session, await decide(session, review_id, payload, who))
