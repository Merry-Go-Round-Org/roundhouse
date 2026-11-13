"""Validation, triage and investigation analyses.

Each analysis is a platform run (a validate run, or a monitor run queued by a PSI band) whose agent
part is one `ai_native_service` run. This module registers that run's inputs, executes it through
the harness path (gateway, `check_exports`, correction rounds, one proposals table), and records
the accepted exports against the platform run.

What each agent may read:

- data: the bound dataset's development (train) partition, with labels. Never a protected one.
- metrics: the sanitised metrics bundle from the official validation report
  (`harness.metrics_bundle`), never the report itself.
- monitoring: the PSI window summary, and the feature batch for an uploaded batch or a
  validation-partition simulation.
- document: the model document accepted at Gate 1.
- reports: the latest validation report's metrics bundle and validator narrative.
"""

import asyncio
import csv
import json
import tempfile
from pathlib import Path
from uuid import uuid4

from sqlalchemy import ForeignKey, JSON, String, select
from sqlalchemy.orm import Mapped, mapped_column

from contracts.harness import Actor
from app.config import settings
from app.db.tables import (
    Base,
    runningRecord,
    RunExecutionRecord,
    ArtifactRecord,
    ReviewRecord,
    ReportRecord,
    MonitoringRecord,
)
from app.services import ai_native_service as ai
from app.services.harness_adapter import runtime, actor_role
from app.services.package_service import PackageError, checked_file, digest
from app.services.artifact_service import artifact_bytes, save_artifact

POLICY_VERSION = "1.0.0"
REQUIRED_EXPORTS = {
    "validation": ("validation_narrative.md",),
    "triage": ("triage.json", "triage_report.md"),
    "investigation": ("investigation_report.md",),
}


class AnalysisRecord(Base):
    """Links a platform run to its analysis: the kind, the agent run, and the accepted result."""

    __tablename__ = "native_analysis"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    inputs: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)


async def enqueue(session, parent, kind, actor_id):
    previous = await session.scalar(
        select(AnalysisRecord).where(
            AnalysisRecord.kind == kind,
            AnalysisRecord.inputs["monitoring_run_id"].as_string() == parent.id,
        )
    )
    if previous:
        return previous.run_id
    run = runningRecord(
        id=str(uuid4()),
        model_id=parent.model_id,
        model_version=parent.model_version,
        dataset_id=parent.dataset_id,
        operation="monitor",
        status="queued",
    )
    session.add(run)
    await session.flush()
    session.add(RunExecutionRecord(run_id=run.id, actor_id=actor_id))
    session.add(
        AnalysisRecord(
            run_id=run.id, kind=kind, inputs={"monitoring_run_id": parent.id}
        )
    )
    await session.flush()
    return run.id


# ---------------------------------------------------------------- inputs
async def _bound(sessions, run):
    from app.services.upload_service import InventoryRecord, DatasetRecord

    async with sessions() as session:
        inventory = await session.get(InventoryRecord, run.model_id)
        dataset = (
            await session.get(DatasetRecord, inventory.dataset_version_id)
            if inventory and inventory.dataset_version_id
            else None
        )
    if not inventory or not dataset:
        raise PackageError("AI analysis requires an uploaded model with a bound dataset")
    return inventory, dataset


async def _data_input(sessions, dataset) -> str:
    from app.services.upload_service import dataset_root

    source = dataset_root(dataset) / "partitions/train.csv"
    if digest(source) != dataset.statistics["partition_sha256"]["train"]:
        raise PackageError("Development partition changed")
    # Training labels are permitted; protected evaluation partitions are never mounted.
    return await ai.register_input(
        sessions,
        "data",
        {
            "development.csv": await asyncio.to_thread(source.read_bytes),
            "dataset.json": json.dumps(dataset.descriptor).encode(),
        },
        meta={"dataset_version_id": dataset.id, "partition": "train"},
    )


async def _document_input(sessions, inventory) -> str:
    async with sessions() as session:
        accepted = await session.scalar(
            select(ReviewRecord).where(
                ReviewRecord.run_id == inventory.document_run_id,
                ReviewRecord.decision.in_(("accepted", "accepted_with_conditions")),
                ReviewRecord.status == "decided",
            )
        )
        if not accepted:
            raise PackageError("Current accepted model document is required")
        artifact = await session.get(ArtifactRecord, accepted.artifact_id)
        content = await artifact_bytes(artifact)
    return await ai.register_input(
        sessions,
        "document",
        {"model_document.md": content},
        meta={"artifact_id": artifact.id, "review_id": accepted.id},
    )


