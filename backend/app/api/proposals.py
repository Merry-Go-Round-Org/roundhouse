"""Unified proposal cards for authenticated people; agents use the service directly."""

from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_session
from app.services.identity import actor
from app.services import proposal_service as service
from app.schemas.ai_native import ProposalRead

router = APIRouter(prefix="/proposals", tags=["AI action proposals"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/actions")
async def actions():
    return {
        name: {
            "args_schema": schema.model_json_schema(),
            "required_role": role,
            "execution_available": True,
        }
        for name, (schema, role) in service.ACTIONS.items()
    }


@router.get("", response_model=list[ProposalRead])
async def listing(
    session: Session,
    model_id: str | None = None,
    source_run_id: str | None = None,
    status: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    query = select(service.ProposalRecord)
    for name, value in (
        ("model_id", model_id),
        ("source_run_id", source_run_id),
        ("status", status),
    ):
        if value is not None:
            query = query.where(getattr(service.ProposalRecord, name) == value)
    rows = list(
        await session.scalars(
            query.order_by(
                service.ProposalRecord.created_at.desc(),
                service.ProposalRecord.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
    )
    for row in rows:
        await service.reconcile(session, row)
    await session.commit()
    return [service.view(row) for row in rows]


@router.get("/{identity}", response_model=ProposalRead)
async def retrieve(identity: str, session: Session):
    record = await session.get(service.ProposalRecord, identity)
    if not record:
        raise HTTPException(404, "Proposal not found")
    await service.reconcile(session, record)
    await session.commit()
    return service.view(record)


@router.post("/{identity}/decision", response_model=ProposalRead)
async def decision(
    identity: str, body: service.Decision, session: Session, who=Depends(actor)
):
    return service.view(await service.decide(session, identity, body, who))


@router.post("/{identity}/execute", response_model=ProposalRead)
async def execution(identity: str, session: Session, who=Depends(actor)):
    return service.view(await service.execute(session, identity, who))
