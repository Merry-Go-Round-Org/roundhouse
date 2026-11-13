"""Persisted monitoring jobs using the legacy owner's single PSI implementation."""

import asyncio
import csv
import json
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4
from sqlalchemy import String, JSON, ForeignKey, select
from sqlalchemy.orm import Mapped, mapped_column
from app.config import settings
from app.db.tables import (
    Base,
    PackageRecord,
    ModelRecord,
    MonitoringRecord,
    RunExecutionRecord,
    AuditRecord,
    runningRecord,
)
from app.services.package_service import (
    verify_snapshot,
    digest,
    read_inputs,
    PackageError,
)
from app.services.legacy_scoring import (
    create_command,
    docker,
    validate_predictions,
    ScoringError,
)
from app.services.report_service import load_module
from contracts.harness import HarnessResult


class MonitorJob(Base):
    __tablename__ = "monitor_jobs"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class MonitoringBaseline(Base):
    __tablename__ = "monitoring_baselines"
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id"), primary_key=True)
    package_sha256: Mapped[str] = mapped_column(String(64))
    dataset_version_id: Mapped[str] = mapped_column(String(36))
    payload: Mapped[dict] = mapped_column(JSON)
    reference_ids: Mapped[list] = mapped_column(JSON)


def trigger_band(result):
    import math
    values = [result.get("score", {}), *result.get("features", {}).values()]
    psis = [v["psi"] for v in values if isinstance(v, dict) and isinstance(v.get("psi"), (int, float)) and math.isfinite(v["psi"]) and v["psi"] >= 0]
    worst = max(psis, default=None)
    return "not_assessable" if worst is None else "red" if worst > .25 else "amber" if worst >= .10 else "green"


def lifecycle_after_monitoring(current, breach):
    """A breach makes the model due for re-validation; otherwise an approved model is now monitored."""
    if breach:
        return "revalidation_due"
    return "monitored" if current == "approved" else current


def engine():
    return load_module(
        "backend_legacy_stability", settings.legacy_models_root / "stability.py"
    )


def features_only(source, target, features, shift=None, max_rows=20000):
    ids = []
    with (
        source.open(newline="", encoding="utf-8-sig") as stream,
        target.open("w", newline="") as output,
    ):
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not set(["row_id", *features]).issubset(
            reader.fieldnames
        ):
            raise PackageError(
                "Monitoring data lacks declared feature columns or row_id"
            )
        writer = csv.DictWriter(output, fieldnames=["row_id", *features])
        writer.writeheader()
        for i, row in enumerate(reader):
            if i >= max_rows:
                break
            item = {key: row[key] for key in ["row_id", *features]}
            if shift:
                item[shift["feature"]] = str(
                    float(item[shift["feature"]]) * shift["factor"]
                )
            writer.writerow(item)
            ids.append(row["row_id"])
    read_inputs(target, features, max_rows=max_rows)
    return ids


