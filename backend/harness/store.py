"""SqlTaskStore: all database access for the harness, using an injected session factory.

Transactions stay short and are never held across an LLM or tool call. Every write
transaction starts with a write statement, so SQLite takes its lock up front and
`busy_timeout` applies, rather than failing later when a read tries to upgrade.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness.db import AgentMessage, AgentTask, LlmCall
from harness.profiles import Profile
from harness.schemas import (
    ACTIVE_STATUSES,
    END_STATUSES,
    InvalidTaskRequest,
    LlmCallAlreadyFinalized,
    LlmCallOutcome,
    LlmCallStart,
    NewMessage,
    NotContinuable,
    Resolution,
    StoredMessage,
    TaskContext,
    TaskNotFound,
    TaskRequest,
    TaskSnapshot,
    TaskStatus,
    ThreadBusy,
    ToolCall,
    UsageTotals,
)

UNKNOWN_RESULT_TEXT = "Execution result unknown. Check the current state before retrying."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes. Everything the store writes is UTC.
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _snapshot(row: AgentTask) -> TaskSnapshot:
    return TaskSnapshot(
        id=row.id,
        thread_id=row.thread_id,
        parent_task_id=row.parent_task_id,
        workspace_id=row.workspace_id,
        request_id=row.request_id,
        actor_id=row.actor_id,
        actor_role=row.actor_role,
        kind=row.kind,
        policy_version=row.policy_version,
        context=TaskContext.model_validate_json(row.context_json),
        profile_name=row.profile_name,
        profile_hash=row.profile_hash,
        status=row.status,
        error_code=row.error_code,
        pending_id=row.pending_id,
        result_text=row.result_text,
        usage=UsageTotals(
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            reasoning_tokens=row.reasoning_tokens,
            cost_usd=row.cost_usd,
            unknown_cost_calls=row.unknown_cost_calls,
        ),
        cancel_requested=row.cancel_requested,
        owner_id=row.owner_id,
        heartbeat_at=_utc(row.heartbeat_at),
        created_at=_utc(row.created_at),
        started_at=_utc(row.started_at),
        finished_at=_utc(row.finished_at),
    )


def _stored(row: AgentMessage) -> StoredMessage:
    tool_calls = None
    if row.tool_calls_json:
        tool_calls = [ToolCall.model_validate(c) for c in json.loads(row.tool_calls_json)]
    return StoredMessage(
        thread_id=row.thread_id,
        seq=row.seq,
        task_id=row.task_id,
        role=row.role,
        origin=row.origin,
        content=row.content,
        tool_calls=tool_calls,
        tool_call_id=row.tool_call_id,
        created_at=_utc(row.created_at),
    )


def resolution_text(resolution: Resolution) -> str:
    # The decision is made on a card, which may sit in this chat: say how it was made, not
    # where, so the assistant never tells the user it happened elsewhere (Flow C retest).
    text = (
        f"Pending action {resolution.pending_id} was {resolution.outcome} by a person on its "
        "decision card. Typed chat text never makes this decision."
    )
    return f"{text} {resolution.detail}" if resolution.detail else text


class SqlTaskStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    # ------------------------------------------------------------ creating tasks
    async def find_by_request_id(self, request_id: str) -> str | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(AgentTask.id).where(AgentTask.request_id == request_id)
            )

    async def create_task(
        self, request: TaskRequest, profile: Profile, continue_from: str | None = None
    ) -> tuple[str, bool]:
        """Commit a `queued` task with its input message. Returns (task_id, created).

        Repairing unresolved tool calls and appending the new input share one transaction,
        so a tool result can never land after the user message that follows it.
        """
        if request.kind != profile.kind:
            raise InvalidTaskRequest(f"profile {profile.name} cannot run a {request.kind} task")
        if request.resolution is not None and continue_from is None:
            raise InvalidTaskRequest("a resolution can only be sent when continuing a task")

        existing = await self.find_by_request_id(request.request_id)
        if existing is not None:
            return existing, False

        inputs = self._input_messages(request, profile, continue_from)
        task_id = str(uuid4())
        try:
            async with self._sessions.begin() as session:
                parent = None
                if continue_from is not None:
                    parent = await self._parent(session, request, continue_from)
                thread_id = parent.thread_id if parent else task_id
                workspace_id = None
                if parent:
                    workspace_id = parent.workspace_id
                elif profile.sandbox:
                    workspace_id = str(uuid4())
                now = _now()
                session.add(
                    AgentTask(
                        id=task_id,
                        thread_id=thread_id,
                        parent_task_id=parent.id if parent else None,
                        workspace_id=workspace_id,
                        request_id=request.request_id,
                        actor_id=request.actor.id,
                        actor_role=request.actor.role,
                        kind=request.kind,
                        policy_version=request.context.policy_version,
                        context_json=request.context.model_dump_json(),
                        profile_name=profile.name,
                        profile_hash=profile.hash,
                        status="queued",
                        input_tokens=0,
                        output_tokens=0,
                        reasoning_tokens=0,
                        cost_usd=0.0,
                        unknown_cost_calls=0,
                        cancel_requested=False,
                        heartbeat_at=now,
                        created_at=now,
                    )
                )
                # The flush takes the write lock and enforces both unique indexes.
                await session.flush()
                if parent is not None:
                    await self._require_newest(session, parent.id, task_id)
                    await self._insert_repairs(session, task_id, thread_id)
                await self._insert_messages(session, task_id, thread_id, inputs)
        except IntegrityError:
            existing = await self.find_by_request_id(request.request_id)
            if existing is not None:
                return existing, False
            raise ThreadBusy("the thread already has an active task") from None
        return task_id, True

    @staticmethod
    def _input_messages(
        request: TaskRequest, profile: Profile, continue_from: str | None
    ) -> list[NewMessage]:
        messages: list[NewMessage] = []
        if request.resolution is not None:
            messages.append(
                NewMessage(
                    role="user", origin="resolution", content=resolution_text(request.resolution)
                )
            )
        prompt = request.prompt
        if prompt is None and not messages and continue_from is None:
            prompt = profile.default_prompt
        if prompt is not None:
            if not prompt.strip():
                raise InvalidTaskRequest("the prompt is empty")
            messages.append(NewMessage(role="user", origin="user", content=prompt))
        if not messages:
            raise InvalidTaskRequest("the task has no prompt or resolution")
        return messages

    async def _parent(
        self, session: AsyncSession, request: TaskRequest, parent_id: str
    ) -> AgentTask:
        parent = await session.get(AgentTask, parent_id)
        if parent is None:
            raise TaskNotFound(parent_id)
        if parent.actor_id != request.actor.id:
            raise NotContinuable("the thread belongs to another actor")
        if parent.kind != request.kind:
            raise NotContinuable("a continuation must have the same kind as its thread")
        active = await session.scalar(
            select(AgentTask.id)
            .where(AgentTask.thread_id == parent.thread_id, AgentTask.status.in_(ACTIVE_STATUSES))
            .limit(1)
        )
        if active is not None:
            raise ThreadBusy("the thread already has an active task")
        await self._require_newest(session, parent.id, None)
        resolution = request.resolution
        if resolution is not None and resolution.pending_id != parent.pending_id:
            raise NotContinuable("the resolution does not match the task's pending action")
        return parent

    @staticmethod
    async def _require_newest(session: AsyncSession, parent_id: str, new_id: str | None) -> None:
        # Threads are linear, so the newest task is the one nothing continues yet.
        query = select(AgentTask.id).where(AgentTask.parent_task_id == parent_id)
        if new_id is not None:
            query = query.where(AgentTask.id != new_id)
        if await session.scalar(query.limit(1)) is not None:
            raise NotContinuable("only the newest task in a thread can be continued")

    # ------------------------------------------------------------ reading tasks
    async def get_task(self, task_id: str) -> TaskSnapshot:
        async with self._sessions() as session:
            row = await session.get(AgentTask, task_id)
            if row is None:
                raise TaskNotFound(task_id)
            return _snapshot(row)

    async def control(self, task_id: str) -> tuple[str, bool]:
        """Return (status, cancel_requested), for checks at turn and tool boundaries."""
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(AgentTask.status, AgentTask.cancel_requested).where(
                        AgentTask.id == task_id
                    )
                )
            ).first()
            if row is None:
                raise TaskNotFound(task_id)
            return row[0], bool(row[1])

    async def list_thread_tasks(self, thread_id: str) -> list[TaskSnapshot]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentTask)
                .where(AgentTask.thread_id == thread_id)
                .order_by(AgentTask.created_at)
            )
            return [_snapshot(r) for r in rows]

    # ------------------------------------------------------------ state transitions
    async def _update_task(self, task_id: str, *where: Any, **values: Any) -> bool:
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(AgentTask).where(AgentTask.id == task_id, *where).values(**values)
            )
            return result.rowcount == 1

    async def claim_task(self, task_id: str, owner_id: str) -> bool:
        """Atomically move a task from queued to running."""
        now = _now()
        return await self._update_task(
            task_id,
            AgentTask.status == "queued",
            status="running",
            owner_id=owner_id,
            started_at=now,
            heartbeat_at=now,
        )

    async def finish_task(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        error_code: str | None = None,
        pending_id: str | None = None,
        result_text: str | None = None,
    ) -> bool:
        """End a running task. A task never returns to running, and an ended task stays ended."""
        if status not in END_STATUSES:
            raise ValueError(f"{status} is not an end state")
        return await self._update_task(
            task_id,
            AgentTask.status == "running",
            status=status,
            error_code=error_code,
            pending_id=pending_id,
            result_text=result_text,
            finished_at=_now(),
        )

    async def cancel_queued(self, task_id: str) -> bool:
        return await self._update_task(
            task_id, AgentTask.status == "queued", status="cancelled", finished_at=_now()
        )

    async def request_cancel(self, task_id: str) -> None:
        """Set the flag that other processes read at turn and tool boundaries."""
        await self.get_task(task_id)
        await self._update_task(
            task_id, AgentTask.status.in_(ACTIVE_STATUSES), cancel_requested=True
        )

    async def touch(self, task_id: str) -> bool:
        return await self._update_task(
            task_id, AgentTask.status.in_(ACTIVE_STATUSES), heartbeat_at=_now()
        )

    async def interrupt_orphans(self, older_than_s: float) -> int:
        """Mark active tasks whose heartbeat is too old as interrupted."""
        cutoff = _now() - timedelta(seconds=older_than_s)
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(AgentTask)
                .where(AgentTask.status.in_(ACTIVE_STATUSES), AgentTask.heartbeat_at < cutoff)
                .values(status="interrupted", finished_at=_now())
            )
            return result.rowcount

    # ------------------------------------------------------------ messages
    async def load_messages(self, thread_id: str) -> list[StoredMessage]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentMessage)
                .where(AgentMessage.thread_id == thread_id)
                .order_by(AgentMessage.seq)
            )
            return [_stored(r) for r in rows]

    async def append_messages(
        self, task_id: str, thread_id: str, messages: list[NewMessage]
    ) -> list[int]:
        async with self._sessions.begin() as session:
            await session.execute(
                update(AgentTask).where(AgentTask.id == task_id).values(heartbeat_at=_now())
            )
            return await self._insert_messages(session, task_id, thread_id, messages)

    async def repair_unresolved(self, task_id: str, thread_id: str) -> int:
        """Give every tool call with no saved result an "unknown" result. It is never re-run."""
        async with self._sessions.begin() as session:
            await session.execute(
                update(AgentTask).where(AgentTask.id == task_id).values(heartbeat_at=_now())
            )
            return await self._insert_repairs(session, task_id, thread_id)

    async def _insert_repairs(self, session: AsyncSession, task_id: str, thread_id: str) -> int:
        rows = await session.scalars(
            select(AgentMessage)
            .where(AgentMessage.thread_id == thread_id)
            .order_by(AgentMessage.seq)
        )
        # A list, not a set: some providers reuse tool call IDs across turns.
        unresolved: list[str] = []
        for message in map(_stored, rows):
            if message.tool_calls:
                unresolved.extend(call.id for call in message.tool_calls)
            if message.tool_call_id in unresolved:
                unresolved.remove(message.tool_call_id)
        repairs = [
            NewMessage(
                role="tool", origin="recovery", content=UNKNOWN_RESULT_TEXT, tool_call_id=call_id
            )
            for call_id in unresolved
        ]
        await self._insert_messages(session, task_id, thread_id, repairs)
        return len(repairs)

    @staticmethod
    async def _insert_messages(
        session: AsyncSession, task_id: str, thread_id: str, messages: list[NewMessage]
    ) -> list[int]:
        if not messages:
            return []
        last = await session.scalar(
            select(func.max(AgentMessage.seq)).where(AgentMessage.thread_id == thread_id)
        )
        seq = last or 0
        now = _now()
        result = []
        for message in messages:
            seq += 1
            tool_calls = None
            if message.tool_calls is not None:
                tool_calls = json.dumps([c.model_dump() for c in message.tool_calls])
            session.add(
                AgentMessage(
                    thread_id=thread_id,
                    seq=seq,
                    task_id=task_id,
                    role=message.role,
                    origin=message.origin,
                    content=message.content,
                    tool_calls_json=tool_calls,
                    tool_call_id=message.tool_call_id,
                    created_at=now,
                )
            )
            result.append(seq)
        await session.flush()
        return result

    # ------------------------------------------------------------ LLM audit
    async def start_llm_call(self, start: LlmCallStart) -> str:
        call_id = str(uuid4())
        async with self._sessions.begin() as session:
            session.add(
                LlmCall(
                    id=call_id,
                    task_id=start.task_id,
                    thread_id=start.thread_id,
                    actor_id=start.actor_id,
                    turn=start.turn,
                    attempt=start.attempt,
                    requested_model=start.requested_model,
                    provider_routing_json=json.dumps(start.provider_routing, sort_keys=True),
                    reasoning_effort=start.reasoning_effort,
                    max_tokens=start.max_tokens,
                    messages_hash=start.messages_hash,
                    tools_hash=start.tools_hash,
                    input_seq=start.input_seq,
                    prompt_tokens=0,
                    completion_tokens=0,
                    reasoning_tokens=0,
                    cached_tokens=0,
                    cost_usd=0.0,
                    cost_estimated=False,
                    status="started",
                    started_at=_now(),
                )
            )
        return call_id

    async def finish_llm_call(
        self, call_id: str, outcome: LlmCallOutcome, assistant: NewMessage | None = None
    ) -> None:
        """Finalize a call once, add it to the task totals and save its assistant message,
        all in one transaction. This is the only update the store offers on `llm_calls`.
        """
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(LlmCall)
                .where(LlmCall.id == call_id, LlmCall.status == "started")
                .values(**outcome.model_dump(), finished_at=_now())
                .returning(LlmCall.task_id, LlmCall.thread_id)
            )
            row = result.first()
            if row is None:
                raise LlmCallAlreadyFinalized(call_id)
            task_id, thread_id = row
            await session.execute(
                update(AgentTask)
                .where(AgentTask.id == task_id)
                .values(
                    input_tokens=AgentTask.input_tokens + outcome.prompt_tokens,
                    output_tokens=AgentTask.output_tokens + outcome.completion_tokens,
                    reasoning_tokens=AgentTask.reasoning_tokens + outcome.reasoning_tokens,
                    cost_usd=AgentTask.cost_usd + outcome.cost_usd,
                    unknown_cost_calls=AgentTask.unknown_cost_calls
                    + (1 if outcome.cost_estimated else 0),
                )
            )
            if assistant is not None:
                await self._insert_messages(session, task_id, thread_id, [assistant])

    async def list_llm_calls(self, thread_id: str) -> list[dict[str, Any]]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(LlmCall)
                .where(LlmCall.thread_id == thread_id)
                .order_by(LlmCall.started_at)
            )
            return [
                {c.name: getattr(row, c.name) for c in LlmCall.__table__.columns} for row in rows
            ]

    async def task_cost(self, task_id: str) -> float:
        return await self._sum_cost(LlmCall.task_id == task_id)

    async def thread_cost(self, thread_id: str) -> float:
        return await self._sum_cost(LlmCall.thread_id == thread_id)

    async def global_cost(self) -> float:
        return await self._sum_cost()

    async def archive_cost(self, archive_id: str, kinds: tuple[str, ...]) -> float:
        """Cost of every task of these kinds whose context names this archive.

        The archive ID lives in `context_json`, so a text match narrows the rows and the
        parsed context decides. No column is added, so existing databases keep working.
        """
        async with self._sessions() as session:
            rows = await session.execute(
                select(AgentTask.id, AgentTask.context_json).where(
                    AgentTask.kind.in_(kinds),
                    AgentTask.context_json.contains(archive_id, autoescape=True),
                )
            )
            task_ids = [
                task_id
                for task_id, context in rows
                if json.loads(context).get("archive_id") == archive_id
            ]
        if not task_ids:
            return 0.0
        return await self._sum_cost(LlmCall.task_id.in_(task_ids))

    async def _sum_cost(self, *where: Any) -> float:
        async with self._sessions() as session:
            total = await session.scalar(select(func.sum(LlmCall.cost_usd)).where(*where))
            return float(total or 0.0)

    async def last_prompt_tokens(self, thread_id: str) -> int:
        """`prompt_tokens` of the thread's latest completed call, across continuations."""
        async with self._sessions() as session:
            value = await session.scalar(
                select(LlmCall.prompt_tokens)
                .where(LlmCall.thread_id == thread_id, LlmCall.status == "completed")
                .order_by(LlmCall.started_at.desc())
                .limit(1)
            )
            return value or 0