async def _reports_input(sessions, model_id) -> str:
    from harness.metrics_bundle import BundleError, bundle_from_validation_report
    from harness.validation_report import METRICS_BUNDLE_FILE

    async with sessions() as session:
        report = await session.scalar(
            select(ReportRecord)
            .where(ReportRecord.model_id == model_id, ReportRecord.kind == "validation")
            .order_by(ReportRecord.created_at.desc())
            .limit(1)
        )
    files: dict[str, bytes] = {}
    meta: dict = {"report_id": report.id if report else None}
    if report:
        try:
            bundle = bundle_from_validation_report(report.payload)
            files[METRICS_BUNDLE_FILE] = bundle.model_dump_json(indent=2).encode()
        except BundleError as exc:
            meta["bundle_error"] = str(exc)
        narrative = (
            (report.payload.get("agent_review") or {}).get("exports", {}).get("validation_narrative.md")
        )
        if narrative:
            files["validation_narrative.md"] = narrative.encode()
    if not files:
        files["README.md"] = b"No validation report is available for this model.\n"
    return await ai.register_input(sessions, "reports", files, meta=meta)


async def _monitoring_input(sessions, monitoring_run_id, triage_report=None) -> str:
    async with sessions() as session:
        monitoring = await session.scalar(
            select(MonitoringRecord).where(MonitoringRecord.run_id == monitoring_run_id)
        )
    if not monitoring:
        raise PackageError("Monitoring evidence missing")
    window = settings.storage_path / "monitoring" / monitoring_run_id
    batch = None
    if monitoring.payload.get("input_scope") in ai.AGENT_READABLE_SCOPES:
        batch = checked_file(window, "current.csv")
        if digest(batch) != monitoring.payload.get("input_sha256"):
            raise PackageError("Monitoring batch changed since the official PSI run")
    return await ai.register_monitoring(
        sessions, monitoring.payload, batch=batch, triage_report=triage_report
    )


# ---------------------------------------------------------------- the challenger
async def _evaluate_challenger(inventory, dataset, files):
    """Score the exported challenger outside the agent and return aggregates only."""
    from harness.scoring import ScoringJob
    from app.services.upload_service import dataset_root
    from app.services.monitoring_service import features_only
    from app.services.native_scoring import service

    challenger = {
        name[len("challenger/") :]: content
        for name, content in files.items()
        if name.startswith("challenger/")
    }
    if not challenger:
        return {"status": "NOT_ASSESSABLE", "reason": "Validator did not export a challenger"}
    with tempfile.TemporaryDirectory(prefix="challenger-") as temp:
        folder = Path(temp)
        artifact = folder / "artifact"
        for name, content in challenger.items():
            target = artifact / name
            if not target.resolve().is_relative_to(artifact.resolve()):
                return {"status": "NOT_ASSESSABLE", "reason": "Challenger path escapes its folder"}
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        results = {}
        for partition in ("validation", "test"):
            labels_file = dataset_root(dataset) / f"partitions/{partition}.csv"
            if digest(labels_file) != dataset.statistics["partition_sha256"][partition]:
                raise PackageError("Challenger evaluation partition changed")
            inputs = folder / (partition + ".csv")
            await asyncio.to_thread(
                features_only,
                labels_file,
                inputs,
                inventory.manifest["feature_order"],
                None,
                2_000_000,
            )
            scored = await service().score(
                ScoringJob(
                    artifact_path=str(artifact),
                    input_path=str(inputs),
                    protected=True,
                    timeout_s=settings.scoring_timeout_seconds,
                )
            )
            if scored.status != "ok":
                return {
                    "status": "NOT_ASSESSABLE",
                    "reason": "Challenger scorer failed outside the agent",
                }
            with labels_file.open() as stream:
                rows = list(csv.DictReader(stream))
            if len(rows) != len(scored.predictions.probabilities):
                raise PackageError("Challenger scores and label rows do not align")
            labels = [
                int(
                    float(row[dataset.descriptor["target_column"]])
                    == float(dataset.descriptor["positive_class"])
                )
                for row in rows
            ]
            results[partition] = {
                "auc": auc(labels, scored.predictions.probabilities),
                "n_rows": len(labels),
                "status": "REVIEW",
                "policy": "Comparison only; acceptance requires human review",
            }
    return results


def auc(labels, scores):
    """Mann–Whitney AUC with tied ranks; undefined with a single outcome class."""
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None
    ordered = sorted(zip(scores, labels))
    total = 0.0
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        rank = (index + 1 + end) / 2
        total += rank * sum(label for _, label in ordered[index:end])
        index = end
    return (total - positives * (positives + 1) / 2) / (positives * negatives)


