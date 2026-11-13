"""API-managed durable chat turns. Disconnect stops observation, never execution."""

import asyncio
import hashlib
import json
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from app.config import settings
from app.db.session import SessionFactory
from app.db.tables import (
    ConversationRecord,
    ChatMessageRecord,
    ModelRecord,
    runningRecord,
    ReportRecord,
    AuditRecord,
    RunExecutionRecord,
)
from app.db.chat_tables import ChatTurnRecord, ChatPublicEvent, PendingChatAction, now
from app.services.harness_adapter import runtime
from app.services.identity import Actor

ACTIVE = ("queued", "running")
_tasks: dict[str, asyncio.Task] = {}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def get_conversation(session, identity, who):
    record = await session.get(ConversationRecord, identity)
    if record is None or record.actor_id != who.id:
        raise HTTPException(404, detail="Conversation not found")
    return record


async def get_turn(session, conversation_id, turn_id, who):
    await get_conversation(session, conversation_id, who)
    record = await session.get(ChatTurnRecord, turn_id)
    if not record or record.conversation_id != conversation_id:
        raise HTTPException(404, detail="Chat turn not found")
    return record


def turn_read(turn):
    return {
        k: getattr(turn, k)
        for k in (
            "id",
            "conversation_id",
            "client_message_id",
            "user_message_id",
            "assistant_message_id",
            "status",
            "created_at",
            "started_at",
            "finished_at",
            "error",
        )
    }


async def _context(session, payload):
    context = payload.context.model_dump(exclude_none=True)
    if context.get("model_id") and not await session.get(
        ModelRecord, context["model_id"]
    ):
        raise HTTPException(404, detail="Model not found")
    for key, table in (("run_id", runningRecord), ("report_id", ReportRecord)):
        if context.get(key):
            item = await session.get(table, context[key])
            if not item:
                raise HTTPException(404, detail="Context resource not found")
            if context.get("model_id") and context["model_id"] != item.model_id:
                raise HTTPException(422, detail="Context belongs to another model")
            context["model_id"] = item.model_id
    return context


