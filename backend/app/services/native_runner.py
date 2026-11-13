"""Archive intake through the harness's AI-native runs.

The survey and each candidate's intake are ordinary `ai_native_service` runs: registered,
hash-checked inputs, the harness gateway, `check_exports` and correction rounds. This module only
registers the archive's inputs, runs the survey and the intakes it queues, and turns their accepted
exports into the intake plan that a person reviews at Gate 0.
"""

import asyncio
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update

from contracts.harness import Actor
from app.db.tables import AuditRecord
from app.services import ai_native_service as ai
from app.services import intake_service as intake
from app.services.harness_adapter import actor_role, runtime
from app.services.package_service import PackageError, checked_file, digest

POLICY_VERSION = "1.0.0"
STAGE_TIMEOUT_S = 1800
HEARTBEAT_S = 120


class IntakeFailed(Exception):
    pass


async def _register_inputs(sessions, archive) -> tuple[str, str]:
    """The archive as an `archive` input, and every dataset's development partition as one
    `data` input with a folder per dataset version ID, which is what intake maps models to."""
    from app.services.upload_service import DatasetRecord, dataset_root

    files = intake.root(archive.id) / "files"
    for item in archive.files:
        if digest(checked_file(files, item["path"])) != item["sha256"]:
            raise IntakeFailed("Archive files changed after upload")
    archive_input = await ai.register_input(
        sessions, "archive", files, meta={"intake_archive_id": archive.id}
    )
    from app.services.dataset_deletion import visible_datasets

    async with sessions() as session:
        datasets = list(await session.scalars(visible_datasets()))
    with tempfile.TemporaryDirectory(prefix="intake-data-") as temp:
        root = Path(temp)
        for dataset in datasets:
            partition = dataset_root(dataset) / "partitions/train.csv"
            expected = dataset.statistics.get("partition_sha256", {}).get("train")
            if not expected or digest(partition) != expected:
                raise IntakeFailed("Dataset partition checksum mismatch")
            target = root / dataset.id
            target.mkdir()
            shutil.copyfile(partition, target / "development.csv")
            (target / "dataset.json").write_text(
                json.dumps(
                    {
                        "dataset_version_id": dataset.id,
                        "dataset_id": dataset.dataset_id,
                        "version": dataset.version,
                        "descriptor": dataset.descriptor,
                    }
                )
            )
        data_input = await ai.register_input(
            sessions, "data", root, meta={"intake_archive_id": archive.id}
        )
    return archive_input, data_input


async def _audit(sessions, archive, action, payload):
    async with sessions() as session:
        session.add(
            AuditRecord(
                actor_id=archive.actor_id,
                resource_id=archive.id,
                action=action,
                payload=payload,
            )
        )
        await session.commit()


async def _execute(runner, sessions, archive, run_id):
    """Run one agent run; stop it when the archive is cancelled. Keeps the archive's heartbeat."""
    cancel = asyncio.Event()

    async def watch():
        beat = 0.0
        while True:
            async with sessions() as session:
                saved = await session.get(intake.ArchiveRecord, archive.id)
            if not saved or saved.status != "running":
                cancel.set()
                return
            beat += 0.5
            if beat >= HEARTBEAT_S:
                beat = 0.0
                await _audit(sessions, archive, "intake_heartbeat", {"run_id": run_id})
            await asyncio.sleep(0.5)

    watcher = asyncio.create_task(watch())
    try:
        return await asyncio.wait_for(
            ai.execute_run(sessions, runner, run_id, cancel=cancel, read_workspace=ai.docker_read_workspace), timeout=STAGE_TIMEOUT_S
        )
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


