"""The boundary between the backend and the agent harness.

The backend calls `Harness.run` with one request and a gateway bound to that request's actor.
Neither side's internal types appear here.
"""
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Actor(BaseModel):
    id: str = Field(min_length=1)
    role: str = Field(min_length=1)


class RequestContext(BaseModel):
    """Trusted values chosen by the backend. They are never put into LLM messages."""

    policy_version: str = Field(min_length=1)
    conversation_id: str | None = None
    model_id: str | None = None
    run_id: str | None = None
    report_id: str | None = None
    # What `resolve_mounts` uses to choose the model package and data for a sandbox.
    model_package_id: str | None = None
    development_data_id: str | None = None
    # Inputs of the AI-native kinds, also used by `resolve_mounts`.
    archive_id: str | None = None  # survey and intake
    candidate_id: str | None = None  # intake: the survey's model candidate
    document_id: str | None = None  # validation and investigation: the model document
    metrics_bundle_id: str | None = None  # validation: the scoring service's metrics bundle
    monitoring_run_id: str | None = None  # triage and investigation
    validation_report_id: str | None = None  # investigation: the latest validation report


class Resolution(BaseModel):
    """A human's decision on a pause, sent with `resume_from`. Chat text never produces one."""

    pending_id: str = Field(min_length=1)
    outcome: Literal["approved", "rejected"]
    detail: str | None = None


class HarnessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)  # the same ID never starts a second task
    actor: Actor
    kind: Literal[
        "chat", "rebuild", "document", "survey", "intake", "validation", "triage", "investigation"
    ]
    prompt: str | None = None  # a new rebuild or document may omit it, and chat needs it
    context: RequestContext
    resume_from: str | None = None  # the `task_id` of the previous result in this conversation
    resolution: Resolution | None = None


class ToolSpec(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str
    parameters: dict[str, Any]  # JSON schema
    risk: Literal["read", "write", "review"]


class ToolResult(BaseModel):
    status: Literal["ok", "denied", "needs_confirmation", "needs_review", "error"]
    content: str
    pending_id: str | None = None  # required for needs_confirmation and needs_review
    proposal_id: str | None = None  # set on an ok result that recorded a proposal

    @model_validator(mode="after")
    def _matches_harness_rules(self) -> "ToolResult":
        # The harness rejects any other shape and reports the call's result as unknown, even
        # when the backend already committed its effect. Fail where the result is built.
        if self.status in ("needs_confirmation", "needs_review") and not self.pending_id:
            raise ValueError(f"{self.status} requires a pending_id")
        if self.proposal_id is not None and self.status != "ok":
            raise ValueError("only an ok result can carry a proposal_id")
        return self


class ToolGateway(Protocol):
    """Holds no business rules. It calls the same services as the API, as the request's actor."""

    async def list_tools(self) -> list[ToolSpec]: ...

    async def call(self, name: str, args: dict[str, Any], idempotency_key: str) -> ToolResult: ...


class HarnessEvent(BaseModel):
    """`token` is reply text for the user. `tool_result` can hold data the actor may see but the
    page may not want to show, so the backend chooses which events reach the frontend.
    """

    task_id: str
    seq: int
    type: Literal[
        "task_started",
        "thinking",
        "token",
        "tool_call",
        "tool_result",
        "confirmation_required",
        "review_required",
        "proposal_created",
        "error",
        "done",
    ]
    data: dict[str, Any] = Field(default_factory=dict)


Emit = Callable[[HarnessEvent], Awaitable[None]]


class CancelSignal(Protocol):
    """`asyncio.Event` satisfies this."""

    def is_set(self) -> bool: ...

    async def wait(self) -> Any: ...


class HarnessUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    unknown_cost_calls: int = 0


class HarnessResult(BaseModel):
    status: Literal[
        "completed", "awaiting_confirmation", "awaiting_review", "failed", "cancelled", "interrupted"
    ]
    task_id: str | None = None  # send as `resume_from` to continue
    text: str | None = None
    error_code: str | None = None  # set when status is "failed"
    checkpoint_id: str | None = None  # send as `Resolution.pending_id` after the human decision
    artifact_ids: list[str] = Field(default_factory=list)  # filled by the backend, from its records
    proposal_ids: list[str] = Field(default_factory=list)  # filled by the backend, by source run
    usage: HarnessUsage = Field(default_factory=HarnessUsage)


class HarnessRejected(Exception):
    """The request did not start a task. A task that started and failed returns a result instead."""

    def __init__(
        self,
        code: Literal["busy", "thread_busy", "not_continuable", "invalid_request"],
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class Harness(Protocol):
    async def run(
        self, request: HarnessRequest, tools: ToolGateway, emit: Emit, cancel: CancelSignal
    ) -> HarnessResult:
        """Runs one task to its end or its pause, and raises `HarnessRejected` if it cannot start."""
        ...
