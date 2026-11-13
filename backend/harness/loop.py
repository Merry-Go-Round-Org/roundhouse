"""One task's agent loop. Stands alone, so a worker can call `run_task` without a TaskManager.

The loop knows only `ToolGateway`. It does not know whether a sandbox sits behind it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Awaitable, Callable

from harness.config import ARCHIVE_KINDS, HarnessSettings
from harness.profiles import Profile, apply_tool_policy
from harness.proposals import PROPOSAL_TOOL_NAME, proposal_problems
from harness.schemas import (
    AgentEvent,
    AuditWriteFailed,
    EventType,
    GatewayFactory,
    LlmCallOutcome,
    LlmCallStart,
    LlmClient,
    LlmCompletion,
    LlmError,
    LlmRequest,
    NewMessage,
    SandboxUnavailable,
    StoredMessage,
    TaskResult,
    TaskSnapshot,
    TaskStatus,
    TextDelta,
    ToolCall,
    ToolGateway,
    ToolResult,
    UsageTotals,
)
from harness.store import UNKNOWN_RESULT_TEXT, SqlTaskStore

logger = logging.getLogger(__name__)

Emit = Callable[[AgentEvent], None]

INVALID_ARGUMENTS_TEXT = "Not executed: the arguments must be a valid JSON object."
NOT_ALLOWED_TEXT = "Denied: this tool is not available for this task."
REPEATED_DENIAL_TEXT = "Denied again: this call was already denied. Tell the user instead."
NOT_EXECUTED_TEXT = "Not executed: an earlier call in this batch paused the task."
PAUSED_CONFIRMATION_TEXT = "Paused: waiting for the user to confirm. The outcome will follow."
PAUSED_REVIEW_TEXT = "Paused: waiting for human review. The outcome will follow."
PROPOSAL_REFUSED_TEXT = "Not sent: the proposal is not valid. Fix it and try again: "


class Heartbeat:
    """Keeps a task's heartbeat fresh, so it is not mistaken for an orphan.

    Stops through a signal rather than `cancel()`, so a write is never interrupted halfway,
    and leaving the block waits for it, so nothing touches the database afterwards.
    """

    def __init__(self, store: SqlTaskStore, task_id: str, interval_s: float) -> None:
        self._store = store
        self._task_id = task_id
        self._interval_s = interval_s
        self._stop = asyncio.Event()
        self._beat: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Heartbeat:
        self._beat = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._beat is not None:
            await self._beat

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(), self._interval_s)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._store.touch(self._task_id)
            except Exception as exc:
                logger.warning("heartbeat failed for task %s: %s", self._task_id, type(exc).__name__)


class _Cancelled(Exception):
    pass


class _Failed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _End:
    def __init__(
        self,
        status: TaskStatus,
        *,
        error_code: str | None = None,
        pending_id: str | None = None,
        text: str | None = None,
    ) -> None:
        self.status = status
        self.error_code = error_code
        self.pending_id = pending_id
        self.text = text


class _StreamState:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.first_chunk_ms: int | None = None
        self.saw_token = False

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _parse_arguments(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _llm_message(message: StoredMessage) -> dict[str, Any]:
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content or ""}
    payload: dict[str, Any] = {"role": message.role, "content": message.content or ""}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    # Invalid arguments were never executed, and some providers reject them
                    # in history. The tool result already tells the LLM what went wrong.
                    "arguments": call.arguments if _parse_arguments(call.arguments) is not None else "{}",
                },
            }
            for call in message.tool_calls
        ]
    return payload


TRUNCATED_REPLY_NOTE = (
    "\n\n*(This reply was cut off at the length limit. Ask me to continue, or to answer "
    "more briefly.)*"
)


def _assistant_message(completion: LlmCompletion, keep_truncated_text: bool = False) -> NewMessage | None:
    """The message to save, or None. Empty replies and truncated tool calls are never saved.

    Truncated text is saved, with a note, only when `keep_truncated_text` is set: chat has
    already streamed it to the user, and a text reply executes nothing.
    """
    reason = completion.finish_reason
    # Some providers report `stop` with tool calls. Dropping the calls would lose work.
    if reason in ("tool_calls", "stop") and completion.tool_calls:
        return NewMessage(
            role="assistant",
            origin="llm",
            content=completion.text or None,
            tool_calls=completion.tool_calls,
        )
    if reason == "stop" and completion.text.strip():
        return NewMessage(role="assistant", origin="llm", content=completion.text)
    if (
        keep_truncated_text
        and reason == "length"
        and not completion.tool_calls
        and completion.text.strip()
    ):
        return NewMessage(
            role="assistant", origin="llm", content=completion.text + TRUNCATED_REPLY_NOTE
        )
    return None


class _TaskRun:
    def __init__(
        self,
        task_id: str,
        store: SqlTaskStore,
        llm: LlmClient,
        gateways: GatewayFactory,
        emit: Emit,
        cancel: asyncio.Event,
        settings: HarnessSettings,
    ) -> None:
        self.task_id = task_id
        self.store = store
        self.llm = llm
        self.gateways = gateways
        self.emit_event = emit
        self.cancel = cancel
        self.settings = settings
        self._event_seq = 0
        self._denied_calls: set[tuple[str, str]] = set()

    # ------------------------------------------------------------ plumbing
    def _emit(self, event_type: EventType, **data: Any) -> None:
        self._event_seq += 1
        event = AgentEvent(task_id=self.task_id, seq=self._event_seq, type=event_type, data=data)
        try:
            self.emit_event(event)
        except Exception as exc:  # a broken consumer must never stop the task
            logger.warning("event consumer failed for task %s: %s", self.task_id, type(exc).__name__)

    async def _race(self, work: Awaitable[Any]) -> Any:
        """Await `work`, interrupting it at once if the cancel event fires."""
        work_future = asyncio.ensure_future(work)
        waiter = asyncio.ensure_future(self.cancel.wait())
        try:
            await asyncio.wait({work_future, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if work_future.done():
                return work_future.result()
            raise _Cancelled()
        finally:
            waiter.cancel()
            if not work_future.done():
                work_future.cancel()
                await asyncio.gather(work_future, return_exceptions=True)

    async def _check_cancel(self) -> None:
        """Runs at each turn and tool boundary, to catch cancels from another process."""
        if self.cancel.is_set():
            raise _Cancelled()
        status, requested = await self.store.control(self.task_id)
        # Another process may have marked this task as interrupted. Stop working either way.
        if requested or status != "running":
            raise _Cancelled()

    # ------------------------------------------------------------ task lifecycle
    async def execute(self) -> TaskResult:
        if not await self.store.claim_task(self.task_id, self.settings.owner_id):
            snapshot = await self.store.get_task(self.task_id)
            self._emit("done", status=snapshot.status)
            return snapshot.to_result()

        self._emit("task_started")
        async with Heartbeat(self.store, self.task_id, self.settings.heartbeat_interval_s):
            try:
                end = await self._run(await self.store.get_task(self.task_id))
            except _Cancelled:
                end = _End("cancelled")
            except _Failed as failure:
                end = _End("failed", error_code=failure.code)
            except AuditWriteFailed:
                end = _End("failed", error_code="audit_write_failed")
            except SandboxUnavailable:
                end = _End("failed", error_code="sandbox_unavailable")
            except asyncio.CancelledError:
                await self._record(_End("cancelled"))
                raise
            except Exception as exc:
                # Log the type only: an exception message could quote tool or LLM content.
                logger.error("task %s failed: %s", self.task_id, type(exc).__name__)
                end = _End("failed", error_code="internal_error")
            return await self._record(end)

    async def _record(self, end: _End) -> TaskResult:
        try:
            await self.store.finish_task(
                self.task_id,
                end.status,
                error_code=end.error_code,
                pending_id=end.pending_id,
                result_text=end.text,
            )
            # Read back, because another process may have ended this task first.
            result = (await self.store.get_task(self.task_id)).to_result()
        except Exception as exc:
            # The task stays `running` with no heartbeat, so orphan recovery will end it.
            logger.error("could not record end of task %s: %s", self.task_id, type(exc).__name__)
            result = TaskResult(
                task_id=self.task_id,
                status="failed",
                error_code="audit_write_failed",
                usage=UsageTotals(),
            )
        if result.status == "failed":
            self._emit("error", code=result.error_code)
        self._emit("done", status=result.status)
        return result

    async def _run(self, task: TaskSnapshot) -> _End:
        profile = self.settings.profiles[task.kind]
        await self.store.repair_unresolved(self.task_id, task.thread_id)
        await self._check_cancel()
        async with self.gateways.open(task.to_info()) as gateway:
            tools = apply_tool_policy(profile, await self._race(gateway.list_tools()))
            allowed = {tool.name for tool in tools}
            tool_payload = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
            turn = 0
            while True:
                await self._check_cancel()
                await self._check_limits(task, profile, turn)
                turn += 1
                completion, assistant = await self._llm_turn(task, profile, turn, tool_payload)

                if completion.finish_reason == "length":
                    if assistant is not None and not assistant.tool_calls:
                        # A chat text reply cut off at the limit: keep it, with its note.
                        return _End("completed", text=assistant.content)
                    raise _Failed("output_truncated")
                if assistant is None:
                    raise _Failed(
                        "empty_response"
                        if completion.finish_reason == "stop"
                        else "unexpected_finish_reason"
                    )
                if not assistant.tool_calls:
                    return _End("completed", text=completion.text)
                paused = await self._run_tools(
                    gateway, allowed, task, profile, turn, assistant.tool_calls
                )
                if paused is not None:
                    return paused

    async def _check_limits(self, task: TaskSnapshot, profile: Profile, turn: int) -> None:
        if turn >= profile.max_turns:
            raise _Failed("turn_limit_exceeded")
        if await self.store.last_prompt_tokens(task.thread_id) > profile.max_prompt_tokens:
            raise _Failed("context_too_large")
        # Checked between turns, so a task can pass a limit by up to one turn.
        if (
            await self.store.task_cost(self.task_id) >= profile.task_budget_usd
            or await self.store.thread_cost(task.thread_id) >= profile.thread_budget_usd
            or await self.store.global_cost() >= self.settings.global_budget_usd
            or await self._archive_budget_spent(task)
        ):
            raise _Failed("budget_exhausted")

    async def _archive_budget_spent(self, task: TaskSnapshot) -> bool:
        """Survey and intake runs of one archive share one budget. Other tasks have none."""
        archive_id = task.context.archive_id
        if task.kind not in ARCHIVE_KINDS or not archive_id:
            return False
        cost = await self.store.archive_cost(archive_id, ARCHIVE_KINDS)
        return cost >= self.settings.archive_budget_usd

    # ------------------------------------------------------------ LLM calls
    async def _llm_turn(
        self, task: TaskSnapshot, profile: Profile, turn: int, tools: list[dict[str, Any]]
    ) -> tuple[LlmCompletion, NewMessage | None]:
        stored = await self.store.load_messages(task.thread_id)
        messages = [{"role": "system", "content": profile.system_prompt}]
        messages += [_llm_message(m) for m in stored]
        request = LlmRequest(
            model=self.settings.model,
            messages=messages,
            tools=tools,
            max_tokens=profile.max_tokens,
            reasoning_effort=profile.effort,
            provider_routing=self.settings.provider_routing,
            idle_timeout_s=profile.idle_timeout_s,
            turn_timeout_s=profile.turn_timeout_s,
        )
        start = LlmCallStart(
            task_id=self.task_id,
            thread_id=task.thread_id,
            actor_id=task.actor_id,
            turn=turn,
            attempt=1,
            requested_model=request.model,
            provider_routing=request.provider_routing,
            reasoning_effort=request.reasoning_effort,
            max_tokens=request.max_tokens,
            messages_hash=_hash(messages),
            tools_hash=_hash(tools),
            input_seq=stored[-1].seq if stored else 0,
        )
        retries = self.settings.max_llm_retries
        for attempt in range(1, retries + 2):
            if attempt > 1:
                await self._check_cancel()
            call_id = await self._audit_start(start.model_copy(update={"attempt": attempt}))
            self._emit("thinking", turn=turn, attempt=attempt)
            state = _StreamState()
            try:
                completion = await self._race(self._consume(request, state))
            except (_Cancelled, asyncio.CancelledError):
                await self._audit_finish(
                    call_id,
                    LlmCallOutcome(
                        status="cancelled",
                        cost_usd=self._estimate_cost(request),
                        cost_estimated=True,
                        first_chunk_ms=state.first_chunk_ms,
                        total_ms=state.elapsed_ms(),
                    ),
                )
                raise
            except LlmError as error:
                await self._audit_finish(call_id, self._failure_outcome(error, state, request))
                if not error.retryable:
                    raise _Failed("llm_request_rejected") from None
                # Chat streams tokens to the user, so a retry would repeat text they already
                # saw. Background tasks only keep the final completion, so they may retry.
                if (state.saw_token and profile.kind == "chat") or attempt > retries:
                    raise _Failed("llm_stream_error") from None
                delay = self.settings.retry_base_delay_s * 2 ** (attempt - 1)
                await self._race(asyncio.sleep(delay))
                continue
            return completion, await self._finish_success(
                call_id, completion, state, request, turn, keep_truncated_text=profile.kind == "chat"
            )
        raise AssertionError("unreachable")

    async def _consume(self, request: LlmRequest, state: _StreamState) -> LlmCompletion:
        stream = self.llm.stream(request)
        completion: LlmCompletion | None = None
        try:
            async for item in stream:
                if state.first_chunk_ms is None:
                    state.first_chunk_ms = state.elapsed_ms()
                if isinstance(item, TextDelta):
                    state.saw_token = True
                    self._emit("token", text=item.text)
                else:
                    completion = item
            if completion is None:
                raise LlmError("stream_ended_early", retryable=True)
            return completion
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()

    def _estimate_cost(self, request: LlmRequest) -> float:
        prompt_chars = len(json.dumps(request.messages)) + len(json.dumps(request.tools))
        return self.settings.prices.cost(prompt_chars // 4, request.max_tokens)

    def _failure_outcome(
        self, error: LlmError, state: _StreamState, request: LlmRequest
    ) -> LlmCallOutcome:
        return LlmCallOutcome(
            status="failed",
            http_status=error.http_status,
            error_code=error.code,
            cost_usd=0.0 if error.no_charge else self._estimate_cost(request),
            cost_estimated=not error.no_charge,
            first_chunk_ms=state.first_chunk_ms,
            total_ms=state.elapsed_ms(),
        )

    async def _finish_success(
        self,
        call_id: str,
        completion: LlmCompletion,
        state: _StreamState,
        request: LlmRequest,
        turn: int,
        *,
        keep_truncated_text: bool = False,
    ) -> NewMessage | None:
        usage = completion.usage
        if usage is None:
            cost, estimated = self._estimate_cost(request), True
        elif usage.cost_usd is None:
            prices = self.settings.prices
            cost = prices.cost(usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens)
            estimated = True
        else:
            cost, estimated = usage.cost_usd, False
        assistant = _assistant_message(completion, keep_truncated_text)
        outcome = LlmCallOutcome(
            status="completed",
            generation_id=completion.generation_id,
            returned_model=completion.model,
            provider=completion.provider,
            finish_reason=completion.finish_reason,
            http_status=completion.http_status,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            reasoning_tokens=usage.reasoning_tokens if usage else 0,
            cached_tokens=usage.cached_tokens if usage else 0,
            cost_usd=cost,
            cost_estimated=estimated,
            first_chunk_ms=state.first_chunk_ms,
            total_ms=state.elapsed_ms(),
        )
        await self._audit_finish(call_id, outcome, assistant)
        self._emit(
            "llm_turn",
            turn=turn,
            provider=completion.provider,
            finish_reason=completion.finish_reason,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
            reasoning_tokens=outcome.reasoning_tokens,
            cost_usd=cost,
            cost_estimated=estimated,
        )
        return assistant

    async def _audit_start(self, start: LlmCallStart) -> str:
        try:
            return await self.store.start_llm_call(start)
        except Exception as exc:
            logger.error("audit write failed for task %s: %s", self.task_id, type(exc).__name__)
            raise AuditWriteFailed() from None

    async def _audit_finish(
        self, call_id: str, outcome: LlmCallOutcome, assistant: NewMessage | None = None
    ) -> None:
        try:
            await self.store.finish_llm_call(call_id, outcome, assistant)
        except Exception as exc:
            logger.error("audit write failed for task %s: %s", self.task_id, type(exc).__name__)
            raise AuditWriteFailed() from None

    # ------------------------------------------------------------ tool calls
    async def _run_tools(
        self,
        gateway: ToolGateway,
        allowed: set[str],
        task: TaskSnapshot,
        profile: Profile,
        turn: int,
        calls: list[ToolCall],
    ) -> _End | None:
        """Run the calls in order. Returns how the task ends if a call pauses it."""
        paused: _End | None = None
        for index, call in enumerate(calls):
            if paused is not None:
                await self._save_tool_result(task, call, "not_executed", NOT_EXECUTED_TEXT)
                continue
            await self._check_cancel()
            args = _parse_arguments(call.arguments)
            self._emit("tool_call", call_id=call.id, name=call.name, arguments=args)
            if args is None:
                await self._save_tool_result(task, call, "error", INVALID_ARGUMENTS_TEXT)
                continue
            if call.name not in allowed:
                await self._save_tool_result(task, call, "denied", NOT_ALLOWED_TEXT)
                continue
            fingerprint = (call.name, json.dumps(args, sort_keys=True))
            if fingerprint in self._denied_calls:
                await self._save_tool_result(task, call, "denied", REPEATED_DENIAL_TEXT)
                continue
            if call.name == PROPOSAL_TOOL_NAME:
                problems = proposal_problems(profile.proposal_types, args)
                if problems:
                    # Not a denial: the agent may fix the proposal and send it again.
                    text = PROPOSAL_REFUSED_TEXT + "; ".join(problems[:10])
                    await self._save_tool_result(task, call, "error", self._limit(text))
                    continue

            key = f"{self.task_id}:{turn}:{index}"
            result = await self._call_gateway(gateway, call, args, key)
            if result.status == "needs_confirmation":
                paused = _End("awaiting_confirmation", pending_id=result.pending_id)
                self._emit(
                    "confirmation_required",
                    pending_id=result.pending_id,
                    name=call.name,
                    arguments=args,
                )
                await self._save_tool_result(task, call, result.status, PAUSED_CONFIRMATION_TEXT)
            elif result.status == "needs_review":
                paused = _End("awaiting_review", pending_id=result.pending_id)
                self._emit(
                    "review_required",
                    pending_id=result.pending_id,
                    name=call.name,
                    arguments=args,
                )
                await self._save_tool_result(task, call, result.status, PAUSED_REVIEW_TEXT)
            else:
                if result.status == "denied":
                    self._denied_calls.add(fingerprint)
                await self._save_tool_result(task, call, result.status, self._limit(result.content))
                if result.status == "ok" and result.proposal_id:
                    self._emit(
                        "proposal_created",
                        proposal_id=result.proposal_id,
                        name=call.name,
                        type=args.get("type"),
                    )
        return paused

    async def _call_gateway(
        self, gateway: ToolGateway, call: ToolCall, args: dict[str, Any], key: str
    ) -> ToolResult:
        try:
            return await self._race(gateway.call(call.name, args, key))
        except _Cancelled:
            raise
        except Exception as exc:
            logger.warning("tool %s failed in task %s: %s", call.name, self.task_id, type(exc).__name__)
            # The call may have taken effect, so the LLM must check before trying again.
            return ToolResult(status="error", content=UNKNOWN_RESULT_TEXT)

    def _limit(self, content: str) -> str:
        limit = self.settings.max_tool_result_chars
        if len(content) <= limit:
            return content
        omitted = len(content) - limit
        return f"{content[:limit]}\n[Truncated: {omitted} characters omitted.]"

    async def _save_tool_result(
        self, task: TaskSnapshot, call: ToolCall, status: str, content: str
    ) -> None:
        await self.store.append_messages(
            self.task_id,
            task.thread_id,
            [NewMessage(role="tool", origin="tool", content=content, tool_call_id=call.id)],
        )
        self._emit("tool_result", call_id=call.id, name=call.name, status=status, content=content)


async def run_task(
    task_id: str,
    *,
    store: SqlTaskStore,
    llm: LlmClient,
    gateways: GatewayFactory,
    emit: Emit,
    cancel: asyncio.Event,
    settings: HarnessSettings,
) -> TaskResult:
    """Run one queued task to an end state. Keeps its own heartbeat, so it needs no manager."""
    return await _TaskRun(task_id, store, llm, gateways, emit, cancel, settings).execute()
