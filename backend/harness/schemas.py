"""Types and interfaces of the harness core. Imports nothing from the backend."""
from __future__ import annotations

from datetime import datetime
from typing import Any, AsyncContextManager, AsyncIterator, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

TaskKind = Literal[
    "chat",
    "rebuild",
    "document",
    "survey",
    "intake",
    "validation",
    "triage",
    "investigation",
]
TaskStatus = Literal[
    "queued",
    "running",
    "completed",
    "awaiting_confirmation",
    "awaiting_review",
    "failed",
    "cancelled",
    "interrupted",
]
ErrorCode = Literal[
    "empty_response",
    "output_truncated",
    "unexpected_finish_reason",
    "llm_stream_error",
    "llm_request_rejected",
    "turn_limit_exceeded",
    "budget_exhausted",
    "context_too_large",
    "audit_write_failed",
    "sandbox_unavailable",
    "internal_error",
]
ReasoningEffort = Literal["low", "medium"]
MessageRole = Literal["user", "assistant", "tool"]
MessageOrigin = Literal["user", "llm", "tool", "resolution", "recovery"]
LlmCallStatus = Literal["started", "completed", "failed", "cancelled"]
EventType = Literal[
    "task_started",
    "thinking",
    "token",
    "tool_call",
    "tool_result",
    "confirmation_required",
    "review_required",
    "proposal_created",
    "llm_turn",
    "error",
    "done",
]

ACTIVE_STATUSES: tuple[str, ...] = ("queued", "running")
END_STATUSES: tuple[str, ...] = (
    "completed",
    "awaiting_confirmation",
    "awaiting_review",
    "failed",
    "cancelled",
    "interrupted",
)


# ---------------------------------------------------------------- errors
class HarnessError(Exception):
    """Base class for errors the harness raises to its caller."""


class HarnessBusy(HarnessError):
    """The queue is full or the manager is shutting down."""


class ThreadBusy(HarnessError):
    """The thread already has a queued or running task."""


class NotContinuable(HarnessError):
    """`continue_from` is not allowed: wrong owner, kind or pending ID, or not the newest task."""


class InvalidTaskRequest(HarnessError):
    """The request cannot start a task, for example because it has no input."""


class TaskNotFound(HarnessError):
    """No task has this ID."""


class SandboxUnavailable(HarnessError):
    """The sandbox could not be started. The task fails with `sandbox_unavailable`."""


class AuditWriteFailed(HarnessError):
    """An `llm_calls` row could not be written. The task makes no further LLM calls."""


class LlmCallAlreadyFinalized(HarnessError):
    """An `llm_calls` row can be finalized only once."""


