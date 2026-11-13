"""SSE compatibility plus durable chat retrieval and authenticated action decisions."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class ChatContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str | None = None
    run_id: str | None = None
    report_id: str | None = None


class ChatMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=10000)
    context: ChatContext = Field(default_factory=ChatContext)
    client_message_id: str | None = Field(default=None, min_length=1, max_length=120)


class ChatDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_decision_id: str = Field(min_length=1, max_length=120)
    action_version: str = Field(min_length=64, max_length=64)
    outcome: Literal["approved", "rejected"]
    rationale: str = Field(min_length=1, max_length=4000)


class ChatEvent(BaseModel):
    event: Literal[
        "token",
        "evidence",
        "tool_call",
        "confirmation_required",
        "review_required",
        "proposal_created",
        "done",
        "error",
    ]
    text: str | None = None
    artifact_id: str | None = None
    assistant_message_id: str | None = None
    pending_id: str | None = None
    proposal_id: str | None = None
    status: str | None = None
