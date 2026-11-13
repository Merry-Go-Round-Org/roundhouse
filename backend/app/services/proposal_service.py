"""Human-authorised actions. Targets and preconditions come from trusted run context."""

from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, DateTime, ForeignKey, String, select, update
from sqlalchemy.orm import Mapped, mapped_column

from harness.proposals import (
    ACTIONS as AGENT_ACTIONS,
    REQUIRED_ROLES,
    Evidence,  # noqa: F401  re-exported for callers
    ProposalDraft,
)

from app.db.tables import (
    Base,
    ModelRecord,
    runningRecord,
    AuditRecord,
    MonitoringRecord,
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Operation(Strict):
    """Backend-only action: chat asks for a platform operation. Agents cannot propose it."""

    operation: Literal["document", "rebuild", "validate"]


# The harness catalogue is the source of truth for agent actions, their arguments and roles.
ACTIONS = {name: (schema, REQUIRED_ROLES[name]) for name, schema in AGENT_ACTIONS.items()}
ACTIONS["start_operation"] = (Operation, "developer")
Draft = ProposalDraft

class Decision(Strict):
    decision: Literal["approved", "rejected"]
    rationale: str = Field(min_length=1, max_length=2000)
    owner_id: str | None = None
    due_date: datetime | None = None


class ProposalRecord(Base):
    __tablename__ = "action_proposals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id"), index=True, nullable=True
    )
    source_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id"), index=True)
    type: Mapped[str] = mapped_column(String(40))
    args: Mapped[dict] = mapped_column(JSON)
    rationale: Mapped[str] = mapped_column(String(2000))
    evidence: Mapped[list] = mapped_column(JSON)
    required_role: Mapped[str] = mapped_column(String(30))
    preconditions: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    decision: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(200), unique=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


def view(record):
    return {c.name: getattr(record, c.name) for c in record.__table__.columns}


async def create(
    session,
    run_id,
    draft,
    actor_id,
    idempotency_key=None,
    *,
    model_id=None,
    task_id=None,
    conversation_id=None,
):
    previous = None
    if idempotency_key:
        previous = await session.scalar(
            select(ProposalRecord).where(
                ProposalRecord.idempotency_key == idempotency_key
            )
        )
    draft = Draft.model_validate(draft)
    if draft.type not in ACTIONS:
        raise HTTPException(422, "Unknown proposal action")
    schema, role = ACTIONS[draft.type]
    args = schema.model_validate(draft.args).model_dump(mode="json")
    if draft.type == "start_operation" and args["operation"] in ("validate", "monitor"):
        role = "validator"
    run = await session.get(runningRecord, run_id) if run_id else None
    if run_id and not run:
        raise HTTPException(404, "Source run not found")
    model = await session.get(ModelRecord, run.model_id if run else model_id)
    if not model or model.lifecycle_status == "deleted":
        raise HTTPException(404, "Bound model not found")
    if run and model.version != run.model_version:
        raise HTTPException(409, "Source run belongs to a superseded model version")
    if previous:
        if (
            previous.source_run_id != run_id
            or previous.model_id != model.id
            or previous.source_task_id != task_id
            or previous.conversation_id != conversation_id
            or previous.type != draft.type
            or previous.args != args
        ):
            raise HTTPException(
                409, "Proposal key belongs to a different action or context"
            )
        return previous
    from app.services.upload_service import InventoryRecord

    inventory = await session.get(InventoryRecord, model.id)
    monitoring = await session.scalar(
        select(MonitoringRecord)
        .where(MonitoringRecord.model_id == model.id)
        .order_by(MonitoringRecord.created_at.desc(), MonitoringRecord.id.desc())
        .limit(1)
    )
    record = ProposalRecord(
        id=str(uuid4()),
        source_run_id=run.id if run else None,
        source_task_id=task_id,
        conversation_id=conversation_id,
        model_id=model.id,
        type=draft.type,
        args=args,
        rationale=draft.rationale,
        evidence=[e.model_dump() for e in draft.evidence],
        required_role=role,
        preconditions={
            "model_version": model.version,
            "monitoring_id": monitoring.id if monitoring else None,
            "document_run_id": inventory.document_run_id if inventory else None,
            "bundle_sha256": inventory.bundle_sha256 if inventory else None,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).isoformat(),
        },
        status="pending",
        idempotency_key=idempotency_key,
    )
    session.add(record)
    session.add(
        AuditRecord(
            actor_id=actor_id,
            resource_id=record.id,
            action="proposal_created",
            payload={
                "source_run_id": run.id if run else None,
                "source_task_id": task_id,
                "type": draft.type,
            },
        )
    )
    await session.flush()
    return record


