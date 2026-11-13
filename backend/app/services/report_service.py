"""Run the legacy owner's validation suite in an isolated runtime and retain its evidence."""

import asyncio
import importlib.util
import json
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4
from sqlalchemy import select
from jsonschema import Draft202012Validator, FormatChecker
from app.config import settings, BACKEND_DIR
from app.db.tables import (
    PackageRecord,
    ReportRecord,
    ReviewRecord,
    FindingRecord,
    AuditRecord,
    runningRecord,
)
from app.services.artifact_service import save_artifact
from app.services.package_service import verify_snapshot, digest, PackageError
from app.services.legacy_scoring import docker, create_command, ScoringError

ROOT = BACKEND_DIR.parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_report(payload):
    schema = json.loads(
        (settings.legacy_models_root / "validation_report.schema.json").read_text()
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
    handoff = load_module(
        "legacy_handoff",
        settings.legacy_models_root / "integration/build_validation_handoff.py",
    )
    handoff.summary_report(payload)


async def registered_package(session, run):
    package = await session.scalar(
        select(PackageRecord).where(PackageRecord.model_id == run.model_id)
    )
    if package is None:
        raise PackageError(
            "Register a matching legacy package before starting this operation."
        )
    if (package.version, package.dataset_id) != (run.model_version, run.dataset_id):
        raise PackageError("Run does not match the registered package version.")
    await asyncio.to_thread(verify_snapshot, package.sha256, package.file_hashes)
    return package


def validation_bundle(destination, package, uploaded_bundle=None):
    root = settings.legacy_models_root.resolve()
    # Trusted validator receives development evidence, never the repository, credentials,
    # raw datasets or private-test files. Agents never receive this bundle.
    allowed = {".py", ".R", ".json", ".csv", ".rds", ".joblib"}
    for source in root.rglob("*"):
        relative = source.relative_to(root)
        if any(
            part.startswith(".")
            or part in ("private_test", "__pycache__", "reproduction", "tests")
            for part in relative.parts
        ):
            continue
        if (
            not source.is_file()
            or source.suffix not in allowed
            or source.name.endswith("_split_ids.csv")
        ):
            continue
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise PackageError("Unsafe validation source file.")
        target = destination / "legacy_models" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    for name in ("validate_all_models.py", "standardize_exl_models.py"):
        shutil.copyfile(ROOT / name, destination / name)
    copied = destination / "legacy_models" / package.name
    original = settings.storage_path / "packages" / package.sha256 / "package"
    if uploaded_bundle is not None:
        # The validator executes exactly the uploaded bytes. Never substitute a
        # similarly named repository model or its saved evaluation evidence.
        if copied.exists():
            shutil.rmtree(copied)
        shutil.copytree(uploaded_bundle, copied)
        # Portable bundles carry their own evidence; no other model's parent
        # evidence may be used to fill missing references.

    if digest(copied / "manifest.json") != digest(original / "manifest.json"):
        raise PackageError(
            "Source package changed after registration; register its new version."
        )
    artifact = "model.rds" if package.language == "R" else "model.joblib"
    if digest(copied / artifact) != digest(original / artifact):
        raise PackageError("Validation artifact differs from registered package.")
    return {
        str(p.relative_to(destination)): digest(p)
        for p in destination.rglob("*")
        if p.is_file()
    }


async def generate_validation(package, sessions=None):
    uploaded_bundle = None
    from app.db.session import SessionFactory

    try:
        from app.services.upload_service import InventoryRecord
    except ImportError:
        InventoryRecord = None
    if InventoryRecord is not None:
        async with (sessions or SessionFactory)() as session:
            inventory = await session.get(InventoryRecord, package.model_id)
        if inventory is not None:
            from app.services.upload_service import DatasetRecord
            from app.services.live_validation import generate_uploaded_validation

            async with (sessions or SessionFactory)() as session:
                dataset = (
                    await session.get(DatasetRecord, inventory.dataset_version_id)
                    if inventory.dataset_version_id
                    else None
                )
            if dataset is None:
                raise PackageError(
                    "NOT_APPLICABLE: model has no verified binary dataset"
                )
            return await generate_uploaded_validation(package, inventory, dataset)
    with tempfile.TemporaryDirectory(prefix="validation-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        hashes = await asyncio.to_thread(
            validation_bundle, root, package, uploaded_bundle
        )
        name = "model-validation-" + uuid4().hex
        command = create_command(name, settings.scoring_image, root, "Python")
        i = command.index("--workdir=/inputs/package")
        command[i] = "--workdir=/inputs"
        # Replace the scorer wrapper, retaining all isolation flags and env clearing.
        i = command.index("timeout")
        code = (
            "import runpy,sys,contextlib,pathlib; "
            "sys.argv=['validate_all_models.py','--models-dir','/inputs/legacy_models','--policy',"
            "'/inputs/legacy_models/validation_policy.json','--out','/tmp/reports','--execute-scorers']; "
            "exec(\"with contextlib.redirect_stdout(sys.stderr):\\n runpy.run_path('/inputs/validate_all_models.py',run_name='__backend_validation__')['main']()\"); "
            f"sys.stdout.write(pathlib.Path('/tmp/reports/{package.name}/validation_report.json').read_text())"
        )
        command[i:] = [
            "timeout",
            "--signal=KILL",
            str(settings.run_timeout_seconds),
            "python",
            "-c",
            code,
        ]
        try:
            await docker(*command)
            raw = await docker(
                "start", "--attach", name, timeout=settings.run_timeout_seconds
            )
            if (
                await docker("inspect", "--format={{.State.ExitCode}}", name)
            ).strip() != b"0":
                raise ScoringError("Validation execution failed.")
            payload = json.loads(raw)
            await asyncio.to_thread(check_report, payload)
        finally:
            await asyncio.shield(docker("rm", "--force", name))
    if payload["model_id"] != package.name:
        raise PackageError("Validation report identity mismatch.")
    snapshot = await asyncio.to_thread(
        verify_snapshot, package.sha256, package.file_hashes
    )
    manifest = json.loads((snapshot / "package/manifest.json").read_text())
    if payload["artifact_sha256"] != manifest["artifact_sha256"]:
        raise PackageError("Validation report artifact mismatch.")
    return payload, hashes


async def persist_report(session, run, kind, payload, actor_id, blockers=None):
    current = await session.get(runningRecord, run.id)
    if current.status != "running":
        raise PackageError("Run was cancelled or stopped before the report was stored.")
    raw = json.dumps(payload, indent=2, allow_nan=False).encode()
    artifact = await save_artifact(
        session, run, raw, f"{kind}.json", "application/json"
    )
    if kind == "validation":
        from app.db.tables import ModelRecord

        model = await session.get(ModelRecord, run.model_id)
        model.lifecycle_status = "validated"
    report = ReportRecord(
        id=str(uuid4()),
        model_id=run.model_id,
        run_id=run.id,
        kind=kind,
        artifact_id=artifact.id,
        payload=payload,
    )
    session.add(report)
    await session.flush()
    review = ReviewRecord(
        id=str(uuid4()),
        run_id=run.id,
        artifact_id=artifact.id,
        artifact_version=artifact.version,
        requested_by=actor_id,
        blockers=blockers or [],
    )
    session.add(review)
    for check in payload.get("checks", []):
        if check["status"] in ("FAIL", "REVIEW", "NOT_ASSESSABLE"):
            session.add(
                FindingRecord(
                    id=str(uuid4()),
                    model_id=run.model_id,
                    report_id=report.id,
                    check_id=check["check_id"],
                    severity=check["status"],
                    summary=check["summary"],
                )
            )
    session.add(
        AuditRecord(
            actor_id=actor_id,
            resource_id=run.id,
            action="report_created",
            payload={
                "report_id": report.id,
                "artifact_id": artifact.id,
                "sha256": artifact.sha256,
            },
        )
    )
    await session.flush()
    return report, review


def report_read(record):
    mapping = {
        "PASS": "green",
        "FAIL": "red",
        "REVIEW": "amber",
        "NOT_ASSESSABLE": "not_assessable",
        "NOT_APPLICABLE": "NOT_APPLICABLE",
    }
    return dict(
        id=record.id,
        model_id=record.model_id,
        run_id=record.run_id,
        kind=record.kind,
        artifact_id=record.artifact_id,
        metrics=[
            dict(
                name=c["check_id"],
                value=None,
                status=mapping[c["status"]],
                reason=c["summary"],
                evidence_ids=[record.artifact_id],
            )
            for c in record.payload.get("checks", [])
        ],
        evidence=record.payload,
    )