# ---------------------------------------------------------------- execution
async def _context(sessions, run, package, analysis, inventory, dataset, report):
    context = {
        "policy_version": POLICY_VERSION,
        "model_id": run.model_id,
        "development_data_id": await _data_input(sessions, dataset),
    }
    if analysis.kind == "validation":
        if report is None:
            raise PackageError("Validation analysis needs the official validation report")
        context.update(
            model_package_id=package.id,
            document_id=await _document_input(sessions, inventory),
            metrics_bundle_id=await ai.register_metrics_bundle(sessions, report),
        )
        return context
    parent = analysis.inputs["monitoring_run_id"]
    triage_report = None
    if analysis.kind == "investigation":
        async with sessions() as session:
            prior = await session.scalar(
                select(AnalysisRecord).where(
                    AnalysisRecord.kind == "triage",
                    AnalysisRecord.inputs["monitoring_run_id"].as_string() == parent,
                )
            )
        exports = ((prior.result or {}).get("exports") or {}) if prior else {}
        if exports.get("triage_report.md"):
            triage_report = exports["triage_report.md"].encode()
        context.update(
            document_id=await _document_input(sessions, inventory),
            validation_report_id=await _reports_input(sessions, run.model_id),
        )
    context["monitoring_run_id"] = await _monitoring_input(sessions, parent, triage_report)
    return context


async def execute(run, package, control, analysis, sessions, *, report=None):
    """Run the analysis's agent run to its end and record the accepted exports."""
    kind = analysis.kind
    inventory, dataset = await _bound(sessions, run)
    actor = Actor(id=control.actor_id, role=await actor_role(sessions, control.actor_id))
    context = await _context(sessions, run, package, analysis, inventory, dataset, report)
    prompt = None
    if kind == "validation":
        prompt = (
            "Follow the validation report template and export the narrative early so you "
            "can act on the platform's checks. Keep the required numbered headings. "
            "In prose, use bullets instead of numbered list markers, refer to sections "
            "by title, and put identifiers and source paths in backticks. "
            "Quote numerical metric values exactly as supplied in the official metrics "
            "bundle. Describe other counts in words and do not invent metrics. "
            "Address export feedback before submitting finding proposals."
        )
    ai_run_id = await ai.create_run(
        sessions, kind, actor, context, prompt=prompt, platform_run_id=run.id
    )
    async with sessions() as session:
        saved = await session.get(AnalysisRecord, run.id)
        saved.inputs = {**saved.inputs, "ai_native_run_id": ai_run_id}
        await session.commit()

    cancel = asyncio.Event()

    async def watch():
        while True:
            async with sessions() as session:
                saved = await session.get(RunExecutionRecord, run.id)
            if saved.cancel_requested:
                cancel.set()
                return
            await asyncio.sleep(0.25)

    watcher = asyncio.create_task(watch())
    try:
        async with runtime(sessions) as runner:
            result = await ai.execute_run(sessions, runner, ai_run_id, cancel=cancel, read_workspace=ai.docker_read_workspace)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    if result.status != "completed":
        detail = f"; {result.text}" if result.text else ""
        raise PackageError(f"AI {kind} run failed: {result.error_code or result.status}{detail}"[:1000])
    files, _ = await ai.latest_exports(sessions, ai_run_id)
    agent_run = await ai._run(sessions, ai_run_id)
    challenger = None
    if kind == "validation":
        challenger = await _evaluate_challenger(inventory, dataset, files)
    exports = {name: files[name] for name in REQUIRED_EXPORTS[kind]}
    async with sessions() as session:
        record = await session.get(runningRecord, run.id)
        for name, raw in exports.items():
            await save_artifact(
                session,
                record,
                raw,
                name,
                "application/json" if name.endswith(".json") else "text/markdown",
            )
        if any(name.startswith("challenger/") for name in files):
            import io
            import zipfile

            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, content in sorted(files.items()):
                    if name.startswith("challenger/"):
                        archive.writestr(name[len("challenger/") :], content)
            await save_artifact(session, record, buffer.getvalue(), "challenger.zip", "application/zip")
        ids = list(
            await session.scalars(
                select(ArtifactRecord.id)
                .where(ArtifactRecord.run_id == run.id)
                .order_by(ArtifactRecord.created_at)
            )
        )
        saved = await session.get(AnalysisRecord, run.id)
        saved.result = {
            "ai_native_run_id": ai_run_id,
            "exports": {name: raw.decode() for name, raw in exports.items()},
            "artifact_ids": ids,
            "proposal_ids": list(result.proposal_ids),
            "outcome": agent_run.outcome,
            **({"challenger": challenger} if challenger is not None else {}),
        }
        next_id = None
        if kind == "triage" and agent_run.outcome.get("outcome") == "problem":
            parent = await session.get(runningRecord, analysis.inputs["monitoring_run_id"])
            next_id = await enqueue(session, parent, "investigation", control.actor_id)
        await session.commit()
    if next_id:
        from app.services.run_service import publish_run

        try:
            await asyncio.to_thread(publish_run, next_id)
        except Exception:
            pass  # Durable run recovery dispatches it.
    return result.model_copy(update={"artifact_ids": ids})
