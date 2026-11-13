"""Spawn, cancel, concurrency, recovery and events, for many tasks in one process.

All limits are per process. Several worker processes multiply them, so the values must be
set from the agreed worker count.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import AsyncIterator

from harness.config import HarnessSettings
from harness.llm import LimitedLlm
from harness.loop import Heartbeat, run_task
from harness.schemas import (
    AgentEvent,
    GatewayFactory,
    HarnessBusy,
    LlmClient,
    TaskKind,
    TaskRequest,
    TaskResult,
    TaskSnapshot,
)
from harness.store import SqlTaskStore

logger = logging.getLogger(__name__)


class _Subscriber:
    """A bounded queue. It never blocks the loop, drops `token` events first, keeps `done`."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._events: deque[AgentEvent] = deque()
        self._ready = asyncio.Event()

    def push(self, event: AgentEvent) -> None:
        if len(self._events) >= self._capacity and not self._make_room(event):
            return
        self._events.append(event)
        self._ready.set()

    def _make_room(self, incoming: AgentEvent) -> bool:
        if incoming.type == "token":
            return False
        for queued in self._events:
            if queued.type == "token":
                self._events.remove(queued)
                return True
        # Only important events are queued. `done` still gets in, at the oldest one's expense.
        if incoming.type == "done":
            self._events.popleft()
            return True
        return False

    async def __aiter__(self) -> AsyncIterator[AgentEvent]:
        while True:
            while self._events:
                event = self._events.popleft()
                yield event
                if event.type == "done":
                    return
            self._ready.clear()
            await self._ready.wait()


class _LocalTask:
    """Mutable state of one task in this process. Never kept in module globals."""

    def __init__(self, task_id: str, kind: TaskKind, gateways: GatewayFactory) -> None:
        self.task_id = task_id
        self.kind = kind
        self.gateways = gateways
        self.cancel = asyncio.Event()
        self.subscribers: list[_Subscriber] = []
        self.runner: asyncio.Task[None] | None = None  # the strong reference until it ends

    def emit(self, event: AgentEvent) -> None:
        for subscriber in self.subscribers:
            subscriber.push(event)


