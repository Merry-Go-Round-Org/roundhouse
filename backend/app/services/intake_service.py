"""Archive intake state, bounded extraction and Gate 0. No model is loaded on the host."""

import hashlib
import json
import re
import stat
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import uuid4

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import JSON, DateTime, String, select, update
from sqlalchemy.orm import Mapped, mapped_column
from app.config import settings
from app.db.tables import Base, AuditRecord
from app.services.proposal_service import Strict


class ArchiveRecord(Base):
    __tablename__ = "intake_archives"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(120))
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    files: Mapped[list] = mapped_column(JSON, default=list)
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    decision: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


def root(identity):
    if not re.fullmatch(r"[a-f0-9-]{36}", identity):
        raise HTTPException(422, "Invalid archive ID")
    return settings.storage_path / "intake" / identity


def unpack(
    archive, destination, *, max_bytes=512 * 1024 * 1024, max_files=5000, max_depth=3
):
    """Nested ZIPs share one count/size budget; paths never follow filesystem links."""
    count, size = 0, 0
    hashes = {}

    def extract(source, target, depth):
        nonlocal count, size
        if depth > max_depth:
            raise ValueError("Nested archive depth exceeded")
        with zipfile.ZipFile(source) as zipped:
            names = set()
            for info in zipped.infolist():
                count += 1
                path = PurePosixPath(info.filename)
                mode = info.external_attr >> 16
                if (
                    count > max_files
                    or not info.filename
                    or path.is_absolute()
                    or ".." in path.parts
                    or "\\" in info.filename
                    or "\x00" in info.filename
                    or ":" in info.filename
                    or len(path.parts) > 30
                    or any(part == ".env" or part == ".git" for part in path.parts)
                    or info.flag_bits & 1
                    or info.filename in names
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                ):
                    raise ValueError("Unsafe archive member or file count exceeded")
                names.add(info.filename)
                out = target.joinpath(*path.parts)
                if not out.resolve().is_relative_to(destination.resolve()):
                    raise ValueError("Archive path escape")
                if info.is_dir():
                    out.mkdir(parents=True, exist_ok=True)
                    continue
                size += info.file_size
                if size > max_bytes:
                    raise ValueError("Unpacked size limit exceeded")
                out.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(info) as src, out.open("xb") as dst:
                    actual, digest = 0, hashlib.sha256()
                    while block := src.read(1024 * 1024):
                        actual += len(block)
                        if actual > info.file_size or actual > max_bytes:
                            raise ValueError("Archive member size mismatch")
                        digest.update(block)
                        dst.write(block)
                hashes[str(out.relative_to(destination))] = digest.hexdigest()
                if out.suffix.lower() == ".zip":
                    extract(out, out.with_name(out.name + ".unpacked"), depth + 1)
                out.chmod(0o444)

    destination.mkdir(parents=True, exist_ok=True)
    extract(archive, destination, 0)
    return [{"path": name, "sha256": sha} for name, sha in sorted(hashes.items())]


def capabilities():
    from harness.profiles import DEFAULT_PROFILES

    kinds = ("survey", "intake", "validation", "triage", "investigation")
    return {kind: kind in DEFAULT_PROFILES for kind in kinds}


async def recover(sessions):
    """Republish durable queued/confirmed work; do not replay a live agent task."""
    import asyncio
    from app.workers.tasks import execute_intake, execute_intake_bundles

    async with sessions() as session:
        from sqlalchemy import func

        active = list(
            await session.scalars(
                select(ArchiveRecord).where(
                    ArchiveRecord.status.in_(("running", "building"))
                )
            )
        )
        for record in active:
            heartbeat = await session.scalar(
                select(func.max(AuditRecord.created_at)).where(
                    AuditRecord.resource_id == record.id
                )
            )
            last = (heartbeat or record.created_at).replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last).total_seconds() > 2100:
                record.status, record.error = (
                    "failed",
                    "Archive processing stopped responding; retry after inspecting its progress",
                )
        await session.commit()
        records = list(
            await session.scalars(
                select(ArchiveRecord)
                .where(ArchiveRecord.status.in_(("queued", "confirmed")))
                .limit(100)
            )
        )
    for record in records:
        task = execute_intake if record.status == "queued" else execute_intake_bundles
        try:
            await asyncio.to_thread(task.delay, record.id)
        except Exception:
            pass


