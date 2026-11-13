"""Authenticated SSE chat commands, canonical history and explicit write confirmations."""

from typing import Annotated
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, case
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_session
from app.db.tables import ConversationRecord, ChatMessageRecord
from app.db.chat_tables import ChatTurnRecord, PendingChatAction
from app.schemas.chat import ChatMessageCreate, ChatDecision
from app.services.identity import Actor, actor
from app.services import chat_service

router = APIRouter(prefix="/conversations", tags=["Chat"])
Session = Annotated[AsyncSession, Depends(get_session)]
Who = Annotated[Actor, Depends(actor)]


def event_response(turn_id, after_seq=0):
    return StreamingResponse(
        chat_service.observe(turn_id, after_seq=after_seq),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Chat-Turn-ID": turn_id,
        },
    )


@router.post("", status_code=201)
async def create_conversation(session: Session, who: Who):
    record = ConversationRecord(id=str(uuid4()), actor_id=who.id)
    session.add(record)
    await session.commit()
    return {"id": record.id}


@router.get("")
async def list_conversations(
    session: Session, who: Who, limit: int = Query(20, ge=1, le=100)
):
    return list(
        await session.scalars(
            select(ConversationRecord)
            .where(ConversationRecord.actor_id == who.id)
            .order_by(ConversationRecord.updated_at.desc())
            .limit(limit)
        )
    )


@router.get("/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    session: Session,
    who: Who,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
):
    await chat_service.get_conversation(session, conversation_id, who)
    return list(
        await session.scalars(
            select(ChatMessageRecord)
            .where(ChatMessageRecord.conversation_id == conversation_id)
            .order_by(
                ChatMessageRecord.created_at,
                case((ChatMessageRecord.role == "user", 0), else_=1),
                ChatMessageRecord.id,
            )
            .offset(offset)
            .limit(limit)
        )
    )


@router.get("/{conversation_id}/messages/{message_id}")
async def get_message(
    conversation_id: str, message_id: str, session: Session, who: Who
):
    await chat_service.get_conversation(session, conversation_id, who)
    message = await session.get(ChatMessageRecord, message_id)
    if not message or message.conversation_id != conversation_id:
        raise HTTPException(404, detail="Message not found")
    return message


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    payload: ChatMessageCreate,
    request: Request,
    session: Session,
    who: Who,
):
    turn = await chat_service.submit(session, conversation_id, payload, who)
    chat_service.start_turn(turn.id)
    if request.headers.get("accept") == "application/json":
        return JSONResponse(
            jsonable_encoder(chat_service.turn_read(turn)), status_code=202
        )
    return event_response(turn.id)


@router.get("/{conversation_id}/turns")
async def list_turns(
    conversation_id: str,
    session: Session,
    who: Who,
    client_message_id: str | None = None,
    limit: int = Query(20, ge=1, le=100),
):
    await chat_service.get_conversation(session, conversation_id, who)
    query = select(ChatTurnRecord).where(
        ChatTurnRecord.conversation_id == conversation_id
    )
    if client_message_id:
        query = query.where(ChatTurnRecord.client_message_id == client_message_id)
    return [
        chat_service.turn_read(t)
        for t in await session.scalars(
            query.order_by(ChatTurnRecord.created_at.desc()).limit(limit)
        )
    ]


@router.get("/{conversation_id}/turns/{turn_id}")
async def get_turn(conversation_id: str, turn_id: str, session: Session, who: Who):
    return chat_service.turn_read(
        await chat_service.get_turn(session, conversation_id, turn_id, who)
    )


@router.get("/{conversation_id}/turns/{turn_id}/events")
async def get_events(
    conversation_id: str,
    turn_id: str,
    session: Session,
    who: Who,
    after_seq: int = Query(0, ge=0),
):
    await chat_service.get_turn(session, conversation_id, turn_id, who)
    return event_response(turn_id, after_seq)


@router.post("/{conversation_id}/turns/{turn_id}/cancel")
async def cancel_turn(conversation_id: str, turn_id: str, session: Session, who: Who):
    turn = await chat_service.get_turn(session, conversation_id, turn_id, who)
    return await chat_service.cancel_turn(session, turn)


@router.get("/{conversation_id}/pending-actions")
async def list_actions(conversation_id: str, session: Session, who: Who):
    await chat_service.get_conversation(session, conversation_id, who)
    rows = await session.scalars(
        select(PendingChatAction)
        .join(ChatTurnRecord, ChatTurnRecord.id == PendingChatAction.turn_id)
        .where(
            ChatTurnRecord.conversation_id == conversation_id,
            PendingChatAction.actor_id == who.id,
        )
        .order_by(PendingChatAction.created_at.desc())
        .limit(100)
    )
    return [await chat_service.action_read(session, a) for a in rows]


@router.get("/{conversation_id}/pending-actions/{action_id}")
async def get_action(conversation_id: str, action_id: str, session: Session, who: Who):
    return await chat_service.action_read(
        session, await chat_service.get_action(session, conversation_id, action_id, who)
    )


@router.post("/{conversation_id}/pending-actions/{action_id}/decision")
async def decide_action(
    conversation_id: str,
    action_id: str,
    payload: ChatDecision,
    session: Session,
    who: Who,
):
    result = await chat_service.decide_action(
        session, conversation_id, action_id, payload, who
    )
    return await chat_service.action_read(session, result)