async def score_window(package, inputs, output, max_rows=20000):
    snapshot = await asyncio.to_thread(
        verify_snapshot, package.sha256, package.file_hashes
    )
    manifest = json.loads((snapshot / "package/manifest.json").read_text())
    if manifest.get("ai_native"):
        from app.services.native_scoring import score
        output.write_bytes(await score(package, inputs, protected=True))
        return
    ids = await asyncio.to_thread(
        read_inputs, inputs, manifest["feature_order"], max_rows
    )
    output_limit = max(2_000_000, min(max_rows * 120, 64_000_000))
    with tempfile.TemporaryDirectory(prefix="monitor-score-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        await asyncio.to_thread(shutil.copytree, snapshot / "package", root / "package")
        (root / "data").mkdir()
        shutil.copyfile(inputs, root / "data/development_inputs.csv")
        name = "monitor-score-" + uuid4().hex
        try:
            await docker(
                *create_command(
                    name, settings.scoring_image, root, package.language, output_limit
                )
            )
            content = await docker(
                "start",
                "--attach",
                name,
                timeout=settings.scoring_timeout_seconds,
                limit=output_limit,
            )
            if (
                await docker("inspect", "--format={{.State.ExitCode}}", name)
            ).strip() != b"0":
                raise ScoringError("Monitoring scorer failed")
            content = validate_predictions(content, ids)
            output.write_bytes(content)
        finally:
            await asyncio.shield(docker("rm", "--force", name))


async def execute_monitor(run, sessions):
    from app.services.upload_service import InventoryRecord, DatasetRecord, dataset_root

    async with sessions() as session:
        job = await session.get(MonitorJob, run.id)
        inventory = await session.get(InventoryRecord, run.model_id)
        package = await session.scalar(
            select(PackageRecord).where(PackageRecord.model_id == run.model_id)
        )
        control = await session.get(RunExecutionRecord, run.id)
        baseline = await session.get(MonitoringBaseline, run.model_id)
        if not job or not inventory or not package:
            raise PackageError(
                "Upload a monitoring batch or request a declared simulation for an uploaded model"
            )
        if inventory.manifest.get("task") != "binary_classification":
            raise PackageError("NOT_APPLICABLE: monitoring requires a binary model")
        dataset = await session.get(DatasetRecord, inventory.dataset_version_id)
    if dataset is None:
        raise PackageError("A verified dataset version is required for monitoring")
    folder = settings.storage_path / "monitoring" / run.id
    folder.mkdir(parents=True, exist_ok=True)
    root = dataset_root(dataset)
    features = inventory.manifest["feature_order"]
    policy = json.loads(
        (settings.legacy_models_root / "validation_policy.json").read_text()
    ).get("stability")
    psi = engine()
    if baseline is None:
        source = root / "partitions/train.csv"
        if digest(source) != dataset.statistics["partition_sha256"]["train"]:
            raise PackageError("Development partition checksum mismatch")
        reference = folder / "reference.csv"
        scores = folder / "reference_scores.csv"
        ids = await asyncio.to_thread(features_only, source, reference, features)
        await score_window(package, reference, scores)
        frozen = await asyncio.to_thread(
            psi.freeze_baseline,
            model_id=package.name,
            feature_order=features,
            reference_inputs=reference,
            reference_scores=scores,
            reference_scope="development",
            artifact_sha256=inventory.manifest["artifact_sha256"],
            config=policy,
        )
        async with sessions() as session:
            baseline = await session.get(MonitoringBaseline, run.model_id)
            if baseline is None:
                baseline = MonitoringBaseline(
                    model_id=run.model_id,
                    package_sha256=package.sha256,
                    dataset_version_id=dataset.id,
                    payload=frozen,
                    reference_ids=ids,
                )
                session.add(baseline)
                await session.commit()
    if (
        baseline.package_sha256 != package.sha256
        or baseline.dataset_version_id != dataset.id
    ):
        raise PackageError(
            "Frozen monitoring baseline is bound to another model/dataset version"
        )
    current = folder / "current.csv"
    scores = folder / "current_scores.csv"
    if job.payload["mode"] == "simulated":
        source = root / "partitions/validation.csv"
        if digest(source) != dataset.statistics["partition_sha256"]["validation"]:
            raise PackageError("Simulation partition checksum mismatch")
        shift = job.payload["shift"]
        if shift["feature"] not in features:
            raise PackageError("Simulation feature is not a model input")
        current_ids = await asyncio.to_thread(
            features_only, source, current, features, shift
        )
    else:
        source = folder / "uploaded.csv"
        if digest(source) != job.payload["input_sha256"]:
            raise PackageError("Monitoring upload checksum mismatch")
        # Uploaded batches require exactly feature columns; target and audit-only columns rejected.
        current_ids = await asyncio.to_thread(read_inputs, source, features, 20000)
        if len(current_ids) > 20000:
            raise PackageError("Monitoring batch exceeds 20000-row limit")
        shutil.copyfile(source, current)
    if set(current_ids).intersection(baseline.reference_ids):
        raise PackageError("Monitoring window overlaps the frozen reference cohort")
    await score_window(package, current, scores)
    result = await asyncio.to_thread(
        psi.evaluate_window,
        baseline=baseline.payload,
        current_inputs=current,
        current_scores=scores,
        expected_model_id=package.name,
        expected_artifact_sha256=inventory.manifest["artifact_sha256"],
    )
    result.update(
        input_sha256=digest(current),
        predictions_sha256=digest(scores),
        simulated=job.payload["mode"] == "simulated",
        shift=job.payload.get("shift"),
        dataset_version_id=dataset.id,
        package_sha256=package.sha256,
        runtime_image=settings.scoring_image,
        input_scope="validation_partition_simulation"
        if job.payload["mode"] == "simulated"
        else "uploaded_batch",
        baseline_partition="train",
        max_rows=20000,
        window_label=job.payload["window_label"],
    )
    from app.services.artifact_service import save_artifact

    async with sessions() as session:
        saved = await session.get(runningRecord, run.id)
        if saved.status != "running":
            raise PackageError("Monitoring run was stopped")
        event = MonitoringRecord(
            id=str(uuid4()), model_id=run.model_id, run_id=run.id, payload=dict(result)
        )
        session.add(event)
        model = await session.get(ModelRecord, run.model_id)
        if settings.ai_native_enabled:
            from app.services.native_analysis import enqueue
            band = trigger_band(result)
            result["trigger_band"] = band
            if band in ("amber", "red"):
                result["analysis_run_id"] = await enqueue(session, run, "triage" if band == "amber" else "investigation", control.actor_id)
            event.payload = dict(result)
            # A red band is a breach. The investigation proposes what to do; it does not run validation itself.
            model.lifecycle_status = lifecycle_after_monitoring(model.lifecycle_status, band == "red")
        elif result["revalidation_required"]:
            model.lifecycle_status = lifecycle_after_monitoring(model.lifecycle_status, True)
            from app.services.run_service import createRun
            from app.schemas.runs import RunCreate

            validation = await createRun(
                session,
                run.model_id,
                RunCreate(
                    operation="validate",
                    model_version=run.model_version,
                    dataset_id=run.dataset_id,
                ),
            )
            session.add(
                RunExecutionRecord(run_id=validation.id, actor_id=control.actor_id)
            )
            result["revalidation_run_id"] = validation.id
            event.payload = dict(result)
        else:
            model.lifecycle_status = lifecycle_after_monitoring(model.lifecycle_status, False)
        artifact = await save_artifact(
            session,
            run,
            json.dumps(result, allow_nan=False).encode(),
            "monitoring.json",
            "application/json",
        )
        session.add(
            AuditRecord(
                actor_id=control.actor_id,
                resource_id=run.id,
                action="monitoring_completed",
                payload=result,
            )
        )
        await session.commit()
    dispatch_id = result.get("analysis_run_id") or result.get("revalidation_run_id")
    if dispatch_id:
        from app.services.run_service import publish_run

        try:
            await asyncio.to_thread(publish_run, dispatch_id)
        except Exception:
            # A durable queued validation remains for the recovery scheduler.
            pass
    return HarnessResult(status="completed", artifact_ids=[artifact.id])