async def submit(session, conversation_id, payload, who):
    conversation = await get_conversation(session, conversation_id, who)
    if not payload.message.strip():
        raise HTTPException(422, detail="Message must not be blank")
    context = await _context(session, payload)
    identity = payload.client_message_id or str(uuid4())
    body_hash = digest({"message": payload.message, "context": context})
    prior = await session.scalar(
        select(ChatTurnRecord).where(
            ChatTurnRecord.conversation_id == conversation_id,
            ChatTurnRecord.client_message_id == identity,
        )
    )
    if prior:
        if prior.request_hash != body_hash:
            raise HTTPException(
                409, detail="Message ID already used for a different request"
            )
        return prior
    if conversation.task_id and conversation.context != context:
        raise HTTPException(
            409, detail="Start a new conversation when changing context"
        )
    if (
        not settings.openrouter_api_key
        or not settings.openrouter_api_key.get_secret_value()
    ):
        raise HTTPException(503, detail="Configure OPENROUTER_API_KEY for chat")
    pending = await session.scalar(
        select(ChatTurnRecord.id).where(
            ChatTurnRecord.conversation_id == conversation_id,
            ChatTurnRecord.status.in_(("awaiting_confirmation", "awaiting_review")),
        )
    )
    if pending:
        raise HTTPException(
            409, detail="An authenticated decision is required before another message"
        )
    changed = await session.execute(
        update(ConversationRecord)
        .where(
            ConversationRecord.id == conversation_id, ConversationRecord.busy.is_(False)
        )
        .values(busy=True, context=context, updated_at=now())
    )
    if changed.rowcount != 1:
        await session.rollback()
        raise HTTPException(409, detail="A chat turn is already running")
    turn = ChatTurnRecord(
        id=str(uuid4()),
        conversation_id=conversation_id,
        actor_id=who.id,
        actor_role=who.role,
        client_message_id=identity,
        request_hash=body_hash,
        user_message_id=str(uuid4()),
        assistant_message_id=str(uuid4()),
        context=context,
        resume_from=conversation.task_id,
    )
    session.add(turn)
    session.add(
        ChatMessageRecord(
            id=turn.user_message_id,
            conversation_id=conversation_id,
            role="user",
            content=payload.message,
            status="completed",
        )
    )
    session.add(
        ChatMessageRecord(
            id=turn.assistant_message_id,
            conversation_id=conversation_id,
            role="assistant",
            content="",
            status="queued",
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        prior = await session.scalar(
            select(ChatTurnRecord).where(
                ChatTurnRecord.conversation_id == conversation_id,
                ChatTurnRecord.client_message_id == identity,
            )
        )
        if prior and prior.request_hash == body_hash:
            return prior
        raise HTTPException(
            409, detail="Concurrent or conflicting message submission"
        ) from None
    return turn


def start_turn(turn_id, sessions=SessionFactory):
    """Retain a task reference outside request scope; route cancellation never reaches it."""
    if turn_id not in _tasks:
        task = asyncio.create_task(process_turn(turn_id, sessions))
        _tasks[turn_id] = task

        def finished(done):
            _tasks.pop(turn_id, None)
            if not done.cancelled():
                done.exception()  # consume failure; durable recovery handles an interrupted process

        task.add_done_callback(finished)


async def _event(session, turn_id, event, **data):
    session.add(ChatPublicEvent(turn_id=turn_id, payload={"event": event, **data}))


async def process_turn(turn_id, sessions):
    from contracts.harness import (
        HarnessRequest,
        Actor as HarnessActor,
        RequestContext,
        Resolution,
    )
    from app.services.tool_gateway import BackendGateway

    async with sessions() as session:
        claim = await session.execute(
            update(ChatTurnRecord)
            .where(ChatTurnRecord.id == turn_id, ChatTurnRecord.status == "queued")
            .values(status="running", started_at=now(), heartbeat_at=now())
        )
        await session.commit()
        if claim.rowcount != 1:
            return
        turn = await session.get(ChatTurnRecord, turn_id)
        message = await session.get(ChatMessageRecord, turn.user_message_id)
    cancel = asyncio.Event()
    status, text, error, task_id = "failed", "", None, None
    request = HarnessRequest(
        request_id=turn.id,
        actor=HarnessActor(id=turn.actor_id, role=turn.actor_role),
        kind="chat",
        prompt=message.content,
        context=RequestContext(
            policy_version="1.0.0", conversation_id=turn.conversation_id, **turn.context
        ),
        resume_from=turn.resume_from,
        resolution=Resolution(**turn.resolution) if turn.resolution else None,
    )

    async def emit(event):
        nonlocal task_id
        task_id = event.task_id
        async with sessions() as session:
            saved = await session.get(ChatTurnRecord, turn_id)
            saved.task_id, saved.heartbeat_at = task_id, now()
            # Public summaries are deliberately built here. Never forward raw args/output.
            if event.type == "token":
                token = event.data.get("text")
                if isinstance(token, str):
                    await _event(session, turn_id, "token", text=token[:16000])
            elif event.type == "tool_call":
                await _event(
                    session,
                    turn_id,
                    "tool_call",
                    text="Using a permitted platform tool…",
                )
            elif event.type == "confirmation_required":
                pending_id = event.data.get("pending_id")
                action = (
                    await session.get(PendingChatAction, pending_id)
                    if pending_id
                    else None
                )
                if action and action.turn_id == turn_id:
                    await _event(
                        session,
                        turn_id,
                        "confirmation_required",
                        pending_id=action.id,
                        text=f"Confirm {action.operation} for the selected model.",
                    )
                elif pending_id:
                    from app.services.proposal_service import ProposalRecord
                    proposal = await session.get(ProposalRecord, pending_id)
                    if proposal and proposal.conversation_id == turn.conversation_id:
                        await _event(session, turn_id, "review_required", proposal_id=proposal.id,
                                     text="Proposal recorded; nothing has started. An eligible user approves it on this card, then executes it as a separate step.")
            elif event.type == "review_required":
                await _event(
                    session,
                    turn_id,
                    "review_required",
                    text="Use the authenticated review queue to make a decision.",
                )
            elif event.type == "proposal_created":
                from app.services.proposal_service import ProposalRecord
                proposal = await session.get(ProposalRecord, event.data.get("proposal_id"))
                if proposal and proposal.conversation_id == turn.conversation_id:
                    await _event(session, turn_id, "review_required", proposal_id=proposal.id,
                                 text="Proposal recorded; nothing has started. An eligible user approves it on this card, then executes it as a separate step.")
            await session.commit()

    async def watcher():
        while True:
            async with sessions() as session:
                row = await session.get(ChatTurnRecord, turn_id)
                row.heartbeat_at = now()
                if row.cancel_requested:
                    cancel.set()
                await session.commit()
            await asyncio.sleep(0.5)

    watch = asyncio.create_task(watcher())
    try:
        async with asyncio.timeout(settings.run_timeout_seconds):
            async with runtime(sessions) as runner:
                result = await runner.run(
                    request, BackendGateway(request, sessions), emit, cancel
                )
                task_id, status = result.task_id, result.status
                text = result.text or ""
                if status == "failed":
                    error = "Agent could not complete this reply."
    except asyncio.CancelledError:
        status, error = "interrupted", "Chat execution interrupted by server shutdown."
        raise
    except TimeoutError:
        status, error = "failed", "Chat execution exceeded its time limit."
    except Exception:
        status, error = (
            "failed",
            "Chat execution failed; check provider configuration and retry deliberately.",
        )
    finally:
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)

        async def persist():
            async with sessions() as session:
                saved = await session.get(ChatTurnRecord, turn_id)
                saved.status, saved.task_id, saved.error, saved.finished_at = (
                    status,
                    task_id,
                    error,
                    now(),
                )
                conversation = await session.get(
                    ConversationRecord, turn.conversation_id
                )
                if task_id:
                    conversation.task_id = task_id
                conversation.busy, conversation.updated_at = False, now()
                message = await session.get(
                    ChatMessageRecord, turn.assistant_message_id
                )
                message.content, message.status = text, status
                session.add(
                    AuditRecord(
                        actor_id=turn.actor_id,
                        resource_id=turn.conversation_id,
                        action="chat_turn",
                        payload={
                            "turn_id": turn_id,
                            "task_id": task_id,
                            "status": status,
                        },
                    )
                )
                if error:
                    await _event(
                        session,
                        turn_id,
                        "error",
                        text=error,
                        code="chat_execution_failed",
                        retryable=True,
                    )
                await _event(
                    session,
                    turn_id,
                    "done",
                    status=status,
                    assistant_message_id=turn.assistant_message_id,
                    text=text,
                )
                await session.commit()

        await asyncio.shield(persist())


async def observe(turn_id, sessions=SessionFactory, after_seq=0):
    """SSE replay survives a lost browser response and never controls execution lifetime."""
    cursor, silent = after_seq, 0
    while True:
        async with sessions() as session:
            events = list(
                await session.scalars(
                    select(ChatPublicEvent)
                    .where(
                        ChatPublicEvent.turn_id == turn_id, ChatPublicEvent.seq > cursor
                    )
                    .order_by(ChatPublicEvent.seq)
                    .limit(100)
                )
            )
            turn = await session.get(ChatTurnRecord, turn_id)
        for event in events:
            cursor = event.seq
            yield f"id: {cursor}\ndata: " + json.dumps(event.payload) + "\n\n"
        if turn is None or (turn.status not in ACTIVE and not events):
            break
        silent = 0 if events else silent + 1
        if silent >= 40:
            yield ": heartbeat\n\n"
            silent = 0
        await asyncio.sleep(0.25)


async def cancel_turn(session, turn):
    if turn.status in ACTIVE:
        turn.cancel_requested = True
        await session.commit()
    return turn_read(turn)


async def startup(sessions=SessionFactory):
    """Single API process deployment: recover queued work and mark lost execution interrupted."""
    async with sessions() as session:
        running = list(
            await session.scalars(
                select(ChatTurnRecord).where(ChatTurnRecord.status == "running")
            )
        )
        for turn in running:
            if turn.id in _tasks:
                continue
            turn.status, turn.error, turn.finished_at = (
                "interrupted",
                "Server restarted during chat execution.",
                now(),
            )
            conversation = await session.get(ConversationRecord, turn.conversation_id)
            conversation.busy = False
            message = await session.get(ChatMessageRecord, turn.assistant_message_id)
            message.status = "interrupted"
            await _event(
                session,
                turn.id,
                "error",
                text=turn.error,
                code="chat_interrupted",
                retryable=True,
            )
            await _event(
                session,
                turn.id,
                "done",
                status="interrupted",
                assistant_message_id=message.id,
            )
        queued = list(
            await session.scalars(
                select(ChatTurnRecord.id).where(ChatTurnRecord.status == "queued")
            )
        )
        await session.commit()
    for turn_id in queued:
        start_turn(turn_id, sessions)


async def shutdown():
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def request_action(
    sessions, request, operation, idempotency_key, parameters=None
):
    from contracts.harness import ToolResult
    from app.services import run_service

    from app.db.chat_tables import ChatActionInput

    if operation == "monitor":
        from app.api.monitoring import SimulationRequest

        try:
            selected = SimulationRequest.model_validate(parameters or {})
            parameters = {
                "mode": "simulated",
                "window_label": selected.window_label,
                "shift": {"feature": selected.feature, "factor": selected.factor},
            }
        except ValueError:
            return ToolResult(
                status="denied",
                content="Specify the feature, numeric factor and window label for a declared monitoring simulation.",
            )
    elif parameters is not None:
        return ToolResult(status="denied", content="Unexpected operation parameters")

    if (
        operation not in ("document", "rebuild", "validate", "monitor")
        or not request.context.model_id
    ):
        return ToolResult(
            status="denied", content="Select a model and a supported operation first."
        )
    async with sessions() as session:
        turn = await session.get(ChatTurnRecord, request.request_id)
        if (
            not turn
            or turn.actor_id != request.actor.id
            or turn.context.get("model_id") != request.context.model_id
        ):
            return ToolResult(status="denied", content="No matching bound chat turn.")
        prior = await session.scalar(
            select(PendingChatAction).where(
                PendingChatAction.idempotency_key == idempotency_key
            )
        )
        if prior:
            if prior.turn_id != turn.id:
                return ToolResult(
                    status="denied", content="Action belongs to another turn."
                )
            return ToolResult(
                status="needs_confirmation",
                content="Explicit confirmation required.",
                pending_id=prior.id,
            )
        try:
            await run_service.authorize_operation(
                session,
                request.context.model_id,
                operation,
                Actor(request.actor.id, request.actor.role),
            )
        except HTTPException:
            return ToolResult(
                status="denied",
                content="This operation is not permitted in the current workflow.",
            )
        if operation == "monitor":
            from app.services.upload_service import InventoryRecord

            inventory = await session.get(InventoryRecord, request.context.model_id)
            if not inventory or parameters["shift"][
                "feature"
            ] not in inventory.manifest.get("feature_order", []):
                return ToolResult(
                    status="denied", content="Choose a feature from the uploaded model"
                )
        model = await session.get(ModelRecord, request.context.model_id)
        action = PendingChatAction(
            id=str(uuid4()),
            turn_id=turn.id,
            actor_id=request.actor.id,
            idempotency_key=idempotency_key,
            action_version=digest(
                {
                    "model": model.id,
                    "version": model.version,
                    "operation": operation,
                    "parameters": parameters,
                    "nonce": str(uuid4()),
                }
            ),
            operation=operation,
            model_id=model.id,
            model_version=model.version,
            dataset_id=model.dataset_id,
        )
        session.add(action)
        await session.flush()
        if parameters is not None:
            session.add(ChatActionInput(action_id=action.id, parameters=parameters))
        await session.commit()
        return ToolResult(
            status="needs_confirmation",
            content="The user must confirm this operation before a run is created.",
            pending_id=action.id,
        )


async def action_read(session, action):
    from app.db.chat_tables import ChatActionInput

    stored = await session.get(ChatActionInput, action.id)
    return {
        "parameters": stored.parameters if stored else None,
        "id": action.id,
        "turn_id": action.turn_id,
        "action_version": action.action_version,
        "status": action.status,
        "kind": "run",
        "operation": action.operation,
        "model_id": action.model_id,
        "model_version": action.model_version,
        "allowed_decisions": ["approved", "rejected"]
        if action.status == "pending"
        else [],
        "run_id": action.run_id,
        "continuation_id": action.continuation_id,
    }


async def get_action(session, conversation_id, action_id, who):
    await get_conversation(session, conversation_id, who)
    action = await session.get(PendingChatAction, action_id)
    turn = await session.get(ChatTurnRecord, action.turn_id) if action else None
    if (
        not action
        or action.actor_id != who.id
        or not turn
        or turn.conversation_id != conversation_id
    ):
        raise HTTPException(404, detail="Pending action not found")
    return action


async def decide_action(session, conversation_id, action_id, payload, who):
    from app.services import run_service
    from app.schemas.runs import RunCreate

    action = await get_action(session, conversation_id, action_id, who)
    decision_hash = digest(payload.model_dump())
    if action.status != "pending":
        if action.decision_hash == decision_hash:
            return action
        raise HTTPException(409, detail="Action already decided")
    if action.action_version != payload.action_version:
        raise HTTPException(409, detail="Action version changed")
    if not payload.rationale.strip():
        raise HTTPException(422, detail="Rationale must not be blank")
    parent = await session.get(ChatTurnRecord, action.turn_id)
    if parent.status != "awaiting_confirmation" or not parent.task_id:
        raise HTTPException(409, detail="Turn is not awaiting this confirmation")
    if payload.outcome == "approved":
        await run_service.authorize_operation(
            session, action.model_id, action.operation, who
        )
        model = await session.get(ModelRecord, action.model_id)
        if (
            model.version != action.model_version
            or model.dataset_id != action.dataset_id
        ):
            raise HTTPException(
                409, detail="Model changed after the action was proposed"
            )
    claimed = await session.execute(
        update(PendingChatAction)
        .where(PendingChatAction.id == action_id, PendingChatAction.status == "pending")
        .values(
            status="decided",
            outcome=payload.outcome,
            decision_hash=decision_hash,
            decision_id=payload.client_decision_id,
            rationale=payload.rationale,
            decided_at=now(),
        )
    )
    if claimed.rowcount != 1:
        await session.rollback()
        raise HTTPException(409, detail="Action already decided")
    run = None
    if payload.outcome == "approved":
        run = await run_service.createRun(
            session,
            action.model_id,
            RunCreate(
                operation=action.operation,
                model_version=action.model_version,
                dataset_id=action.dataset_id,
            ),
        )
        session.add(RunExecutionRecord(run_id=run.id, actor_id=who.id))
        if action.operation == "monitor":
            from app.db.chat_tables import ChatActionInput
            from app.services.monitoring_service import MonitorJob

            stored = await session.get(ChatActionInput, action.id)
            if stored is None:
                raise HTTPException(
                    409, detail="Monitoring parameters unavailable; propose again"
                )
            session.add(MonitorJob(run_id=run.id, payload=stored.parameters))
        action.run_id = run.id
    conversation = await session.get(ConversationRecord, conversation_id)
    conversation.busy = True
    parent.status = "resolved"
    continuation = ChatTurnRecord(
        id=str(uuid4()),
        conversation_id=conversation_id,
        actor_id=who.id,
        actor_role=who.role,
        client_message_id="decision:" + action.id,
        request_hash=decision_hash,
        user_message_id=str(uuid4()),
        assistant_message_id=str(uuid4()),
        context=parent.context,
        resume_from=parent.task_id,
        resolution={
            "pending_id": action.id,
            "outcome": payload.outcome,
            "detail": f"{payload.rationale}. Backend run ID: {run.id if run else 'none; rejected'}",
        },
    )
    action.continuation_id = continuation.id
    session.add(continuation)
    session.add(
        ChatMessageRecord(
            id=continuation.user_message_id,
            conversation_id=conversation_id,
            role="user",
            content="Explain the recorded confirmation outcome. Do not repeat the operation.",
            status="completed",
        )
    )
    session.add(
        ChatMessageRecord(
            id=continuation.assistant_message_id,
            conversation_id=conversation_id,
            role="assistant",
            content="",
            status="queued",
        )
    )
    session.add(
        AuditRecord(
            actor_id=who.id,
            resource_id=action.id,
            action="chat_action_decision",
            payload={
                "outcome": payload.outcome,
                "run_id": run.id if run else None,
                "rationale": payload.rationale,
            },
        )
    )
    await session.commit()
    if run:
        try:
            await asyncio.to_thread(run_service.publish_run, run.id)
        except Exception:
            await session.execute(
                update(runningRecord)
                .where(runningRecord.id == run.id, runningRecord.status == "queued")
                .values(
                    status="failed",
                    stage="dispatch_failed",
                    finished_at=now(),
                    error="Could not dispatch execution. Inspect run history before retrying.",
                )
            )
            await session.commit()
    start_turn(continuation.id)
    await session.refresh(action)
    return action
