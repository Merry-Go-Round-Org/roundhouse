"""Fake platform tools and a scripted LLM, for offline tests and the scripted command line.

Production code must not import this package. An import rule test enforces that.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Sequence
from uuid import uuid4

from harness.profiles import SANDBOX_TOOL_NAMES
from harness.proposals import PROPOSAL_TOOL_NAME, proposal_tool_spec
from harness.schemas import (
    LlmCompletion,
    LlmRequest,
    LlmUsage,
    SandboxUnavailable,
    TaskInfo,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolSpec,
)

ScriptItem = TextDelta | LlmCompletion | Exception | Callable[[], Awaitable[None]]
Turn = Sequence[ScriptItem]

DEFAULT_USAGE = LlmUsage(prompt_tokens=100, completion_tokens=20, cost_usd=0.0001)


def text_turn(text: str, *, finish_reason: str = "stop", usage: LlmUsage | None = DEFAULT_USAGE) -> Turn:
    """A turn that streams `text` word by word and ends with `finish_reason`."""
    words = text.split(" ")
    deltas = [TextDelta(text=w if i == 0 else f" {w}") for i, w in enumerate(words) if text]
    return [
        *deltas,
        LlmCompletion(text=text, finish_reason=finish_reason, provider="scripted", model="scripted/model", generation_id=f"gen-{uuid4().hex[:8]}", usage=usage),
    ]


def tool_turn(*calls: tuple[str, Any], text: str = "", usage: LlmUsage | None = DEFAULT_USAGE) -> Turn:
    """A turn that requests tools. A `str` argument is sent as raw, possibly invalid, JSON."""
    tool_calls = [
        ToolCall(
            id=f"call_{uuid4().hex[:8]}",
            name=name,
            arguments=args if isinstance(args, str) else json.dumps(args),
        )
        for name, args in calls
    ]
    return [
        LlmCompletion(text=text, tool_calls=tool_calls, finish_reason="tool_calls", provider="scripted", model="scripted/model", generation_id=f"gen-{uuid4().hex[:8]}", usage=usage)
    ]


class ScriptedLlm:
    """Plays turns from a list, or from a function of the request. An item that is an
    exception is raised, and an async function is awaited, to simulate stalls.
    """

    def __init__(self, script: Iterable[Turn] | Callable[[LlmRequest], Turn]) -> None:
        self._next = script if callable(script) else None
        self._turns = None if callable(script) else iter(script)
        self.requests: list[LlmRequest] = []

    async def stream(self, request: LlmRequest) -> AsyncIterator[TextDelta | LlmCompletion]:
        self.requests.append(request)
        turn = self._next(request) if self._next else next(self._turns)
        for item in turn:
            if isinstance(item, Exception):
                raise item
            if callable(item):
                await item()
            else:
                yield item


def _tool(name: str, description: str, risk: str, **properties: dict[str, Any]) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        risk=risk,
        parameters={"type": "object", "properties": properties},
    )


PLATFORM_TOOLS = [
    _tool("get_model_status", "Read a model's status.", "read", model_id={"type": "string"}),
    _tool("update_model_owner", "Change a model's owner.", "write", owner={"type": "string"}),
    _tool("submit_review_decision", "Prepare a review decision.", "review", decision={"type": "string"}),
    _tool("delete_model", "Delete a model.", "write", model_id={"type": "string"}),
    _tool("start_rebuild", "Start a rebuild and return its task ID.", "write"),
    _tool("export_artifact", "Export a file as an official artifact.", "write", path={"type": "string"}),
    _tool("run_parity", "Run the parity interface on an artifact.", "read", artifact_id={"type": "string"}),
    _tool("request_review", "Ask for human review.", "review", summary={"type": "string"}),
    _tool("failing_tool", "Always fails.", "read"),
    _tool("exploding_tool", "Raises instead of returning a result.", "read"),
    _tool("huge_tool", "Returns a very long result.", "read"),
    proposal_tool_spec(),
]
SANDBOX_TOOLS = [
    _tool(name, f"Fake {name}.", "write", path={"type": "string"}, content={"type": "string"})
    for name in SANDBOX_TOOL_NAMES
]


class RecordedCall:
    def __init__(self, task: TaskInfo, name: str, args: dict[str, Any], key: str) -> None:
        self.task = task
        self.name = name
        self.args = args
        self.idempotency_key = key

    def __repr__(self) -> str:
        return f"RecordedCall({self.name}, {self.args}, {self.idempotency_key})"


class FakeGateway:
    def __init__(self, factory: FakeGatewayFactory, task: TaskInfo) -> None:
        self._factory = factory
        self._task = task

    async def list_tools(self) -> list[ToolSpec]:
        # Like the real gateway, sandbox tools exist only for tasks that have a workspace.
        tools = list(PLATFORM_TOOLS)
        if self._task.workspace_id:
            tools += SANDBOX_TOOLS
        return tools

    async def call(self, name: str, args: dict[str, Any], idempotency_key: str) -> ToolResult:
        self._factory.calls.append(RecordedCall(self._task, name, args, idempotency_key))
        if self._factory.on_call is not None:
            await self._factory.on_call(name)
        return self._result(name, args)

    def _result(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name == "update_model_owner":
            return ToolResult(status="needs_confirmation", content="Waiting.", pending_id=f"confirm-{uuid4().hex[:8]}")
        if name in ("submit_review_decision", "request_review"):
            return ToolResult(status="needs_review", content="Waiting.", pending_id=f"review-{uuid4().hex[:8]}")
        if name == "delete_model":
            return ToolResult(status="denied", content="You may not delete models.")
        if name == "failing_tool":
            return ToolResult(status="error", content="The tool failed.")
        if name == "exploding_tool":
            raise RuntimeError("internal gateway detail")
        if name == "huge_tool":
            return ToolResult(status="ok", content="x" * 50_000)
        if name == "start_rebuild":
            return ToolResult(status="ok", content=json.dumps({"task_id": "rebuild-task"}))
        if name == PROPOSAL_TOOL_NAME:
            proposal_id = f"proposal-{uuid4().hex[:8]}"
            return ToolResult(
                status="ok",
                content=json.dumps({"proposal_id": proposal_id, "status": "pending"}),
                proposal_id=proposal_id,
            )
        return ToolResult(status="ok", content=json.dumps({"tool": name, "args": args}))


class FakeGatewayFactory:
    def __init__(
        self,
        *,
        on_call: Callable[[str], Awaitable[None]] | None = None,
        sandbox_available: bool = True,
    ) -> None:
        self.calls: list[RecordedCall] = []
        self.opened: list[TaskInfo] = []
        self.closed: list[str] = []
        self.on_call = on_call
        self.sandbox_available = sandbox_available

    @asynccontextmanager
    async def open(self, task: TaskInfo) -> AsyncIterator[FakeGateway]:
        if task.workspace_id and not self.sandbox_available:
            raise SandboxUnavailable("docker is not available")
        self.opened.append(task)
        try:
            yield FakeGateway(self, task)
        finally:
            self.closed.append(task.task_id)
