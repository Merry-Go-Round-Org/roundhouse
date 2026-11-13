from typing import Annotated
from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_session
from app.db.tables import (
    ModelRecord,
    PackageRecord,
    runningRecord,
    ScoringRecord,
    ReportRecord,
    ReviewRecord,
    FindingRecord,
    MonitoringRecord,
    AuditRecord,
)

router = APIRouter(tags=["Dashboard"])


@router.get("/dashboard")
async def get_dashboard(session: Annotated[AsyncSession, Depends(get_session)]):
    result = {}
    result["models"] = await session.scalar(
        select(func.count()).select_from(ModelRecord).where(ModelRecord.lifecycle_status != "deleted")
    )
    for name, table in [
        ("packages", PackageRecord),
        ("reports", ReportRecord),
        ("monitoring_events", MonitoringRecord),
    ]:
        result[name] = await session.scalar(select(func.count()).select_from(table))
    for name, table in [
        ("runs", runningRecord),
        ("scoring_runs", ScoringRecord),
        ("reviews", ReviewRecord),
        ("findings", FindingRecord),
    ]:
        result[name] = dict(
            (
                await session.execute(
                    select(table.status, func.count()).group_by(table.status)
                )
            ).all()
        )
    result["lifecycle"] = dict(
        (
            await session.execute(
                select(ModelRecord.lifecycle_status, func.count())
                .where(ModelRecord.lifecycle_status != "deleted")
                .group_by(ModelRecord.lifecycle_status)
            )
        ).all()
    )
    from datetime import timezone

    def aware(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    completed = list(
        await session.scalars(
            select(runningRecord).where(
                runningRecord.started_at.is_not(None),
                runningRecord.finished_at.is_not(None),
            )
        )
    )
    times = {}
    for run in completed:
        times.setdefault(run.operation, []).append(
            max(0, (aware(run.finished_at) - aware(run.started_at)).total_seconds())
        )
    result["stage_elapsed_seconds"] = {
        op: {
            "count": len(values),
            "mean": sum(values) / len(values),
            "total": sum(values),
        }
        for op, values in times.items()
    }
    review_times = []
    for review in await session.scalars(
        select(ReviewRecord).where(ReviewRecord.status == "decided")
    ):
        opened = await session.scalar(
            select(AuditRecord)
            .where(
                AuditRecord.resource_id == review.id,
                AuditRecord.actor_id == review.reviewer_id,
                AuditRecord.action == "review_opened",
            )
            .order_by(AuditRecord.created_at)
        )
        if opened:
            review_times.append(
                max(
                    0,
                    (
                        aware(review.decided_at) - aware(opened.created_at)
                    ).total_seconds(),
                )
            )
    from harness.db import LlmCall

    result["llm_cost_usd"] = await session.scalar(
        select(func.coalesce(func.sum(LlmCall.cost_usd), 0))
    )
    result["review_elapsed_seconds"] = {
        "count": len(review_times),
        "total": sum(review_times),
        "mean": sum(review_times) / len(review_times) if review_times else None,
    }
    models = list(await session.scalars(select(ModelRecord)))
    result["red_alerts"] = sum(m.lifecycle_status == "revalidation_due" for m in models)
    result["measurement_limitations"] = [
        "Elapsed times are not person-hours or active review effort",
        "LLM cost may include provider estimates; no savings baseline is claimed",
        "Only persisted executions and explicit review-open events are counted",
    ]
    result["source"] = "persisted_records"
    return result
