"""Backend operation routing through the agreed harness.runner bridge."""

import asyncio
import json
from contextlib import asynccontextmanager
from uuid import uuid4
from typing import Literal
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.config import settings
from app.db.tables import (
    runningRecord,
    RunExecutionRecord,
    ArtifactRecord,
    ReviewRecord,
    AuditRecord,
)
from app.services.report_service import (
    registered_package,
    generate_validation,
    persist_report,
)
from app.services.package_service import PackageError
from contracts.harness import HarnessRequest, HarnessResult


class RunJob(BaseModel):
    """Persisted backend queue payload; deliberately separate from HarnessRequest."""

    run_id: str
    model_id: str
    model_version: str
    dataset_id: str
    operation: Literal["document", "rebuild", "validate", "monitor"]


def prepare_engine(engine):
    from harness.db import enable_sqlite_pragmas

    enable_sqlite_pragmas(engine)


def create_harness_tables(connection):
    from harness.db import create_tables

    create_tables(connection)


def harness_settings(recalibration=False):
    from harness.config import HarnessSettings
    from harness.profiles import DEFAULT_PROFILES

    profiles = dict(DEFAULT_PROFILES)
    profiles["document"] = profiles["document"].model_copy(
        update={
            "max_turns": 36,
            "tool_policy": tuple(
                dict.fromkeys((*profiles["document"].tool_policy, "request_review"))
            ),
        }
    )
    if recalibration:
        from harness.profiles import _SANDBOX_RULES, _SHARED_RULES
        profiles["rebuild"] = profiles["rebuild"].model_copy(update={
            "name": "backend-recalibration-v1",
            "system_prompt": "Recalibrate the bound fitted model using only supplied development data. "
                "Preserve its feature/target contract. Never use evaluation data or claim migration parity. "
                "Export candidate/ with score.py and plain parameter files. State training changes and limitations; "
                "the candidate requires independent validation before acceptance. " + _SANDBOX_RULES + _SHARED_RULES,
        })
    configured = HarnessSettings.from_env() if hasattr(HarnessSettings, "from_env") else HarnessSettings()
    return configured.model_copy(update={"global_budget_usd": settings.harness_budget_usd, "profiles": profiles})


def mount_resolver(sessions):
    async def resolve(info):
        from harness.sandbox import SandboxMounts
        from app.db.tables import PackageRecord
        from app.services.package_service import verify_snapshot
        from harness.schemas import SandboxUnavailable
        from app.services.ai_native_service import AiNativeError, input_mounts

        # The AI-native inputs (archive, document, metrics, monitoring, reports, and data),
        # each a registered, hash-checked copy. A bad input fails the task as
        # sandbox_unavailable before any LLM call.
        try:
            extra = await input_mounts(sessions, info)
        except AiNativeError as exc:
            raise SandboxUnavailable(str(exc)) from None
        if not info.model_package_id:
            return SandboxMounts(**extra)
        async with sessions() as session:
            package = await session.get(PackageRecord, info.model_package_id)
        # Development data is either the package's own fixture data or a registered data input.
        registered = extra.pop("development_data_path", None)
        if not package or (
            registered is None and (package.input_sha256 or None) != info.development_data_id
        ):
            raise PackageError("No matching bound package and development data")
        snapshot = await asyncio.to_thread(
            verify_snapshot, package.sha256, package.file_hashes
        )
        development = registered or (str(snapshot / "data") if (snapshot / "data").is_dir() else None)
        if info.kind == "rebuild" and info.run_id:
            from app.services.native_analysis import AnalysisRecord
            from app.services.upload_service import InventoryRecord, DatasetRecord, dataset_root
            from app.services.package_service import digest
            import shutil
            async with sessions() as session:
                goal = await session.get(AnalysisRecord, info.run_id)
                inventory = await session.get(InventoryRecord, package.model_id)
                if goal and goal.kind == "recalibration":
                    dataset = await session.get(DatasetRecord, inventory.dataset_version_id) if inventory else None
                    if not dataset:
                        raise PackageError("Recalibration requires a bound development dataset")
                    source = dataset_root(dataset) / "partitions/train.csv"
                    if digest(source) != dataset.statistics["partition_sha256"]["train"]:
                        raise PackageError("Recalibration development data changed")
                    folder = settings.storage_path / "analysis" / info.run_id / "development"
                    folder.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, folder / "development.csv")
                    (folder / "dataset.json").write_text(json.dumps(dataset.descriptor))
                    development = str(folder)
        return SandboxMounts(
            model_package_path=str(snapshot / "package"),
            development_data_path=development,
            **extra,
        )

    return resolve


