"""Durable worker lifecycle. Only run IDs cross the broker boundary."""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update, or_
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.tables import (
    runningRecord,
    RunResultRecord,
    ScoringRecord,
    ConversationRecord,
    RunExecutionRecord,
)
from app.services import harness_adapter
from app.services.package_service import PackageError
from app.services.legacy_scoring import ScoringError
from app.workers.celery_app import celery_app
from contracts.harness import HarnessResult


RUN_STATUS = {
    "completed": "completed",
    "awaiting_review": "awaiting_review",
    "awaiting_confirmation": "awaiting_review",
    "failed": "failed",
    "cancelled": "failed",
    "interrupted": "failed",
}


def now():
    return datetime.now(timezone.utc)


def run_timeout(operation: str) -> int:
    # Documenting a full package needs more LLM/tool turns than other operations.
    if settings.ai_native_enabled and operation in ("validate", "monitor", "rebuild"):
        return max(settings.run_timeout_seconds, 1800)
    return (
        max(settings.run_timeout_seconds, 900)
        if operation == "document"
        else settings.run_timeout_seconds
    )


async def process_run(run_id, sessions):
    async with sessions() as session:
        claim = await session.execute(
            update(runningRecord)
            .where(runningRecord.id == run_id, runningRecord.status == "queued")
            .values(status="running", stage="executing", started_at=now(), error=None)
        )
        await session.commit()
        if claim.rowcount != 1:
            return  # Duplicate delivery, missing record, or terminal state.
        record = await session.get(runningRecord, run_id)
        request = harness_adapter.RunJob(
            run_id=record.id,
            model_id=record.model_id,
            model_version=record.model_version,
            dataset_id=record.dataset_id,
            operation=record.operation,
        )

    result = None
    try:

        async def wait_cancelled():
            while True:
                async with sessions() as session:
                    control = await session.get(RunExecutionRecord, run_id)
                    if control and control.cancel_requested:
                        return
                await asyncio.sleep(0.25)

        execution = asyncio.create_task(harness_adapter.execute(request))
        cancellation = asyncio.create_task(wait_cancelled())
        try:
            done, _ = await asyncio.wait(
                [execution, cancellation],
                timeout=run_timeout(request.operation),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation in done:
                raise PackageError("Cancelled by operator")
            if execution not in done:
                raise TimeoutError()
            result = HarnessResult.model_validate(await execution)
        finally:
            for task in (execution, cancellation):
                if not task.done():
                    task.cancel()
            await asyncio.gather(execution, cancellation, return_exceptions=True)
        if (
            result.status in ("awaiting_review", "awaiting_confirmation")
            and not result.checkpoint_id
        ):
            raise ValueError("Checkpoint required")
        status = RUN_STATUS[result.status]
        error = (result.error_code or result.status) if status == "failed" else None
    except (PackageError, ScoringError) as exc:
        result = None
        status, error = "failed", str(exc)
    except NotImplementedError:
        result = None
        status, error = "failed", "Harness execution is not connected yet."
    except TimeoutError:
        result = None
        status, error = "failed", "Execution exceeded its time limit."
    except Exception:
        result = None
        # Do not persist arbitrary exception strings (could contain secrets).
        status, error = "failed", "Execution failed. Review the worker integration."

    async with sessions() as session:
        changed = await session.execute(
            update(runningRecord)
            .where(runningRecord.id == run_id, runningRecord.status == "running")
            .values(
                status=status,
                stage=result.status if result else status,
                error=error,
                finished_at=now() if status in ("completed", "failed") else None,
            )
        )
        if changed.rowcount and result is not None:
            await session.merge(
                RunResultRecord(run_id=run_id, payload=result.model_dump())
            )
        await session.commit()


async def recover_runs(sessions):
    """Republish queued work; fail abandoned execution without replaying side effects."""
    async with sessions() as session:
        await session.execute(
            update(runningRecord)
            .where(
                runningRecord.status == "running",
                or_(*[(runningRecord.operation == operation) &
                      (runningRecord.started_at < now() - timedelta(seconds=run_timeout(operation) + 60))
                      for operation in ("document", "validate", "monitor", "rebuild")]),
            )
            .values(
                status="failed",
                stage="interrupted",
                finished_at=now(),
                error="Execution stopped responding or exceeded its time limit.",
            )
        )
        ids = list(
            await session.scalars(
                select(runningRecord.id)
                .where(
                    runningRecord.status == "queued",
                    runningRecord.created_at < now() - timedelta(seconds=60),
                )
                .limit(100)
            )
        )
        await session.commit()
    async with sessions() as session:
        await session.execute(
            update(ScoringRecord)
            .where(
                ScoringRecord.status == "running",
                ScoringRecord.started_at
                < now() - timedelta(seconds=settings.scoring_timeout_seconds + 120),
            )
            .values(
                status="failed",
                finished_at=now(),
                error="Scoring worker stopped responding.",
            )
        )
        scoring_ids = list(
            await session.scalars(
                select(ScoringRecord.id)
                .where(
                    ScoringRecord.status == "queued",
                    ScoringRecord.created_at < now() - timedelta(seconds=60),
                )
                .limit(100)
            )
        )
        await session.commit()
    async with sessions() as session:
        await session.execute(
            update(ConversationRecord)
            .where(
                ConversationRecord.busy.is_(True),
                ConversationRecord.updated_at
                < now() - timedelta(seconds=settings.run_timeout_seconds + 60),
            )
            .values(busy=False)
        )
        await session.commit()
    from app.services.upload_service import UploadRecord

    async with sessions() as session:
        await session.execute(
            update(UploadRecord)
            .where(
                UploadRecord.status == "running",
                UploadRecord.started_at
                < now() - timedelta(seconds=settings.run_timeout_seconds + 120),
            )
            .values(
                status="failed",
                finished_at=now(),
                error="Upload worker interrupted; inspect checks and resubmit",
            )
        )
        upload_ids = list(
            await session.scalars(
                select(UploadRecord.id)
                .where(
                    UploadRecord.status == "queued",
                    UploadRecord.created_at < now() - timedelta(seconds=60),
                )
                .limit(100)
            )
        )
        await session.commit()
    for upload_id in upload_ids:
        await asyncio.to_thread(
            execute_upload.apply_async, args=[upload_id], task_id=upload_id, retry=False
        )
    from harness.store import SqlTaskStore
    from app.services import ai_native_service

    await SqlTaskStore(sessions).interrupt_orphans(run_timeout("document") + 60)
    await ai_native_service.recover_runs(sessions, run_timeout("document") + 120)
    for execution_id in scoring_ids:
        await asyncio.to_thread(
            execute_scoring.apply_async,
            args=[execution_id],
            task_id=execution_id,
            retry=False,
        )
    from app.services.run_service import publish_run

    for run_id in ids:
        await asyncio.to_thread(publish_run, run_id)


async def with_database(action, *args):
    # Each task owns its engine: no pooled connections cross asyncio event loops.
    engine = create_async_engine(settings.resolved_database_url)
    harness_adapter.prepare_engine(engine)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        await action(*args, sessions)
    finally:
        await engine.dispose()


@celery_app.task(
    name="model_risk.execute_run", acks_late=True, reject_on_worker_lost=True
)
def execute_run(run_id: str) -> None:
    asyncio.run(with_database(process_run, run_id))


@celery_app.task(name="model_risk.recover_runs")
def recover_pending_runs() -> None:
    asyncio.run(with_database(recover_runs))
    from app.services.intake_service import recover
    asyncio.run(with_database(recover))


@celery_app.task(
    name="model_risk.execute_scoring", acks_late=True, reject_on_worker_lost=True
)
def execute_scoring(execution_id: str) -> None:
    from app.services.legacy_scoring import process_scoring

    asyncio.run(with_database(process_scoring, execution_id))


@celery_app.task(
    name="model_risk.execute_upload", acks_late=True, reject_on_worker_lost=True
)
def execute_upload(upload_id: str):
    from app.services.upload_service import process_upload

    asyncio.run(with_database(process_upload, upload_id))


@celery_app.task(name="model_risk.execute_intake", acks_late=True, reject_on_worker_lost=True)
def execute_intake(archive_id: str):
    from app.services.native_runner import process_intake

    asyncio.run(with_database(process_intake, archive_id))


@celery_app.task(name="model_risk.execute_intake_bundles", acks_late=True, reject_on_worker_lost=True)
def execute_intake_bundles(archive_id: str):
    from app.services.intake_service import build_bundles

    async def guarded(identity, sessions):
        from app.services.intake_service import ArchiveRecord
        try:
            await build_bundles(identity, sessions)
        except Exception:
            async with sessions() as session:
                record = await session.get(ArchiveRecord, identity)
                if record and record.status == "building":
                    record.status, record.error = "failed", "Bundle registration failed; inspect the confirmed plan and retry"
                    await session.commit()
    asyncio.run(with_database(guarded, archive_id))