async def fresh(session, record):
    model = await session.get(ModelRecord, record.model_id)
    from app.services.native_analysis import AnalysisRecord

    analysis = (
        await session.get(AnalysisRecord, record.source_run_id)
        if record.source_run_id else None
    )
    monitoring_is_current = True
    if analysis and analysis.kind in ("triage", "investigation"):
        latest = await session.scalar(
            select(MonitoringRecord.id)
            .where(MonitoringRecord.model_id == record.model_id)
            .order_by(MonitoringRecord.created_at.desc(), MonitoringRecord.id.desc())
            .limit(1)
        )
        monitoring_is_current = latest == record.preconditions["monitoring_id"]
    from app.services.upload_service import InventoryRecord

    inventory = await session.get(InventoryRecord, record.model_id)
    return (
        model is not None
        and model.version == record.preconditions["model_version"]
        and monitoring_is_current
        and record.preconditions.get("document_run_id")
        == (inventory.document_run_id if inventory else None)
        and record.preconditions.get("bundle_sha256")
        == (inventory.bundle_sha256 if inventory else None)
    )


def expired(record):
    timestamp = record.preconditions.get("expires_at")
    return timestamp is not None and datetime.fromisoformat(timestamp) <= datetime.now(
        timezone.utc
    )


async def reconcile(session, record):
    if record.status not in ("pending", "approved"):
        return
    status = (
        "expired"
        if expired(record)
        else "superseded"
        if not await fresh(session, record)
        else None
    )
    if status:
        await session.execute(
            update(ProposalRecord)
            .where(
                ProposalRecord.id == record.id,
                ProposalRecord.status.in_(("pending", "approved")),
            )
            .values(status=status)
        )
        await session.flush()
        await session.refresh(record)