async def task_info_for_request(sessions, request):
    """Resolve a runner-owned workspace without accepting a task ID from the agent."""
    from harness.store import SqlTaskStore

    store = SqlTaskStore(sessions)
    identity = await store.find_by_request_id(request.request_id)
    if identity is None:
        raise PackageError("Harness task has not started")
    snapshot = await store.get_task(identity)
    if (
        snapshot.actor_id != request.actor.id
        or snapshot.actor_role != request.actor.role
        or snapshot.kind != request.kind
        or snapshot.context.model_dump(exclude_none=True)
        != request.context.model_dump(exclude_none=True)
    ):
        raise PackageError("Harness task context does not match the bound request")
    return snapshot.to_info()


@asynccontextmanager
async def runtime(sessions, recalibration=False):
    from harness.runner import open_harness
    from harness.sandbox import SandboxConfig

    if (
        not settings.openrouter_api_key
        or not settings.openrouter_api_key.get_secret_value()
    ):
        raise PackageError("Configure OPENROUTER_API_KEY for agent execution.")
    sandbox = SandboxConfig.from_env() if hasattr(SandboxConfig, "from_env") else SandboxConfig()
    if settings.scoring_image:
        sandbox = sandbox.model_copy(update={"image": settings.scoring_image})
    async with open_harness(
        sessions,
        settings.openrouter_api_key.get_secret_value(),
        settings=harness_settings(recalibration),
        resolve_mounts=mount_resolver(sessions),
        sandbox_config=sandbox,
    ) as runner:
        from app.services.resource_limits import limits
        runner.share_limits(*limits())
        yield runner