class LlmError(Exception):
    """An LLM call failed. The message never contains the API key or reasoning content."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        http_status: int | None = None,
        retryable: bool = False,
        no_charge: bool = False,
    ) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        # The provider refused the request outright, so nothing can have been billed.
        self.no_charge = no_charge


# ---------------------------------------------------------------- requests
class Actor(BaseModel):
    id: str = Field(min_length=1)
    role: str = Field(min_length=1)


class TaskContext(BaseModel):
    """Supplied by the backend. None of these values are ever put into LLM messages."""

    policy_version: str = Field(min_length=1)
    conversation_id: str | None = None
    model_id: str | None = None
    run_id: str | None = None
    report_id: str | None = None
    model_package_id: str | None = None
    development_data_id: str | None = None
    # What the mount resolver uses for the AI-native kinds. The backend chooses them.
    archive_id: str | None = None
    candidate_id: str | None = None
    document_id: str | None = None
    metrics_bundle_id: str | None = None
    monitoring_run_id: str | None = None
    validation_report_id: str | None = None


class Resolution(BaseModel):
    """Outcome of a confirmation or review, sent by the backend when it resumes a task."""

    pending_id: str = Field(min_length=1)
    outcome: Literal["approved", "rejected"]
    detail: str | None = None


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)  # the same ID never creates a second task
    actor: Actor
    kind: TaskKind
    prompt: str | None = None
    context: TaskContext
    resolution: Resolution | None = None


class TaskInfo(BaseModel):
    """Trusted context handed to the gateway. The LLM can neither read nor set it."""

    task_id: str
    thread_id: str
    workspace_id: str | None
    kind: TaskKind
    actor_id: str
    actor_role: str
    policy_version: str
    run_id: str | None = None
    model_package_id: str | None = None
    development_data_id: str | None = None
    archive_id: str | None = None
    candidate_id: str | None = None
    document_id: str | None = None
    metrics_bundle_id: str | None = None
    monitoring_run_id: str | None = None
    validation_report_id: str | None = None


# ---------------------------------------------------------------- tools
class ToolSpec(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str
    parameters: dict[str, Any]  # JSON schema
    risk: Literal["read", "write", "review"]


class ToolResult(BaseModel):
    status: Literal["ok", "denied", "needs_confirmation", "needs_review", "error"]
    content: str
    pending_id: str | None = None
    # Set by the backend when the call created a proposal. Only an `ok` result can carry it.
    proposal_id: str | None = None

    @model_validator(mode="after")
    def _pending_requires_id(self) -> ToolResult:
        if self.status in ("needs_confirmation", "needs_review") and not self.pending_id:
            raise ValueError(f"{self.status} requires a pending_id")
        if self.proposal_id is not None and self.status != "ok":
            raise ValueError("only an ok result can carry a proposal_id")
        return self


class ToolGateway(Protocol):
    async def list_tools(self) -> list[ToolSpec]: ...

    async def call(self, name: str, args: dict[str, Any], idempotency_key: str) -> ToolResult: ...


class GatewayFactory(Protocol):
    def open(self, task: TaskInfo) -> AsyncContextManager[ToolGateway]: ...


# ---------------------------------------------------------------- LLM
class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str  # raw JSON text from the LLM, parsed only after the stream ends


class LlmUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float | None = None


class TextDelta(BaseModel):
    text: str


class LlmCompletion(BaseModel):
    """The last item of a stream. There is deliberately no field for reasoning content."""

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    provider: str | None = None
    model: str | None = None
    generation_id: str | None = None
    usage: LlmUsage | None = None
    http_status: int = 200


class LlmRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    max_tokens: int
    reasoning_effort: ReasoningEffort
    provider_routing: dict[str, Any]
    idle_timeout_s: float
    turn_timeout_s: float


class LlmClient(Protocol):
    def stream(self, request: LlmRequest) -> AsyncIterator[TextDelta | LlmCompletion]: ...


# ---------------------------------------------------------------- results and events
class UsageTotals(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    unknown_cost_calls: int = 0


class TaskResult(BaseModel):
    """Has no artifact IDs: `export_artifact` records in the backend are the source of truth."""

    task_id: str
    status: TaskStatus
    text: str | None = None
    error_code: str | None = None
    pending_id: str | None = None
    usage: UsageTotals = Field(default_factory=UsageTotals)


class TaskSnapshot(BaseModel):
    id: str
    thread_id: str
    parent_task_id: str | None
    workspace_id: str | None
    request_id: str
    actor_id: str
    actor_role: str
    kind: TaskKind
    policy_version: str
    context: TaskContext
    profile_name: str
    profile_hash: str
    status: TaskStatus
    error_code: str | None
    pending_id: str | None
    result_text: str | None
    usage: UsageTotals
    cancel_requested: bool
    owner_id: str | None
    heartbeat_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def to_info(self) -> TaskInfo:
        return TaskInfo(
            task_id=self.id,
            thread_id=self.thread_id,
            workspace_id=self.workspace_id,
            kind=self.kind,
            actor_id=self.actor_id,
            actor_role=self.actor_role,
            policy_version=self.policy_version,
            **self.context.model_dump(
                include={
                    "run_id",
                    "model_package_id",
                    "development_data_id",
                    "archive_id",
                    "candidate_id",
                    "document_id",
                    "metrics_bundle_id",
                    "monitoring_run_id",
                    "validation_report_id",
                }
            ),
        )

    def to_result(self) -> TaskResult:
        return TaskResult(
            task_id=self.id,
            status=self.status,
            text=self.result_text,
            error_code=self.error_code,
            pending_id=self.pending_id,
            usage=self.usage,
        )


class NewMessage(BaseModel):
    role: MessageRole
    origin: MessageOrigin
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


class StoredMessage(NewMessage):
    thread_id: str
    seq: int
    task_id: str
    created_at: datetime


class AgentEvent(BaseModel):
    task_id: str
    seq: int
    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)


class LlmCallStart(BaseModel):
    task_id: str
    thread_id: str
    actor_id: str
    turn: int
    attempt: int
    requested_model: str
    provider_routing: dict[str, Any]
    reasoning_effort: ReasoningEffort
    max_tokens: int
    messages_hash: str
    tools_hash: str
    input_seq: int


class LlmCallOutcome(BaseModel):
    status: Literal["completed", "failed", "cancelled"]
    generation_id: str | None = None
    returned_model: str | None = None
    provider: str | None = None
    finish_reason: str | None = None
    http_status: int | None = None
    error_code: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    cost_estimated: bool = False
    first_chunk_ms: int | None = None
    total_ms: int | None = None