def require_intake():
    supported = capabilities()
    if not supported["survey"] or not supported["intake"]:
        raise HTTPException(
            503,
            detail={
                "code": "ai_native_harness_required",
                "message": "The installed harness lacks survey/intake profiles. Ask the harness owner to integrate its AI Native update.",
            },
        )


class CandidateDecision(Strict):
    candidate_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    accept: bool
    manifest: dict = Field(default_factory=dict)
    dataset_mapping: dict | None = None


class GateZero(Strict):
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidates: list[CandidateDecision] = Field(min_length=1, max_length=100)
    rationale: str = Field(min_length=1, max_length=2000)


def plan_hash(plan):
    return hashlib.sha256(
        json.dumps(
            plan, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


async def confirm(session, identity, payload, who):
    if who.role not in ("developer", "admin"):
        raise HTTPException(403, "Gate 0 requires a developer or admin")
    archive = await session.get(ArchiveRecord, identity)
    if not archive:
        raise HTTPException(404, "Archive not found")
    if archive.status != "awaiting_review" or not archive.plan:
        raise HTTPException(409, "Archive has no intake plan awaiting review")
    if plan_hash(archive.plan) != payload.plan_sha256:
        raise HTTPException(409, "Intake plan changed; reload before reviewing")
    candidates = {c["candidate_id"]: c for c in archive.plan["candidates"]}
    ids = [c.candidate_id for c in payload.candidates]
    if len(set(ids)) != len(ids) or set(ids) != set(candidates):
        raise HTTPException(422, "Decide each candidate exactly once")
    bundles = []
    for decision in payload.candidates:
        if not decision.accept:
            continue
        candidate = candidates[decision.candidate_id]
        if candidate["fitted_model"] == "training_code_only":
            raise HTTPException(
                422, "Training code alone cannot be accepted as a fitted model"
            )
        if candidate.get("problems"):
            raise HTTPException(409, "Resolve candidate problems before acceptance")
        fields = {key: item.get("value") for key, item in candidate["manifest"].items()}
        fields.update(decision.manifest)
        from harness.outputs import MANIFEST_FIELDS

        if set(decision.manifest) - set(MANIFEST_FIELDS):
            raise HTTPException(422, "Gate 0 edits must use known inventory fields")
        if (
            not fields.get("name")
            or not fields.get("version")
            or not fields.get("language")
        ):
            raise HTTPException(422, "Confirm model name, version and language")
        if not isinstance(fields["name"], str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", fields["name"]
        ):
            raise HTTPException(
                422, "Model name must be a safe identifier of at most 120 characters"
            )
        if (
            not isinstance(fields["version"], str)
            or not 1 <= len(fields["version"]) <= 40
        ):
            raise HTTPException(
                422, "Model version must be a string of 1–40 characters"
            )
        if not isinstance(fields["language"], str) or len(fields["language"]) > 10:
            raise HTTPException(422, "Model language must be a short name")
        from app.schemas.models import canonical_language

        fields["language"] = canonical_language(fields["language"])
        if fields["language"] not in ("R", "Python"):
            raise HTTPException(422, "This workspace supports R and Python model bundles")
        if fields.get("owner") is not None and not isinstance(fields["owner"], str):
            raise HTTPException(422, "Model owner must be a name or null")
        mapping = decision.dataset_mapping or candidate.get("dataset_mapping")
        if mapping:
            from harness.outputs import DatasetMapping
            from pydantic import ValidationError
            from app.services.upload_service import DatasetRecord

            try:
                mapping = DatasetMapping.model_validate(mapping).model_dump()
            except ValidationError:
                raise HTTPException(422, "Invalid dataset mapping") from None
            dataset = await session.get(DatasetRecord, mapping["dataset_id"])
            if dataset is None:
                raise HTTPException(422, "Selected dataset no longer exists")
            if fields.get("target") not in (
                None,
                "",
                dataset.descriptor["target_column"],
            ):
                raise HTTPException(
                    422,
                    "Confirmed target disagrees with the bound dataset; correct the target in Gate 0",
                )
            if fields.get("positive_class") is not None and str(
                fields["positive_class"]
            ) != str(dataset.descriptor["positive_class"]):
                raise HTTPException(
                    422,
                    "Confirmed positive class disagrees with the bound dataset",
                )
        bundles.append(
            {
                "candidate_id": decision.candidate_id,
                "manifest": fields,
                "files": candidate["files"],
                "provenance": candidate["manifest"],
                "scoring": candidate["scoring"],
                "executable": candidate["executable"],
                "dataset_mapping": mapping,
            }
        )
    # Persist the immutable human decision before a separate deterministic bundle job.
    changed = await session.execute(
        update(ArchiveRecord)
        .where(ArchiveRecord.id == identity, ArchiveRecord.status == "awaiting_review")
        .values(
            status="confirmed",
            decision={"actor_id": who.id, **payload.model_dump(), "bundles": bundles},
        )
    )
    if changed.rowcount != 1:
        raise HTTPException(409, "Gate 0 already decided")
    session.add(
        AuditRecord(
            actor_id=who.id,
            resource_id=identity,
            action="gate_0_confirmed",
            payload=payload.model_dump(),
        )
    )
    await session.commit()
    await session.refresh(archive)
    return archive


async def build_bundles(identity, sessions):
    """Idempotent registration: one committed model per confirmed candidate."""
    import asyncio
    import shutil
    from app.db.tables import ModelRecord, PackageRecord, RunExecutionRecord
    from app.services.upload_service import (
        UploadRecord,
        InventoryRecord,
        DatasetRecord,
        upload_root,
        dataset_root,
        build_snapshot,
        check,
    )
    from app.services.package_service import checked_file, digest
    from app.services.monitoring_service import features_only
    from app.services.run_service import createRun, publish_run
    from app.schemas.runs import RunCreate

    async with sessions() as session:
        archive = await session.get(ArchiveRecord, identity)
        if not archive or archive.status != "confirmed":
            return
        claim = await session.execute(
            update(ArchiveRecord)
            .where(ArchiveRecord.id == identity, ArchiveRecord.status == "confirmed")
            .values(status="building")
        )
        if claim.rowcount != 1:
            return
        await session.commit()
        decisions = archive.decision
    for candidate in decisions["bundles"]:
        upload_id = str(
            __import__("uuid").uuid5(
                __import__("uuid").NAMESPACE_URL,
                identity + ":" + candidate["candidate_id"],
            )
        )
        async with sessions() as session:
            existing = await session.get(UploadRecord, upload_id)
            if existing and existing.model_id:
                inventory = await session.get(InventoryRecord, existing.model_id)
                package = await session.scalar(
                    select(PackageRecord).where(
                        PackageRecord.model_id == existing.model_id
                    )
                )
                if (
                    inventory
                    and package
                    and not inventory.provenance.get("reproduction")
                ):
                    from app.services.native_scoring import reproduce

                    existing_bundle = upload_root(upload_id) / "bundle"
                    original_reference = (
                        existing_bundle / "original/fixtures/reference_predictions.csv"
                    )
                    reproduction = (
                        await reproduce(
                            package,
                            original_reference if original_reference.exists() else None,
                        )
                        if inventory.manifest.get("executable")
                        else {"status": "NOT_ASSESSABLE"}
                    )
                    if reproduction.get("status") == "PASS" and reproduction.get(
                        "prediction_reference"
                    ):
                        target = existing_bundle / "fixtures/reference_predictions.csv"
                        shutil.copyfile(
                            settings.storage_path
                            / reproduction["prediction_reference"],
                            target,
                        )
                        hashes = {
                            str(file.relative_to(existing_bundle)): digest(file)
                            for file in existing_bundle.rglob("*")
                            if file.is_file()
                        }
                        inventory.provenance = {
                            **inventory.provenance,
                            "bundle_file_hashes": hashes,
                        }
                        inventory.bundle_sha256 = plan_hash(hashes)
                    inventory.provenance = {
                        **inventory.provenance,
                        "reproduction": reproduction,
                    }
                    await session.commit()
                continue
            mapping = candidate.get("dataset_mapping")
            dataset = (
                await session.get(DatasetRecord, mapping["dataset_id"])
                if mapping
                else None
            )
            if mapping and not dataset:
                raise ValueError("Confirmed dataset no longer exists")
            model_id = str(uuid4())
            bundle = upload_root(upload_id) / "bundle"
            bundle.mkdir(parents=True, exist_ok=True)
            known = {f["path"]: f["sha256"] for f in archive.files}
            for relative in candidate["files"]:
                source = checked_file(root(identity) / "files", relative)
                if digest(source) != known.get(relative):
                    raise ValueError("Archive file changed after Gate 0")
                target = bundle / "original" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            from app.schemas.models import canonical_language

            fields = {
                **candidate["manifest"],
                "language": canonical_language(candidate["manifest"].get("language")),
            }
            field_provenance = {}
            for field, original in candidate["provenance"].items():
                value = fields.get(field)
                changed = value != original.get("value")
                field_provenance[field] = {
                    **original,
                    "value": value,
                    "confirmed": True,
                    "confirmed_by": decisions["actor_id"],
                    "original_value": original.get("value"),
                }
                if (
                    changed
                    and original.get("source") == "unknown"
                    and value is not None
                ):
                    field_provenance[field].update(
                        source="proposed",
                        reason="Supplied by the human reviewer at Gate 0",
                    )
            features = fields.get("features") or []
            if (
                not isinstance(features, list)
                or not all(isinstance(f, str) for f in features)
                or len(set(features)) != len(features)
            ):
                raise ValueError("Confirmed features must be unique names")
            if dataset:
                nonidentity = any(
                    k != v for k, v in mapping.get("feature_map", {}).items()
                )
                if nonidentity and not candidate["scoring"].get("wrapper"):
                    raise ValueError(
                        "Nonidentity feature mappings require a reviewed scoring wrapper"
                    )
                if candidate["scoring"].get("wrapper"):
                    features = [
                        mapping.get("feature_map", {}).get(feature, feature)
                        for feature in features
                    ]
                forbidden = {
                    dataset.descriptor["target_column"],
                    "row_id",
                    *dataset.descriptor["audit_only_columns"],
                }
                if set(features) & forbidden or not set(features) <= set(
                    dataset.columns
                ):
                    raise ValueError(
                        "Features include target/audit columns or unknown inputs"
                    )
                if mapping.get("unmatched_features") or len(set(features)) != len(
                    features
                ):
                    raise ValueError(
                        "Resolve unmatched or duplicated input features before accepting this candidate"
                    )
            entry = candidate["scoring"].get("entry_point")
            if entry:
                entry = "original/" + entry
            if candidate["scoring"].get("wrapper"):
                # The agent-written wrapper, exactly as reviewed at Gate 0.
                folder = root(identity) / "wrappers" / candidate["candidate_id"]
                names = candidate["scoring"].get("wrapper_files") or ["score.py"]
                hashes = {name: digest(checked_file(folder, name)) for name in names}
                if plan_hash(hashes) != candidate["scoring"].get("wrapper_sha256"):
                    raise ValueError("Reviewed scoring wrapper changed")
                for name in names:
                    target = bundle / "scoring_wrapper" / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(checked_file(folder, name), target)
                entry = "scoring_wrapper/score.py"
            executable = bool(candidate["executable"] and entry and dataset)
            if entry:
                checked_file(bundle, entry)
            manifest = {
                **fields,
                "name": str(fields["name"])[:120],
                "model_version": str(fields["version"])[:40],
                "dataset_id": dataset.dataset_id if dataset else "",
                "feature_order": features,
                "entry_point": entry,
                "task": "binary_classification",
                "ai_native": True,
                "executable": executable,
                "pass_model_dir": bool(candidate["scoring"].get("wrapper")),
                "model_subdir": "original",
                "artifact_sha256": plan_hash({f: known[f] for f in candidate["files"]}),
                "provenance": field_provenance,
            }
            if dataset:
                if fields.get("target") not in (
                    None,
                    "",
                    dataset.descriptor["target_column"],
                ):
                    raise ValueError(
                        "Confirmed target disagrees with the bound dataset"
                    )
                if fields.get("positive_class") is not None and str(
                    fields["positive_class"]
                ) != str(dataset.descriptor["positive_class"]):
                    raise ValueError(
                        "Confirmed positive class disagrees with the bound dataset"
                    )
                manifest.update(
                    target=dataset.descriptor["target_column"],
                    positive_class=dataset.descriptor["positive_class"],
                    dataset_sha256=dataset.sha256,
                )
            saved_models = [
                "original/" + name
                for name in candidate["files"]
                if name.endswith((".rds", ".joblib", ".pkl"))
            ]
            artifact_path = saved_models[0] if saved_models else entry
            if artifact_path:
                manifest.update(
                    artifact_paths=[artifact_path],
                    artifact_sha256=digest(checked_file(bundle, artifact_path)),
                )
            (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2))
            schema = {
                "input": {
                    "type": "object",
                    "properties": {
                        "row_id": {"type": "string"},
                        **{f: {"type": "number"} for f in features},
                    },
                    "required": ["row_id", *features],
                    "additionalProperties": False,
                },
                "output": {
                    "type": "object",
                    "properties": {
                        "row_id": {"type": "string"},
                        "positive_probability": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                    },
                    "required": ["row_id", "positive_probability"],
                    "additionalProperties": False,
                },
            }
            (bundle / "schema.json").write_text(json.dumps(schema, indent=2))
            fixture = bundle / "fixtures/development_inputs.csv"
            fixture.parent.mkdir(exist_ok=True)
            if executable:
                train = dataset_root(dataset) / "partitions/train.csv"
                if digest(train) != dataset.statistics["partition_sha256"]["train"]:
                    raise ValueError("Development partition changed")
                features_only(train, fixture, features, max_rows=10000)
            # Documentation-only models have no development fixture.
            snapshot_manifest = dict(manifest)
            if not executable:
                snapshot_manifest["task"] = "documentation_only"
            fingerprint, hashes, rows = await asyncio.to_thread(
                build_snapshot, bundle, snapshot_manifest
            )
            model = ModelRecord(
                id=model_id,
                name=manifest["name"],
                version=manifest["model_version"],
                language=str(fields["language"])[:10],
                dataset_id=manifest["dataset_id"],
                purpose=str(fields.get("purpose") or "Not provided")[:1000],
                owner=fields.get("owner"),
            )
            session.add(model)
            await session.flush()
            session.add(
                UploadRecord(
                    id=upload_id,
                    status="completed",
                    actor_id=archive.actor_id,
                    archive_sha256=archive.sha256,
                    model_id=model.id,
                    checks=[check("gate_0_registration", reason="Confirmed intake bundle registered")],
                    files=candidate["files"],
                )
            )
            await session.flush()
            package = PackageRecord(
                id=str(uuid4()),
                model_id=model.id,
                name=model.name,
                version=model.version,
                dataset_id=model.dataset_id,
                language=model.language,
                sha256=fingerprint,
                input_sha256=hashes.get("data/development_inputs.csv", ""),
                file_hashes=hashes,
                n_rows=rows,
            )
            session.add(package)
            bundle_hashes = {
                str(f.relative_to(bundle)): digest(f)
                for f in bundle.rglob("*")
                if f.is_file()
            }
            inventory = InventoryRecord(
                model_id=model.id,
                name=model.name,
                model_version=model.version,
                upload_id=upload_id,
                dataset_version_id=dataset.id if dataset else None,
                manifest=manifest,
                bundle_sha256=plan_hash(bundle_hashes),
                provenance={
                    "archive_id": identity,
                    "candidate_id": candidate["candidate_id"],
                    "fields": field_provenance,
                    "gate_0": decisions,
                    "bundle_file_hashes": bundle_hashes,
                },
            )
            session.add(inventory)
            await session.flush()
            doc = await createRun(
                session,
                model.id,
                RunCreate(
                    operation="document",
                    model_version=model.version,
                    dataset_id=model.dataset_id or None,
                ),
            )
            session.add(RunExecutionRecord(run_id=doc.id, actor_id=archive.actor_id))
            await session.commit()
        # Reproduction and documentation are independent; failure to execute does not lose inventory.
        reproduction = {
            "status": "NOT_ASSESSABLE",
            "reason": "Model is not executable in the platform environment",
        }
        if executable:
            from app.services.native_scoring import reproduce

            reference = bundle / "original/fixtures/reference_predictions.csv"
            reproduction = await reproduce(
                package, reference if reference.exists() else None
            )
        async with sessions() as session:
            saved = await session.get(InventoryRecord, model.id)
            if reproduction["status"] == "PASS" and reproduction.get(
                "prediction_reference"
            ):
                shutil.copyfile(
                    settings.storage_path / reproduction["prediction_reference"],
                    bundle / "fixtures/reference_predictions.csv",
                )
                saved.provenance = {
                    **saved.provenance,
                    "bundle_file_hashes": {
                        str(f.relative_to(bundle)): digest(f)
                        for f in bundle.rglob("*")
                        if f.is_file()
                    },
                }
                saved.bundle_sha256 = plan_hash(saved.provenance["bundle_file_hashes"])
            saved.provenance = {**saved.provenance, "reproduction": reproduction}
            await session.commit()
        try:
            await asyncio.to_thread(publish_run, doc.id)
        except Exception:
            pass  # Durable run recovery will dispatch it.
    async with sessions() as session:
        saved = await session.get(ArchiveRecord, identity)
        saved.status = "completed"
        await session.commit()