class TaskManager:
    def __init__(
        self,
        store: SqlTaskStore,
        llm: LlmClient,
        gateways: GatewayFactory,
        settings: HarnessSettings | None = None,
    ) -> None:
        self._store = store
        self._gateways = gateways
        self._settings = settings or HarnessSettings()
        self._llm = LimitedLlm(llm, self._settings.llm_concurrency)
        self._chat_slots = asyncio.Semaphore(self._settings.chat_concurrency)
        self._background_slots = asyncio.Semaphore(self._settings.background_concurrency)
        self._tasks: dict[str, _LocalTask] = {}
        self._closing = False

    def share_llm_slots(self, slots: asyncio.Semaphore) -> None:
        self._llm.share_slots(slots)

    # ------------------------------------------------------------ public interface
    async def spawn(
        self,
        request: TaskRequest,
        continue_from: str | None = None,
        *,
        gateways: GatewayFactory | None = None,
    ) -> str:
        """Commit a queued task and start it. Returns before the task finishes.

        `gateways` replaces the manager's factory for this task, so a caller can bind its own tools.
        """
        existing = await self._store.find_by_request_id(request.request_id)
        if existing is not None:
            return existing
        self._check_capacity(request.kind)

        profile = self._settings.profiles[request.kind]
        task_id, created = await self._store.create_task(request, profile, continue_from)
        if created:
            # Capacity can have changed during the await, but `max_queued` is a safety limit
            # rather than an exact count, so a task that is already committed still runs.
            local = _LocalTask(task_id, request.kind, gateways or self._gateways)
            self._tasks[task_id] = local
            local.runner = asyncio.create_task(self._run(local))
            local.runner.add_done_callback(lambda _done: self._tasks.pop(task_id, None))
        return task_id

    async def cancel(self, task_id: str) -> None:
        await self._store.request_cancel(task_id)  # also reaches other processes
        local = self._tasks.get(task_id)
        if local is not None:
            local.cancel.set()

    async def get(self, task_id: str) -> TaskSnapshot:
        return await self._store.get_task(task_id)

    def subscribe(self, task_id: str) -> AsyncIterator[AgentEvent]:
        """Register now and read later, so nothing is missed between spawn and iteration.

        `done` is always the last event. A client that needs more after a gap in `seq`
        reloads from the database, which is the source of truth.
        """
        local = self._tasks.get(task_id)
        if local is None:
            return self._follow_from_store(task_id)
        subscriber = _Subscriber(self._settings.event_queue_size)
        local.subscribers.append(subscriber)
        return subscriber.__aiter__()

    async def wait(self, task_id: str) -> TaskResult:
        return (await self._wait_ended(task_id)).to_result()

    async def recover_orphans(self) -> int:
        """Mark queued and running tasks with a stale heartbeat as interrupted."""
        return await self._store.interrupt_orphans(self._settings.orphan_timeout_s)

    async def shutdown(self) -> None:
        """Refuse new tasks, cancel the running ones and wait for their final state."""
        self._closing = True
        locals_now = list(self._tasks.values())
        # Signal all of them before any await, so a queued task cannot take a slot that a
        # cancelled task has just freed.
        for local in locals_now:
            local.cancel.set()
        for local in locals_now:
            try:
                await self._store.request_cancel(local.task_id)
            except Exception as exc:
                logger.warning("could not flag task %s: %s", local.task_id, type(exc).__name__)
        runners = [local.runner for local in locals_now if local.runner is not None]
        if runners:
            await asyncio.gather(*runners, return_exceptions=True)

    # ------------------------------------------------------------ capacity
    def _pool(self, kind: TaskKind) -> tuple[asyncio.Semaphore, int]:
        if kind == "chat":
            return self._chat_slots, self._settings.chat_concurrency
        return self._background_slots, self._settings.background_concurrency

    def _active(self, chat: bool) -> int:
        return sum(1 for t in self._tasks.values() if (t.kind == "chat") == chat)

    def _check_capacity(self, kind: TaskKind) -> None:
        if self._closing:
            raise HarnessBusy("the harness is shutting down")
        chat_limit = self._settings.chat_concurrency
        background_limit = self._settings.background_concurrency
        active_chat, active_background = self._active(True), self._active(False)
        # A pool with a free slot can start this task now, so a backlog in the other pool
        # never blocks it.
        if kind == "chat" and active_chat < chat_limit:
            return
        if kind != "chat" and active_background < background_limit:
            return
        queued = max(active_chat - chat_limit, 0) + max(active_background - background_limit, 0)
        if queued >= self._settings.max_queued:
            raise HarnessBusy(f"{queued} tasks are already queued")

    # ------------------------------------------------------------ running a task
    async def _run(self, local: _LocalTask) -> None:
        slots, _limit = self._pool(local.kind)
        try:
            if not await self._wait_for_slot(local, slots):
                await self._store.cancel_queued(local.task_id)
                snapshot = await self._store.get_task(local.task_id)
                local.emit(_done_event(snapshot))
                return
            try:
                await run_task(
                    local.task_id,
                    store=self._store,
                    llm=self._llm,
                    gateways=local.gateways,
                    emit=local.emit,
                    cancel=local.cancel,
                    settings=self._settings,
                )
            finally:
                slots.release()
        except Exception as exc:
            # run_task handles task failures itself, so this is a database failure at most.
            # The row stays active with no heartbeat, and orphan recovery will end it.
            logger.error("task %s could not finish: %s", local.task_id, type(exc).__name__)
            local.emit(
                AgentEvent(task_id=local.task_id, seq=0, type="done", data={"status": "failed"})
            )

    async def _wait_for_slot(self, local: _LocalTask, slots: asyncio.Semaphore) -> bool:
        """Wait for a slot, or return False if the task was cancelled while queued.

        A queued task keeps its heartbeat here, or a long backlog would look like orphans.
        """
        acquire = asyncio.ensure_future(slots.acquire())
        cancelled = asyncio.ensure_future(local.cancel.wait())
        try:
            async with Heartbeat(
                self._store, local.task_id, self._settings.heartbeat_interval_s
            ):
                await asyncio.wait({acquire, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if not cancelled.done():
                return True
            if acquire.done() and not acquire.cancelled():
                slots.release()
            return False
        finally:
            cancelled.cancel()
            if not acquire.done():
                acquire.cancel()

    # ------------------------------------------------------------ tasks in other processes
    async def _wait_ended(self, task_id: str) -> TaskSnapshot:
        local = self._tasks.get(task_id)
        if local is not None and local.runner is not None:
            # Shield it, so that a caller who stops waiting does not cancel the task.
            await asyncio.shield(asyncio.gather(local.runner, return_exceptions=True))
        while True:
            snapshot = await self._store.get_task(task_id)
            if not snapshot.is_active:
                return snapshot
            await asyncio.sleep(self._settings.wait_poll_interval_s)

    async def _follow_from_store(self, task_id: str) -> AsyncIterator[AgentEvent]:
        yield _done_event(await self._wait_ended(task_id))


def _done_event(snapshot: TaskSnapshot) -> AgentEvent:
    return AgentEvent(task_id=snapshot.id, seq=0, type="done", data={"status": snapshot.status})