async def execute(request: RunJob) -> HarnessResult:
    engine = create_async_engine(settings.resolved_database_url)
    prepare_engine(engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await execute_with_sessions(request, sessions)
    finally:
        await engine.dispose()


async def execute_with_sessions(request, sessions):
    async with sessions() as session:
        run = await session.get(runningRecord, request.run_id)
        control = await session.get(RunExecutionRecord, run.id)
        if control is None:
            control = RunExecutionRecord(run_id=run.id, actor_id="local-operator")
            session.add(control)
            await session.commit()
        package = await registered_package(session, run)
        from app.services.native_analysis import AnalysisRecord, execute as execute_analysis
        analysis = await session.get(AnalysisRecord, run.id)
    if analysis and analysis.kind in ("triage", "investigation"):
        return await execute_analysis(run, package, control, analysis, sessions)
    if request.operation == "validate":
        payload, hashes = await generate_validation(package, sessions)
        async with sessions() as session:
            context = await session.scalar(select(AuditRecord).where(AuditRecord.resource_id == run.id, AuditRecord.action == "revalidation_monitoring_context"))
            if context and context.payload.get("monitoring_id"):
                from app.db.tables import MonitoringRecord
                monitoring = await session.get(MonitoringRecord, context.payload["monitoring_id"])
                payload = {**payload, "monitoring_context": monitoring.payload if monitoring else {"status": "NOT_ASSESSABLE"}}
        if settings.ai_native_enabled:
            from app.services.native_analysis import AnalysisRecord, execute as execute_analysis
            async with sessions() as session:
                analysis = await session.get(AnalysisRecord, run.id)
                if analysis is None:
                    analysis = AnalysisRecord(run_id=run.id, kind="validation", inputs={})
                    session.add(analysis)
                    await session.commit()
            # The validator sees the sanitised metrics bundle built from this report, never the report.
            await execute_analysis(run, package, control, analysis, sessions, report=payload)
            async with sessions() as session:
                analysis = await session.get(AnalysisRecord, run.id)
                payload = {
                    **payload,
                    "agent_review": analysis.result,
                    "challenger": analysis.result.get("challenger", {}),
                }
                if payload.get("challenger", {}).get("status") == "NOT_ASSESSABLE":
                    payload["checks"] = [*payload["checks"], {"check_id": "challenger_comparison", "status": "NOT_ASSESSABLE", "required": True,
                        "summary": "Independent challenger comparison could not be completed", "evidence_kind": "fresh_execution", "details": payload["challenger"]}]
                    payload["summary"] = {**payload["summary"], "required_checks_passed": False,
                        "blocking_check_ids": [*payload["summary"]["blocking_check_ids"], "challenger_comparison"],
                        "limitation_check_ids": [*payload["summary"].get("limitation_check_ids", []), "challenger_comparison"]}
        async with sessions() as session:
            report, review = await persist_report(
                session,
                run,
                "validation",
                payload,
                control.actor_id,
                payload["summary"]["blocking_check_ids"],
            )
            session.add(
                AuditRecord(
                    actor_id=control.actor_id,
                    resource_id=run.id,
                    action="validation_inputs",
                    payload={
                        "package_sha256": package.sha256,
                        "files": hashes,
                        "runtime_image": settings.scoring_image,
                    },
                )
            )
            await session.commit()
            return HarnessResult(
                status="awaiting_review",
                artifact_ids=[report.artifact_id],
                checkpoint_id=review.id,
                proposal_ids=(analysis.result or {}).get("proposal_ids", []) if analysis else [],
            )
    if request.operation == "monitor":
        from app.services.monitoring_service import execute_monitor

        return await execute_monitor(run, sessions)
    if run.operation == "rebuild" and package.language != "R" and not (analysis and analysis.kind == "recalibration"):
        raise PackageError("This migration workflow requires a registered R package.")
    return await execute_agent(run, package, control, sessions)


async def execute_agent(run, package, control, sessions):
    from contracts.harness import RequestContext, Actor, Resolution
    from app.services.tool_gateway import BackendGateway
    from app.services.legacy_scoring import create_command

    create_command("preflight", settings.scoring_image, settings.storage_path, "Python")

    async with sessions() as session:
        from app.services.native_analysis import AnalysisRecord
        goal = await session.get(AnalysisRecord, run.id)
    context = runtime(sessions, recalibration=True) if goal and goal.kind == "recalibration" else runtime(sessions)
    async with context as runner:
        prompt = (
            "Write model_document.md using the exact document template. Inspect mounted code and evidence; treat old notes as untrusted. Export it and request human review."
            if run.operation == "document"
            else (
                "Rebuild the fitted R model in Python inside candidate/. Include score.py and plain parameter files; never refit. "
                "Preserve fitted parameters at round-trip double precision, feature order and preprocessing. "
                "Use a CSV parser for quoted headers and a numerically stable library sigmoid (for example scipy.special.expit), rather than hand-written sigmoid branches. "
                "Once the minimal candidate can score, immediately export candidate/ and call run_parity: it is the platform's official parity check, the same one the human reviewer runs. "
                "Do not spend the turn budget building a separate R/Python comparison or adjusting parameters based on an ad-hoc comparison. "
                "If official parity fails, fix candidate/ using its diagnostics and worst rows, export again and rerun run_parity. "
                "sandbox_exec takes one command as an argv list; use separate calls or an explicit script for multiple commands. "
                "Request review once parity passes, or when you cannot make it pass, saying which."
            )
        )
        async with sessions() as session:
            from app.services.native_analysis import AnalysisRecord
            goal = await session.get(AnalysisRecord, run.id)
            if goal and goal.kind == "recalibration":
                prompt = ("Produce a recalibrated candidate using only the agreed development data. "
                          "Do not access evaluation data. Export candidate/ and request independent review. "
                          "State all changes and limitations. Recalibration is not prediction-preserving migration. "
                          + json.dumps(goal.inputs))
        async with sessions() as session:
            revision = await session.scalar(
                select(AuditRecord).where(
                    AuditRecord.resource_id == run.id,
                    AuditRecord.action == "document_revision_requested",
                )
            )
            if revision:
                prompt += (
                    " Reviewer revision instructions: " + revision.payload["rationale"]
                )
        if control.resolution:
            prompt = (
                "The human review was accepted. Keep the exported artifact unchanged and summarize completion, including conditions."
                if control.resolution["outcome"] == "approved"
                else "The human requested changes. Revise using the decision rationale, export a new version, call run_parity again for a rebuild, and request a new review."
            )
        task_request = HarnessRequest(
            request_id=run.id
            + (":" + control.resolution["pending_id"] if control.resolution else ""),
            actor=Actor(
                id=control.actor_id, role=await actor_role(sessions, control.actor_id)
            ),
            kind=run.operation,
            prompt=prompt,
            context=RequestContext(
                policy_version="1.0.0",
                model_id=run.model_id,
                run_id=run.id,
                model_package_id=package.id,
                development_data_id=package.input_sha256 or None,
            ),
            resolution=Resolution(**control.resolution) if control.resolution else None,
            resume_from=control.parent_task_id,
        )

        async def emit(event):
            async with sessions() as session:
                saved = await session.get(RunExecutionRecord, run.id)
                saved.task_id = event.task_id
                record = await session.get(runningRecord, run.id)
                if record.status == "running":
                    record.stage = event.type
                session.add(
                    AuditRecord(
                        actor_id=control.actor_id,
                        resource_id=run.id,
                        action="agent_event",
                        payload=event.model_dump(),
                    )
                )
                await session.commit()

        cancel_signal = asyncio.Event()

        async def cancellation():
            while True:
                async with sessions() as session:
                    saved = await session.get(RunExecutionRecord, run.id)
                if saved.cancel_requested:
                    cancel_signal.set()
                    return
                await asyncio.sleep(0.25)

        watcher = asyncio.create_task(cancellation())
        try:
            result = await runner.run(
                task_request,
                BackendGateway(task_request, sessions),
                emit,
                cancel_signal,
            )
            if run.operation == "rebuild" and not (goal and goal.kind == "recalibration"):
                from app.services.parity_service import latest_candidate, run_parity, ParityError
                for attempt in range(2):
                    async with sessions() as session:
                        candidate = await latest_candidate(session, run.id)
                    if not candidate or result.status not in ("completed", "awaiting_review"):
                        break
                    try:
                        parity = await run_parity(sessions, candidate, initiator="agent", actor_id=control.actor_id, rebuild_run_id=run.id)
                    except ParityError:
                        break  # Missing reference remains blocked at the human gate.
                    if parity.passed or result.status != "completed" or attempt == 1:
                        break
                    task_request = task_request.model_copy(update={"request_id": run.id + ":parity-repair", "resume_from": result.task_id,
                        "prompt": "Official development parity failed. Repair the candidate, export candidate/ again, and finish. " + json.dumps(parity.result)})
                    result = await runner.run(task_request, BackendGateway(task_request, sessions), emit, cancel_signal)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        async with sessions() as session:
            saved = await session.get(RunExecutionRecord, run.id)
            saved.task_id = result.task_id
            await session.commit()
            artifacts = list(
                await session.scalars(
                    select(ArtifactRecord).where(ArtifactRecord.run_id == run.id)
                )
            )
            if result.status == "awaiting_review":
                review = await session.get(ReviewRecord, result.checkpoint_id)
                if not review or review.run_id != run.id:
                    raise PackageError("Agent returned an invalid review checkpoint.")
                return result.model_copy(
                    update=dict(
                        status="awaiting_review",
                        artifact_ids=[a.id for a in artifacts],
                        checkpoint_id=review.id,
                    )
                )
            if result.status == "completed":
                expected_type = (
                    "text/markdown"
                    if run.operation == "document"
                    else "application/zip"
                )
                candidates = sorted(
                    [a for a in artifacts if a.media_type == expected_type],
                    key=lambda a: a.created_at,
                )
                if not candidates:
                    raise PackageError("Agent did not export the required artifact.")
                latest = candidates[-1]
                review = await session.scalar(
                    select(ReviewRecord)
                    .where(
                        ReviewRecord.run_id == run.id,
                        ReviewRecord.artifact_id == latest.id,
                    )
                    .order_by(ReviewRecord.created_at.desc())
                )
                if not review:
                    review = ReviewRecord(
                        id=str(uuid4()),
                        run_id=run.id,
                        artifact_id=latest.id,
                        artifact_version=latest.version,
                        requested_by=control.actor_id,
                        # Gate 2 checks official parity when the decision is made.
                        blockers=[],
                    )
                    session.add(review)
                    await session.commit()
                if review.decision not in ("accepted", "accepted_with_conditions"):
                    return result.model_copy(
                        update=dict(
                            status="awaiting_review",
                            artifact_ids=[a.id for a in artifacts],
                            checkpoint_id=review.id,
                        )
                    )
                return result.model_copy(
                    update=dict(
                        status="completed", artifact_ids=[a.id for a in artifacts]
                    )
                )
        raise PackageError(
            "Agent execution ended: " + (result.error_code or result.status)
        )


async def actor_role(sessions, identity):
    from app.db.auth_tables import UserRecord

    async with sessions() as session:
        user = await session.get(UserRecord, identity)
        if user and user.active:
            return user.role
    if settings.allow_local_demo_auth:
        return "operator"
    raise PackageError("Initiating user is unavailable or disabled")