async def _candidate(sessions, archive, candidate, run_id, result):
    """One plan entry from an intake run. A failed run is kept, with its problem, so the person
    sees it at Gate 0 and can only leave it out."""
    if isinstance(result, BaseException) or result.status != "completed":
        code = (
            type(result).__name__
            if isinstance(result, BaseException)
            else result.error_code or result.status
        )
        return {
            "candidate_id": candidate["candidate_id"],
            "fitted_model": candidate["fitted_model"],
            "executable": False,
            "execution_error": None,
            "manifest": {},
            "dataset_mapping": None,
            "scoring": {"entry_point": None, "wrapper": False},
            "problems": [f"Intake run failed: {code}"],
            "files": candidate["files"],
            "intake_run_id": run_id,
        }
    files, _ = await ai.latest_exports(sessions, run_id)
    data = json.loads(files["intake.json"])
    if data["scoring"].get("wrapper"):
        wrapper = {
            name[len("scoring_wrapper/") :]: content
            for name, content in files.items()
            if name.startswith("scoring_wrapper/")
        }
        folder = intake.root(archive.id) / "wrappers" / candidate["candidate_id"]
        if folder.exists():
            shutil.rmtree(folder)
        hashes = {}
        for relative, content in sorted(wrapper.items()):
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            hashes[relative] = digest(target)
        data["scoring"]["wrapper_files"] = sorted(hashes)
        data["scoring"]["wrapper_sha256"] = intake.plan_hash(hashes)
    return {**data, "files": candidate["files"], "intake_run_id": run_id}


async def process_intake(identity, sessions):
    async with sessions() as session:
        changed = await session.execute(
            update(intake.ArchiveRecord)
            .where(
                intake.ArchiveRecord.id == identity,
                intake.ArchiveRecord.status == "queued",
            )
            .values(status="running")
        )
        await session.commit()
        if changed.rowcount != 1:
            return
        archive = await session.get(intake.ArchiveRecord, identity)
    try:
        intake.require_intake()
        archive_input, data_input = await _register_inputs(sessions, archive)
        actor = Actor(id=archive.actor_id, role=await actor_role(sessions, archive.actor_id))
        survey_id = await ai.create_run(
            sessions,
            "survey",
            actor,
            {"policy_version": POLICY_VERSION, "archive_id": archive_input},
            follow_up={"development_data_id": data_input},
        )
        await _audit(sessions, archive, "intake_survey_started", {"run_id": survey_id})
        async with runtime(sessions) as runner:
            result = await _execute(runner, sessions, archive, survey_id)
            if result.status != "completed":
                raise IntakeFailed(f"Survey run failed: {result.error_code or result.status}")
            survey_run = await ai._run(sessions, survey_id)
            if survey_run.outcome.get("personal_data_suspected"):
                async with sessions() as session:
                    saved = await session.get(intake.ArchiveRecord, identity)
                    saved.status, saved.error = (
                        "blocked",
                        "Survey flagged suspected personal data; no candidates were processed",
                    )
                    await session.commit()
                return
            survey_files, _ = await ai.latest_exports(sessions, survey_id)
            survey = json.loads(survey_files["survey.json"])
            intake_ids = survey_run.outcome.get("intake_run_ids", [])
            if len(intake_ids) != len(survey["candidates"]):
                raise IntakeFailed("The survey's candidates and intake runs do not match")
            await _audit(sessions, archive, "intake_runs_started", {"run_ids": intake_ids})
            results = await asyncio.gather(
                *(_execute(runner, sessions, archive, run_id) for run_id in intake_ids),
                return_exceptions=True,
            )
        candidates = [
            await _candidate(sessions, archive, candidate, run_id, outcome)
            for candidate, run_id, outcome in zip(survey["candidates"], intake_ids, results)
        ]
        if candidates and all(not c["manifest"] for c in candidates):
            raise IntakeFailed("Every intake run failed")
        plan = {
            "candidates": candidates,
            "unassigned_files": survey["unassigned_files"],
            "archive_sha256": archive.sha256,
            "survey_run_id": survey_id,
        }
        async with sessions() as session:
            saved = await session.get(intake.ArchiveRecord, identity)
            if saved.status != "running":
                return
            saved.plan = plan
            if not any(c["fitted_model"] != "training_code_only" for c in candidates):
                saved.status, saved.error = (
                    "blocked",
                    "No fitted model was found; training code alone cannot be registered",
                )
            else:
                saved.status = "awaiting_review"
            await session.commit()
    except Exception as exc:
        reason = (
            str(exc)
            if isinstance(exc, (IntakeFailed, PackageError))
            else "Intake did not complete; inspect safe audit events and harness availability"
        )
        async with sessions() as session:
            saved = await session.get(intake.ArchiveRecord, identity)
            if saved.status == "running":
                saved.status, saved.error = "failed", reason[:1000]
            await session.commit()
        await _audit(
            sessions,
            archive,
            "intake_failed",
            {"error": type(exc).__name__, "at": datetime.now(timezone.utc).isoformat()},
        )
