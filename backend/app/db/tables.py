"""Initial persisted model metadata; add run/review tables with migrations later."""

from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import DateTime, String, CheckConstraint, ForeignKey, Text, JSON, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ModelRecord(Base):
    __tablename__ = "models"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    name: Mapped[str] = mapped_column(String(120))
    version: Mapped[str] = mapped_column(String(40))
    language: Mapped[str] = mapped_column(String(10))
    dataset_id: Mapped[str] = mapped_column(String(40))
    purpose: Mapped[str] = mapped_column(String(1000))
    owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lifecycle_status: Mapped[str] = mapped_column(String(30), default="development")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


@event.listens_for(ModelRecord.lifecycle_status, "set", retval=True)
def _keep_soft_deleted(target, value, oldvalue, initiator):
    """A soft-deleted model stays deleted.

    Work that finishes after deletion (a late review decision, report or monitoring
    event) must not move the model back into the inventory. Only an explicit
    restore, which sets `_restoring`, may leave the deleted state.
    """
    if oldvalue == "deleted" and value != "deleted" and not getattr(target, "_restoring", False):
        return oldvalue
    return value


class runningRecord(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('document', 'rebuild','validate', 'monitor')",
            name="runs_operations_ck",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_review', 'completed','failed')",
            name="runs_status_ck",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    model_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("models.id"),
        index=True,
    )

    model_version: Mapped[str] = mapped_column(String(40))
    dataset_id: Mapped[str] = mapped_column(String(40))
    operation: Mapped[str] = mapped_column(String(40))

    ## Explicitely choosing the initial state:

    status: Mapped[str] = mapped_column(String(30), index=True)
    stage: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )
    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class RunResultRecord(Base):
    __tablename__ = "run_results"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class PackageRecord(Base):
    __tablename__ = "model_packages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id"), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(40))
    dataset_id: Mapped[str] = mapped_column(String(40))
    language: Mapped[str] = mapped_column(String(10))
    sha256: Mapped[str] = mapped_column(String(64))
    input_sha256: Mapped[str] = mapped_column(String(64))
    file_hashes: Mapped[dict] = mapped_column(JSON)
    n_rows: Mapped[int] = mapped_column()


class ScoringRecord(Base):
    __tablename__ = "scoring_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    package_id: Mapped[str] = mapped_column(ForeignKey("model_packages.id"))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ArtifactRecord(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    version: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(120))
    media_type: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class ReportRecord(Base):
    __tablename__ = "reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class ReviewRecord(Base):
    __tablename__ = "reviews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"))
    artifact_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    requested_by: Mapped[str] = mapped_column(String(120))
    reviewer_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    conditions: Mapped[list] = mapped_column(JSON, default=list)
    blockers: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class FindingRecord(Base):
    __tablename__ = "findings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id"), index=True)
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id"))
    check_id: Mapped[str] = mapped_column(String(100))
    severity: Mapped[str] = mapped_column(String(30))
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="open")
    comments: Mapped[list] = mapped_column(JSON, default=list)


class MonitoringRecord(Base):
    __tablename__ = "monitoring_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class RunExecutionRecord(Base):
    __tablename__ = "run_execution"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(120), default="local-operator")
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    parent_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resolution: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(default=False)


class ConversationRecord(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(120))
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    busy: Mapped[bool] = mapped_column(default=False)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class ChatMessageRecord(Base):
    __tablename__ = "chat_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(30))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class AuditRecord(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    actor_id: Mapped[str] = mapped_column(String(120))
    resource_id: Mapped[str] = mapped_column(String(100), index=True)
    action: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class FindingAssignment(Base):
    """Separate additive table so existing findings need no destructive migration."""

    __tablename__ = "finding_assignments"
    finding_id: Mapped[str] = mapped_column(ForeignKey("findings.id"), primary_key=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
