"""Durable chat execution and public event records, separate from the harness store."""

from datetime import datetime, timezone
from sqlalchemy import ForeignKey, String, Text, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.tables import Base


def now():
    return datetime.now(timezone.utc)


class ChatTurnRecord(Base):
    __tablename__ = "chat_turns"
    __table_args__ = (UniqueConstraint("conversation_id", "client_message_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id"), index=True
    )
    actor_id: Mapped[str] = mapped_column(String(120))
    actor_role: Mapped[str] = mapped_column(String(30))
    client_message_id: Mapped[str] = mapped_column(String(120))
    request_hash: Mapped[str] = mapped_column(String(64))
    user_message_id: Mapped[str] = mapped_column(String(36))
    assistant_message_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    resume_from: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resolution: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=now)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    heartbeat_at: Mapped[datetime] = mapped_column(default=now)


class ChatPublicEvent(Base):
    __tablename__ = "chat_public_events"
    seq: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("chat_turns.id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=now)


class PendingChatAction(Base):
    __tablename__ = "chat_pending_actions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("chat_turns.id"), index=True)
    actor_id: Mapped[str] = mapped_column(String(120))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    action_version: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(20))
    model_id: Mapped[str] = mapped_column(String(36))
    model_version: Mapped[str] = mapped_column(String(120))
    dataset_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    decision_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    continuation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=now)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ChatActionInput(Base):
    """Immutable parameters shown to the human before a monitoring action starts."""

    __tablename__ = "chat_action_inputs"
    action_id: Mapped[str] = mapped_column(
        ForeignKey("chat_pending_actions.id"), primary_key=True
    )
    parameters: Mapped[dict] = mapped_column(JSON)