async def decide(session, identity, payload, who):
    record = await session.get(ProposalRecord, identity)
    if not record:
        raise HTTPException(404, "Proposal not found")
    if who.role not in (record.required_role, "admin"):
        raise HTTPException(403, "Proposal requires role: " + record.required_role)
    if payload.decision == "approved" and record.type == "open_finding":
        from app.db.auth_tables import UserRecord

        owner = (
            await session.get(UserRecord, payload.owner_id)
            if payload.owner_id
            else None
        )
        if not owner or not owner.active or not payload.due_date:
            raise HTTPException(
                422, "An active finding owner and due date are required"
            )
    if expired(record) or not await fresh(session, record):
        await session.execute(
            update(ProposalRecord)
            .where(
                ProposalRecord.id == identity,
                ProposalRecord.status.in_(("pending", "approved")),
            )
            .values(status="expired" if expired(record) else "superseded")
        )
        await session.commit()
        raise HTTPException(409, "Proposal superseded by newer evidence")
    claim = await session.execute(
        update(ProposalRecord)
        .where(ProposalRecord.id == identity, ProposalRecord.status == "pending")
        .values(
            status=payload.decision,
            decision={
                "actor_id": who.id,
                **payload.model_dump(mode="json"),
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )
    )
    if claim.rowcount != 1:
        raise HTTPException(409, "Proposal already decided")
    session.add(
        AuditRecord(
            actor_id=who.id,
            resource_id=identity,
            action="proposal_decision",
            payload=payload.model_dump(mode="json"),
        )
    )
    investigation = None
    if (
        payload.decision == "rejected"
        and record.type == "mark_drift_benign"
        and record.preconditions["monitoring_id"]
    ):
        from app.services.native_analysis import enqueue

        event = await session.get(
            MonitoringRecord, record.preconditions["monitoring_id"]
        )
        parent = await session.get(runningRecord, event.run_id)
        investigation = await enqueue(session, parent, "investigation", who.id)
    await session.commit()
    if investigation:
        from app.services.run_service import publish_run
        import asyncio

        try:
            await asyncio.to_thread(publish_run, investigation)
        except Exception:
            pass
    await session.refresh(record)
    return record


async def execute(session, identity, who):
    """Claim and mutate in one transaction; rollback retains an approved retryable action."""
    from app.db.tables import (
        FindingRecord,
        ReportRecord,
        RunExecutionRecord,
        ReviewRecord,
        FindingAssignment,
    )
    from app.services.run_service import authorize_operation, createRun
    from app.schemas.runs import RunCreate
    from app.services.upload_service import InventoryRecord

    record = await session.get(ProposalRecord, identity)
    if not record:
        raise HTTPException(404, "Proposal not found")
    if who.role not in (record.required_role, "admin"):
        raise HTTPException(403, "Required proposal role missing")
    if expired(record) or not await fresh(session, record):
        await session.execute(
            update(ProposalRecord)
            .where(ProposalRecord.id == identity, ProposalRecord.status == "approved")
            .values(status="expired" if expired(record) else "superseded")
        )
        await session.commit()
        raise HTTPException(409, "Proposal superseded")
    claim = await session.execute(
        update(ProposalRecord)
        .where(ProposalRecord.id == identity, ProposalRecord.status == "approved")
        .values(status="executed")
    )
    if claim.rowcount != 1:
        raise HTTPException(409, "Proposal is not approved or has already executed")
    args = ACTIONS[record.type][0].model_validate(record.args).model_dump()
    result = {}
    if record.type in ("start_revalidation", "start_recalibration", "start_operation"):
        operation = (
            args["operation"]
            if record.type == "start_operation"
            else "rebuild"
            if record.type == "start_recalibration"
            else "validate"
        )
        if operation == "monitor":
            raise HTTPException(
                422,
                "Monitoring requires an explicit batch or simulation; use the Monitoring page",
            )
        if record.type == "start_recalibration":
            await authorize_operation(session, record.model_id, "document", who)
            inventory = await session.get(InventoryRecord, record.model_id)
            accepted = (
                await session.scalar(
                    select(ReviewRecord.id).where(
                        ReviewRecord.run_id == inventory.document_run_id,
                        ReviewRecord.status == "decided",
                        ReviewRecord.decision.in_(
                            ("accepted", "accepted_with_conditions")
                        ),
                    )
                )
                if inventory
                else None
            )
            if not accepted:
                raise HTTPException(
                    409, "Current Gate 1 acceptance required before recalibration"
                )
        else:
            await authorize_operation(session, record.model_id, operation, who)
        model = await session.get(ModelRecord, record.model_id)
        run = await createRun(
            session,
            model.id,
            RunCreate(
                operation=operation,
                model_version=model.version,
                dataset_id=model.dataset_id,
            ),
        )
        session.add(RunExecutionRecord(run_id=run.id, actor_id=who.id))
        if record.type == "start_recalibration":
            from app.services.native_analysis import AnalysisRecord

            session.add(
                AnalysisRecord(
                    run_id=run.id,
                    kind="recalibration",
                    inputs={"goal": "recalibration", **args, "proposal_id": identity},
                )
            )
        result = {
            "run_id": run.id,
            "monitoring_id": record.preconditions["monitoring_id"],
        }
    elif record.type == "open_finding":
        report = await session.scalar(
            select(ReportRecord)
            .where(ReportRecord.run_id == record.source_run_id)
            .order_by(ReportRecord.created_at.desc())
            .limit(1)
        )
        if not report:
            raise HTTPException(409, "A persisted report is required for this finding")
        accepted = await session.scalar(
            select(ReviewRecord.id).where(
                ReviewRecord.run_id == report.run_id,
                ReviewRecord.status == "decided",
                ReviewRecord.decision.in_(("accepted", "accepted_with_conditions")),
            )
        )
        if not accepted:
            raise HTTPException(
                409, "Accept the validation report at Gate 3 before recording findings"
            )
        finding = FindingRecord(
            id=str(uuid4()),
            model_id=record.model_id,
            report_id=report.id,
            check_id=",".join(args["check_ids"])[:100] or "agent_review",
            severity=args["severity"],
            summary=args["title"],
            comments=[args],
        )
        session.add(finding)
        await session.flush()
        session.add(
            FindingAssignment(
                finding_id=finding.id,
                owner_id=record.decision["owner_id"],
                due_date=datetime.fromisoformat(record.decision["due_date"]),
            )
        )
        result = {"finding_id": finding.id}
    elif record.type == "restrict_use":
        inventory = await session.get(InventoryRecord, record.model_id)
        if not inventory:
            raise HTTPException(409, "Uploaded inventory required")
        inventory.provenance = {
            **inventory.provenance,
            "use_restrictions": [
                *inventory.provenance.get("use_restrictions", []),
                {**args, "proposal_id": identity, "actor_id": who.id},
            ],
        }
        result = {"restriction_recorded": True}
    else:
        monitoring = (
            await session.get(MonitoringRecord, record.preconditions["monitoring_id"])
            if record.preconditions["monitoring_id"]
            else None
        )
        if not monitoring:
            raise HTTPException(409, "Monitoring evidence required")
        key = (
            "benign_review" if record.type == "mark_drift_benign" else "data_incidents"
        )
        value = {**args, "proposal_id": identity, "actor_id": who.id}
        monitoring.payload = {
            **monitoring.payload,
            key: value
            if key == "benign_review"
            else [*monitoring.payload.get(key, []), value],
        }
        result = {"monitoring_id": monitoring.id}
    if record.type == "start_revalidation":
        # An approved re-validation means the current sign-off no longer stands.
        model = await session.get(ModelRecord, record.model_id)
        model.lifecycle_status = "revalidation_due"
        session.add(
            AuditRecord(
                actor_id=who.id,
                resource_id=result["run_id"],
                action="revalidation_monitoring_context",
                payload={
                    "monitoring_id": record.preconditions["monitoring_id"],
                    "proposal_id": identity,
                },
            )
        )
    record.result = result
    session.add(
        AuditRecord(
            actor_id=who.id,
            resource_id=identity,
            action="proposal_executed",
            payload=result,
        )
    )
    await session.commit()
    if result.get("run_id"):
        from app.services.run_service import publish_run
        import asyncio

        try:
            await asyncio.to_thread(publish_run, result["run_id"])
        except Exception:
            pass  # Existing durable recovery scheduler dispatches queued runs.
    await session.refresh(record)
    return record
