"""Tables owned by the harness team, in the backend database, with their own DeclarativeBase."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Connection,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    event,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_ACTIVE_ONLY = text("status IN ('queued', 'running')")


class HarnessBase(DeclarativeBase):
    pass


class AgentTask(HarnessBase):
    __tablename__ = "agent_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(36), index=True)
    parent_task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(36))
    request_id: Mapped[str] = mapped_column(String(200), unique=True)
    actor_id: Mapped[str] = mapped_column(String(200))
    actor_role: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))
    policy_version: Mapped[str] = mapped_column(String(100))
    context_json: Mapped[str] = mapped_column(Text)
    profile_name: Mapped[str] = mapped_column(String(100))
    profile_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), index=True)
    error_code: Mapped[str | None] = mapped_column(String(50))
    pending_id: Mapped[str | None] = mapped_column(String(200))
    result_text: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    unknown_cost_calls: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    owner_id: Mapped[str | None] = mapped_column(String(200))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "uq_agent_tasks_one_active_per_thread",
            "thread_id",
            unique=True,
            sqlite_where=_ACTIVE_ONLY,
            postgresql_where=_ACTIVE_ONLY,
        ),
    )


class AgentMessage(HarnessBase):
    """Append-only. Holds no system prompts, reasoning, or truncated or cancelled output."""

    __tablename__ = "agent_messages"

    thread_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    role: Mapped[str] = mapped_column(String(20))
    origin: Mapped[str] = mapped_column(String(20))
    content: Mapped[str | None] = mapped_column(Text)
    tool_calls_json: Mapped[str | None] = mapped_column(Text)
    tool_call_id: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LlmCall(HarnessBase):
    """One row per call attempt. Message text is linked through `input_seq`, not copied."""

    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    thread_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_id: Mapped[str] = mapped_column(String(200))
    turn: Mapped[int] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer)
    requested_model: Mapped[str] = mapped_column(String(200))
    provider_routing_json: Mapped[str] = mapped_column(Text)
    reasoning_effort: Mapped[str] = mapped_column(String(20))
    max_tokens: Mapped[int] = mapped_column(Integer)
    messages_hash: Mapped[str] = mapped_column(String(64))
    tools_hash: Mapped[str] = mapped_column(String(64))
    input_seq: Mapped[int] = mapped_column(Integer)
    generation_id: Mapped[str | None] = mapped_column(String(200))
    returned_model: Mapped[str | None] = mapped_column(String(200))
    provider: Mapped[str | None] = mapped_column(String(200))
    finish_reason: Mapped[str | None] = mapped_column(String(50))
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(50))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cost_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_chunk_ms: Mapped[int | None] = mapped_column(Integer)
    total_ms: Mapped[int | None] = mapped_column(Integer)


def create_tables(connection: Connection) -> None:
    """Create the harness tables. Call with `await connection.run_sync(create_tables)`."""
    HarnessBase.metadata.create_all(connection)


def enable_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Enable WAL and busy_timeout on every new connection. Does nothing on other databases."""
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
