"""The bridge from `contracts/harness.py` to the harness core. The backend calls only this module.

`open_harness` belongs at the start of a process, or of a worker job, because the runner holds the
LLM connection pool and the concurrency limits. Its limits are per process.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts import harness as contract
from harness.config import HarnessSettings
from harness.llm import OpenRouterClient
from harness.manager import TaskManager
from harness.sandbox import (
    MountResolver,
    ProcessRunner,
    SandboxConfig,
    SandboxGatewayFactory,
    SandboxMounts,
    SandboxSlots,
    auto_sandbox_limit,
)
from harness.scoring import ScoringService
from harness.schemas import (
    Actor,
    AgentEvent,
    HarnessBusy,
    InvalidTaskRequest,
    LlmClient,
    NotContinuable,
    Resolution,
    SandboxUnavailable,
    TaskContext,
    TaskInfo,
    TaskNotFound,
    TaskRequest,
    TaskResult,
    ThreadBusy,
    ToolGateway,
    ToolResult,
    ToolSpec,
)
from harness.store import SqlTaskStore

logger = logging.getLogger(__name__)

# Cost and provider details stay in `llm_calls`. They are not for callers.
_INTERNAL_EVENTS = {"llm_turn"}


class _BackendTools:
    """The backend's gateway, in the core's types. A malformed answer raises, so the core treats
    the call as having an unknown result rather than trusting it.
    """

    def __init__(self, tools: contract.ToolGateway) -> None:
        self._tools = tools

    async def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec.model_validate(spec.model_dump()) for spec in await self._tools.list_tools()]

    async def call(self, name: str, args: dict[str, Any], idempotency_key: str) -> ToolResult:
        result = await self._tools.call(name, args, idempotency_key)
        return ToolResult.model_validate(result.model_dump())


class _OneGateway:
    """Gives one task the gateway that was passed for its request."""

    def __init__(self, tools: contract.ToolGateway) -> None:
        self._tools = _BackendTools(tools)

    @asynccontextmanager
    async def open(self, task: TaskInfo) -> AsyncIterator[ToolGateway]:
        yield self._tools


class _NoGateway:
    """Every task passes its own gateway, so reaching this is a bug."""

    def open(self, task: TaskInfo) -> Any:
        raise RuntimeError("a task must be spawned with its own gateway")


async def _no_mounts(task: TaskInfo) -> SandboxMounts:
    raise SandboxUnavailable("no mount resolver is configured")


def _task_request(request: contract.HarnessRequest) -> TaskRequest:
    resolution = request.resolution
    return TaskRequest(
        request_id=request.request_id,
        actor=Actor(id=request.actor.id, role=request.actor.role),
        kind=request.kind,
        prompt=request.prompt,
        context=TaskContext(**request.context.model_dump()),
        resolution=Resolution(**resolution.model_dump()) if resolution else None,
    )


def _harness_result(result: TaskResult) -> contract.HarnessResult:
    return contract.HarnessResult(
        status=result.status,
        task_id=result.task_id,
        text=result.text,
        error_code=result.error_code,
        checkpoint_id=result.pending_id,
        usage=contract.HarnessUsage(**result.usage.model_dump()),
    )


class HarnessRunner:
    """Implements `contracts.harness.Harness`."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        llm: LlmClient,
        *,
        settings: HarnessSettings | None = None,
        resolve_mounts: MountResolver | None = None,
        sandbox_config: SandboxConfig | None = None,
        process_runner: ProcessRunner | None = None,
    ) -> None:
        self._settings = settings or HarnessSettings()
        self._manager = TaskManager(SqlTaskStore(sessions), llm, _NoGateway(), self._settings)
        self._resolve_mounts = resolve_mounts or _no_mounts
        self._sandbox_config = sandbox_config or SandboxConfig()
        self._process_runner = process_runner
        # One limit for every container this runner starts, agent or scoring.
        self.sandbox_slots = SandboxSlots(
            self._settings.max_concurrent_sandboxes or auto_sandbox_limit(self._sandbox_config)
        )

    async def run(
        self,
        request: contract.HarnessRequest,
        tools: contract.ToolGateway,
        emit: contract.Emit,
        cancel: contract.CancelSignal,
    ) -> contract.HarnessResult:
        task_id = await self._spawn(request, tools)
        # Register before anything can yield, so no event is missed.
        events = self._manager.subscribe(task_id)
        watcher = asyncio.create_task(self._cancel_when_signalled(task_id, cancel))
        try:
            await self._forward(events, emit)
            return _harness_result(await self._manager.wait(task_id))
        except asyncio.CancelledError:
            # The caller gave up, for example on a timeout, so the task must not run on unseen.
            await asyncio.shield(self._manager.cancel(task_id))
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def recover_orphans(self) -> int:
        return await self._manager.recover_orphans()

    def scoring_service(self, work_dir: str | None = None) -> ScoringService:
        """The scoring service, sharing this runner's container limit with agent tasks."""
        return ScoringService(
            config=self._sandbox_config,
            runner=self._process_runner,
            slots=self.sandbox_slots,
            work_dir=work_dir,
        )

    def share_limits(self, sandboxes: SandboxSlots, llm: asyncio.Semaphore) -> None:
        self.sandbox_slots = sandboxes
        self._manager.share_llm_slots(llm)

    async def shutdown(self) -> None:
        await self._manager.shutdown()

    async def _spawn(self, request: contract.HarnessRequest, tools: contract.ToolGateway) -> str:
        gateways = SandboxGatewayFactory(
            _OneGateway(tools),
            self._resolve_mounts,
            config=self._sandbox_config,
            runner=self._process_runner,
            profiles=self._settings.profiles,
            slots=self.sandbox_slots,
        )
        try:
            return await self._manager.spawn(
                _task_request(request), continue_from=request.resume_from, gateways=gateways
            )
        except HarnessBusy:
            raise contract.HarnessRejected("busy", retryable=True) from None
        except ThreadBusy:
            raise contract.HarnessRejected("thread_busy", retryable=True) from None
        except (NotContinuable, TaskNotFound):
            raise contract.HarnessRejected("not_continuable") from None
        except InvalidTaskRequest:
            raise contract.HarnessRejected("invalid_request") from None

    async def _cancel_when_signalled(self, task_id: str, cancel: contract.CancelSignal) -> None:
        await cancel.wait()
        await self._manager.cancel(task_id)

    @staticmethod
    async def _forward(events: AsyncIterator[AgentEvent], emit: contract.Emit) -> None:
        """A failed `emit`, such as a closed browser, stops the events but never the task."""
        emitting = True
        async for event in events:
            if not emitting or event.type in _INTERNAL_EVENTS:
                continue
            try:
                await emit(contract.HarnessEvent(**event.model_dump()))
            except Exception as exc:
                logger.warning("emit failed for task %s: %s", event.task_id, type(exc).__name__)
                emitting = False


@asynccontextmanager
async def open_harness(
    sessions: async_sessionmaker[AsyncSession],
    api_key: str,
    *,
    settings: HarnessSettings | None = None,
    resolve_mounts: MountResolver | None = None,
    sandbox_config: SandboxConfig | None = None,
) -> AsyncIterator[HarnessRunner]:
    """Opens a runner with the real LLM client. The harness tables must already exist."""
    settings = settings or HarnessSettings()
    llm = OpenRouterClient(api_key, settings)
    runner = HarnessRunner(
        sessions,
        llm,
        settings=settings,
        resolve_mounts=resolve_mounts,
        sandbox_config=sandbox_config,
    )
    try:
        await runner.recover_orphans()
        yield runner
    finally:
        try:
            await runner.shutdown()
        finally:
            await llm.aclose()
