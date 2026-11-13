"""Immutable uploaded datasets and model bundles; uploaded models execute only in Docker."""

import asyncio
import csv
import hashlib
import io
import json
import math
import re
import shutil
import stat
import tempfile
import zipfile
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4
from sqlalchemy import (
    String,
    JSON,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    select,
    update,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.exc import IntegrityError
from app.config import settings
from app.db.tables import (
    Base,
    ModelRecord,
    PackageRecord,
    runningRecord,
    RunExecutionRecord,
    AuditRecord,
)
from app.schemas.uploads import DatasetDescriptor
from app.services.package_service import (
    PackageError,
    digest,
    checked_file,
    verify_snapshot,
    read_inputs,
)

MAX_SOURCE = 512 * 1024 * 1024
MAX_BUNDLE = 256 * 1024 * 1024
MAX_UNPACKED = 512 * 1024 * 1024


def now():
    return datetime.now(timezone.utc)


class UploadError(ValueError):
    pass


class DatasetRecord(Base):
    __tablename__ = "uploaded_datasets"
    __table_args__ = (UniqueConstraint("dataset_id", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(40), index=True)
    version: Mapped[str] = mapped_column(String(120))
    sha256: Mapped[str] = mapped_column(String(64))
    split_sha256: Mapped[str] = mapped_column(String(64))
    descriptor: Mapped[dict] = mapped_column(JSON)
    columns: Mapped[list] = mapped_column(JSON)
    statistics: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UploadRecord(Base):
    __tablename__ = "model_uploads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    actor_id: Mapped[str] = mapped_column(String(120))
    archive_sha256: Mapped[str] = mapped_column(String(64))
    checks: Mapped[list] = mapped_column(JSON, default=list)
    files: Mapped[list] = mapped_column(JSON, default=list)
    model_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class InventoryRecord(Base):
    __tablename__ = "uploaded_inventory"
    __table_args__ = (UniqueConstraint("name", "model_version"),)
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id"), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    model_version: Mapped[str] = mapped_column(String(40))
    upload_id: Mapped[str] = mapped_column(ForeignKey("model_uploads.id"))
    dataset_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("uploaded_datasets.id"), nullable=True
    )
    manifest: Mapped[dict] = mapped_column(JSON)
    provenance: Mapped[dict] = mapped_column(JSON)
    bundle_sha256: Mapped[str] = mapped_column(String(64))
    document_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


def check(name, passed=True, reason="Verified", blocking=True):
    return dict(
        name=name,
        status="passed" if passed else "failed",
        reason=reason,
        blocking=blocking,
    )


def dataset_root(record):
    if not re.fullmatch(r"[a-f0-9-]{36}", record.id):
        raise UploadError("Invalid dataset reference")
    return settings.storage_path / "datasets" / record.id


def upload_root(identity):
    if not re.fullmatch(r"[a-f0-9-]{36}", identity):
        raise UploadError("Invalid upload reference")
    return settings.storage_path / "uploads" / identity


async def store_stream(upload, path, limit):
    """Copy UploadFile's spooled stream in bounded chunks; never buffer a dataset."""
    path.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    size = 0
    try:
        with path.open("xb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise UploadError("Upload exceeds the permitted file size")
                h.update(chunk)
                await asyncio.to_thread(output.write, chunk)
        if size == 0:
            raise UploadError("Uploaded file is empty")
        return h.hexdigest()
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


def _rows(source, descriptor):
    """Stream CSV/XLSX; UCI XLS is small and parsed with xlrd, never with model code."""
    suffix = Path(descriptor.source_filename).suffix.lower()
    if suffix == ".csv":
        with source.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            yield reader.fieldnames
            yield from reader
    elif suffix == ".xls":
        import xlrd

        book = xlrd.open_workbook(str(source), on_demand=True)
        sheet = book.sheet_by_index(0)
        header = 1 if descriptor.dataset_id == "uci_credit" else 0
        names = [str(v).strip() for v in sheet.row_values(header)]
        try:
            yield names
            for i in range(header + 1, sheet.nrows):
                yield dict(zip(names, sheet.row_values(i), strict=True))
        finally:
            book.release_resources()
    elif suffix == ".xlsx":
        import openpyxl

        book = openpyxl.load_workbook(source.open("rb"), read_only=True, data_only=True)
        try:
            rows = book.active.iter_rows(values_only=True)
            names = [str(v).strip() for v in next(rows)]
            yield names
            for values in rows:
                yield dict(zip(names, values, strict=True))
        finally:
            book.close()
    else:
        raise UploadError("Dataset source must be CSV, XLS or XLSX")


def validate_dataset(root, descriptor):
    source = root / "source"
    split = root / "split.csv"
    if digest(source) != descriptor.sha256:
        raise UploadError("Source SHA-256 does not match descriptor")
    if digest(split) != descriptor.split_sha256:
        raise UploadError("Split SHA-256 does not match descriptor")
    if set(descriptor.partition_counts) != {"train", "validation", "test"} or any(
        v < 0 for v in descriptor.partition_counts.values()
    ):
        raise UploadError("Descriptor must declare train, validation and test counts")
    assignment = {}
    with split.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames not in (["row_id", "split"], ["row_id", "partition"]):
            raise UploadError("Split header must be row_id,split or row_id,partition")
        for row in reader:
            identity = row["row_id"]
            part = row.get("split", row.get("partition"))
            if (
                not identity
                or identity in assignment
                or part not in descriptor.partition_counts
                or None in row
            ):
                raise UploadError(
                    "Every split row ID must be unique and have exactly one valid partition"
                )
            assignment[identity] = part
            if len(assignment) > 2_000_000:
                raise UploadError("Too many split rows")
    rows = _rows(source, descriptor)
    columns = next(rows)
    if not columns or len(columns) != len(set(columns)) or any(not c for c in columns):
        raise UploadError("Dataset columns must be unique and nonempty")
    required = {descriptor.target_column, *descriptor.audit_only_columns}
    if descriptor.time_column:
        required.add(descriptor.time_column)
    if descriptor.dataset_id == "uci_credit":
        required.add("ID")
    if not required.issubset(columns):
        raise UploadError(
            "Dataset is missing declared target, audit, ID or time columns"
        )
    if "row_id" in columns:
        raise UploadError("Original source must not contain generated row_id")
    out = root / "partitions"
    out.mkdir()
    counts = dict.fromkeys(descriptor.partition_counts, 0)
    positive = dict(counts)
    with ExitStack() as stack:
        writers = {}
        for part in counts:
            f = stack.enter_context(
                (out / f"{part}.csv").open("w", newline="", encoding="utf-8")
            )
            writers[part] = csv.DictWriter(f, fieldnames=["row_id", *columns])
            writers[part].writeheader()
        for index, row in enumerate(rows):
            if None in row or any(v is None for v in row.values()):
                raise UploadError("Malformed dataset row")
            try:
                if descriptor.dataset_id == "uci_credit":
                    raw = float(row["ID"])
                    if not raw.is_integer() or raw < 1:
                        raise ValueError()
                    identity = f"uci-{int(raw):06d}"
                else:
                    identity = f"ulb-{index:06d}"
                part = assignment.pop(identity)
                value = float(row[descriptor.target_column])
                if value not in (0, 1) or not math.isfinite(value):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise UploadError(
                    "Source IDs or binary labels do not match the declared split"
                ) from None
            writers[part].writerow({"row_id": identity, **row})
            counts[part] += 1
            positive[part] += int(value == float(descriptor.positive_class))
    if (
        assignment
        or counts != descriptor.partition_counts
        or sum(counts.values()) != descriptor.row_count
    ):
        raise UploadError(
            "Source/split membership or partition counts do not match descriptor"
        )
    hashes = {part: digest(out / f"{part}.csv") for part in counts}
    for f in out.iterdir():
        f.chmod(0o444)
    return columns, dict(
        row_count=sum(counts.values()),
        positive_rate=sum(positive.values()) / sum(counts.values()),
        partition_counts=counts,
        positive_rates={
            p: positive[p] / counts[p] if counts[p] else None for p in counts
        },
        partition_sha256=hashes,
    )


async def ingest_dataset(session, source, descriptor_file, split, actor_id):
    identity = str(uuid4())
    root = settings.storage_path / "datasets" / identity
    root.mkdir(parents=True)
    try:
        await store_stream(descriptor_file, root / "dataset.json", 1024 * 1024)
        descriptor = DatasetDescriptor.model_validate_json(
            (root / "dataset.json").read_text()
        )
        await store_stream(source, root / "source", MAX_SOURCE)
        if split is None:
            if descriptor.dataset_id == "uci_credit":
                canonical = settings.legacy_models_root / "evidence" / "uci_split_ids.csv"
                if not canonical.is_file() or canonical.is_symlink():
                    raise UploadError("No verified canonical UCI split is available; upload the split file")
                if digest(canonical) != descriptor.split_sha256:
                    raise UploadError("Canonical split SHA-256 does not match descriptor")
                await asyncio.to_thread(shutil.copyfile, canonical, root / "split.csv")
            elif descriptor.dataset_id == "ulb_fraud":
                from runpy import run_path

                restore_script = settings.legacy_models_root / "shared" / "restore_ulb_split.py"
                if not restore_script.is_file():
                    raise UploadError("Canonical ULB split generator is unavailable; upload the split file")
                restore = run_path(str(restore_script))["restore"]
                try:
                    await asyncio.to_thread(restore, root / "split.csv")
                except ValueError as exc:
                    raise UploadError(str(exc)) from exc
                if digest(root / "split.csv") != descriptor.split_sha256:
                    raise UploadError("Canonical split SHA-256 does not match descriptor")
            else:
                raise UploadError("No verified canonical split is available; upload the split file")
        else:
            await store_stream(split, root / "split.csv", 32 * 1024 * 1024)
        columns, stats = await asyncio.to_thread(validate_dataset, root, descriptor)
        existing = await session.scalar(
            select(DatasetRecord).where(
                DatasetRecord.dataset_id == descriptor.dataset_id,
                DatasetRecord.version == descriptor.version,
            )
        )
        if existing:
            if (
                existing.sha256 != descriptor.sha256
                or existing.split_sha256 != descriptor.split_sha256
                or existing.descriptor != descriptor.model_dump()
            ):
                raise UploadError(
                    "Dataset ID/version already exists with different content; use a new version"
                )
            shutil.rmtree(root)
            from app.services.dataset_deletion import restore

            await restore(session, existing, actor_id, "identical dataset version uploaded again")
            await session.commit()
            return existing
        record = DatasetRecord(
            id=identity,
            dataset_id=descriptor.dataset_id,
            version=descriptor.version,
            sha256=descriptor.sha256,
            split_sha256=descriptor.split_sha256,
            descriptor=descriptor.model_dump(),
            columns=columns,
            statistics=stats,
        )
        session.add(record)
        session.add(
            AuditRecord(
                actor_id=actor_id,
                resource_id=identity,
                action="dataset_uploaded",
                payload={
                    "sha256": record.sha256,
                    "split_sha256": record.split_sha256,
                    "version": record.version,
                },
            )
        )
        await session.commit()
        return record
    except BaseException:
        await session.rollback()
        shutil.rmtree(root, ignore_errors=True)
        raise


def safe_name(name):
    path = PurePosixPath(name)
    if (
        not name
        or any(ord(c) < 32 or ord(c) > 126 or c == chr(34) for c in name)
        or name.startswith("/")
        or "\\" in name
        or ":" in name
        or any(p in ("", "..", ".") or p.startswith(".") for p in path.parts)
        or path.as_posix() != name
    ):
        raise UploadError("Unsafe archive path")
    return name


def unpack_bundle(archive, root):
    root.mkdir(parents=True, exist_ok=True)
    total = 0
    names = set()
    with zipfile.ZipFile(archive) as z:
        if not z.infolist() or len(z.infolist()) > 512:
            raise UploadError("Bundle must contain 1–512 entries")
        for info in z.infolist():
            name = safe_name(
                info.filename.rstrip("/") if info.is_dir() else info.filename
            )
            if name in names:
                raise UploadError("Duplicate ZIP entries are forbidden")
            names.add(name)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or (
                stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))
            ):
                raise UploadError("ZIP links and special files are forbidden")
            total += info.file_size
            if (
                total > MAX_UNPACKED
                or info.file_size > MAX_BUNDLE
                or (
                    info.file_size > 1024 * 1024
                    and info.file_size > max(info.compress_size, 1) * 1000
                )
            ):
                raise UploadError("ZIP expansion exceeds limits")
            if info.flag_bits & 1:
                raise UploadError("Encrypted ZIP entries are forbidden")
            if info.is_dir():
                continue
            allowed = (
                len(PurePosixPath(name).parts) == 1
                or name.startswith("shared/")
                or name.startswith("documentation/")
                or name
                in (
                    "fixtures/development_inputs.csv",
                    "fixtures/reference_predictions.csv",
                )
            )
            if not allowed or Path(name).suffix not in {
                ".py",
                ".R",
                ".rds",
                ".joblib",
                ".json",
                ".md",
                ".txt",
                ".csv",
            }:
                raise UploadError("Bundle contains unsupported files or protected data")
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("xb") as dest:
                copied = 0
                while chunk := src.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > info.file_size or copied > MAX_BUNDLE:
                        raise UploadError("ZIP entry exceeds declared size")
                    dest.write(chunk)
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def validate_bundle(root):
    import jsonschema

    try:
        manifest = json.loads(checked_file(root, "manifest.json").read_text())
        schema = json.loads(checked_file(root, "schema.json").read_text())
        for field, limit in [
            ("name", 120),
            ("model_version", 40),
            ("language", 10),
            ("entry_point", 100),
            ("task", 50),
        ]:
            if (
                not isinstance(manifest.get(field), str)
                or not 0 < len(manifest[field]) <= limit
            ):
                raise UploadError("Missing or invalid manifest field: " + field)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", manifest["name"]):
            raise UploadError("Model name must be a safe identifier")
        task = manifest["task"]
        binary = task == "binary_classification"
        if not binary and not (
            task == "survival_analysis"
            and manifest.get("synthetic_demo") is True
            and manifest.get("dataset_id") is None
        ):
            raise UploadError(
                "Unsupported task: only binary models and explicitly synthetic survival demonstrations are supported"
            )
        if manifest["language"] not in ("R", "Python"):
            raise UploadError("Unsupported model language")
        entry = "score.R" if manifest["language"] == "R" else "score.py"
        if manifest["entry_point"] != entry:
            raise UploadError("Entry point must match the language scoring interface")
        checked_file(root, entry)
        for part in ("input", "output"):
            jsonschema.Draft202012Validator.check_schema(schema[part])
        features = manifest.get("feature_order")
        if (
            not isinstance(features, list)
            or not features
            or len(set(features)) != len(features)
            or not all(isinstance(x, str) for x in features)
        ):
            raise UploadError("Invalid feature_order")
        if set(schema["input"]["required"]) != {"row_id", *features}:
            raise UploadError("Input schema does not match feature_order")
        if binary and schema["output"]["required"] != [
            "row_id",
            "positive_probability",
        ]:
            raise UploadError(
                "Binary output schema must require row_id and positive_probability"
            )
        artifacts = manifest.get("artifact_paths")
        artifacts = [artifacts] if isinstance(artifacts, str) else artifacts
        if not isinstance(artifacts, list) or len(artifacts) != 1:
            raise UploadError("Exactly one saved model artifact is required")
        if digest(checked_file(root, safe_name(artifacts[0]))) != manifest.get(
            "artifact_sha256"
        ):
            raise UploadError("Model artifact checksum mismatch")
        provenance = json.loads(
            checked_file(root, "bundle_provenance.json").read_text()
        )
        supplied = provenance.get(
            "bundle_file_sha256", provenance.get("file_sha256", {})
        )
        if not supplied:
            supplied = manifest.get("bundle_file_sha256", {})
        # The authoritative builder records all actual entry hashes in file_sha256.
        if not supplied:
            raise UploadError("Bundle provenance must include file checksums")
        actual = {
            str(p.relative_to(root)): digest(p)
            for p in sorted(root.rglob("*"))
            if p.is_file() and p.name != "bundle_provenance.json"
        }
        if supplied != actual:
            raise UploadError(
                "Bundle file checksums or file set do not match provenance"
            )
        for field, relative in [
            ("scoring_code_sha256", entry),
            ("model_code_sha256", "train.py"),
            ("shared_pipeline_sha256", "shared/pipeline.py"),
        ]:
            if (
                manifest.get(field)
                and digest(checked_file(root, relative)) != manifest[field]
            ):
                raise UploadError("Declared code checksum mismatch: " + field)
        if provenance.get("bundled_scoring_code_sha256") != digest(
            checked_file(root, entry)
        ):
            raise UploadError("Bundled scorer checksum mismatch")
        if binary:
            read_inputs(checked_file(root, "fixtures/development_inputs.csv"), features)
            checked_file(root, "fixtures/reference_predictions.csv")
        return manifest, provenance
    except UploadError:
        raise
    except PackageError as exc:
        prefix = "Missing or unsafe package file: "
        message = str(exc)
        relative = message[len(prefix):] if message.startswith(prefix) else ""
        if (
            relative
            and len(relative) <= 500
            and not relative.startswith(("/", "~"))
            and "\\" not in relative
            and ".." not in PurePosixPath(relative).parts
            and not re.search(r"[\x00-\x1f\x7f]", relative)
        ):
            raise UploadError("Missing or unsafe bundle file: " + relative) from None
        raise UploadError("Invalid model package reference") from None
    except Exception as exc:
        raise UploadError(
            "Invalid model manifest/schema/provenance: " + type(exc).__name__
        ) from None


def build_snapshot(bundle, manifest):
    base = settings.storage_path / "packages"
    base.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="upload-", dir=base))
    stage.chmod(0o755)
    try:
        # Public aggregate evidence and old notes are inputs; reference answers stay outside agent mounts.
        for src in bundle.rglob("*"):
            if (
                not src.is_file()
                or src.name == "reference_predictions.csv"
            ):
                continue
            dest = stage / "package" / src.relative_to(bundle)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        fixture = bundle / "fixtures/development_inputs.csv"
        rows = 0
        if manifest["task"] == "binary_classification":
            dest = stage / "data/development_inputs.csv"
            dest.parent.mkdir(parents=True)
            shutil.copyfile(fixture, dest)
            rows = len(read_inputs(dest, manifest["feature_order"]))
        hashes = {
            str(p.relative_to(stage)): digest(p)
            for p in sorted(stage.rglob("*"))
            if p.is_file()
        }
        fingerprint = hashlib.sha256(
            json.dumps(hashes, sort_keys=True).encode()
        ).hexdigest()
        final = base / fingerprint
        if final.exists():
            verify_snapshot(fingerprint, hashes)
        else:
            stage.rename(final)
        return fingerprint, hashes, rows
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def verify_uploaded_bundle(inventory):
    root = upload_root(inventory.upload_id) / "bundle"
    expected = inventory.provenance["bundle_file_hashes"]
    actual = {
        str(p.relative_to(root)): digest(checked_file(root, str(p.relative_to(root))))
        for p in root.rglob("*")
        if p.is_file()
    }
    if actual != expected:
        raise UploadError("Uploaded bundle integrity check failed")
    return root


async def _record_checks(sessions, identity, checks, **values):
    async with sessions() as session:
        await session.execute(
            update(UploadRecord)
            .where(UploadRecord.id == identity)
            .values(checks=list(checks), **values)
        )
        await session.commit()


async def process_upload(identity, sessions):
    async with sessions() as session:
        changed = await session.execute(
            update(UploadRecord)
            .where(UploadRecord.id == identity, UploadRecord.status == "queued")
            .values(status="running", started_at=now())
        )
        await session.commit()
        if changed.rowcount != 1:
            return
        upload = await session.get(UploadRecord, identity)
    root = upload_root(identity)
    checks = []
    phase = "safe_unpacking"
    try:
        if digest(root / "bundle.zip") != upload.archive_sha256:
            raise UploadError("Stored archive hash changed")
        files = await asyncio.to_thread(
            unpack_bundle, root / "bundle.zip", root / "bundle"
        )
        checks.append(check(phase))
        await _record_checks(sessions, identity, checks, files=files)
        phase = "manifest_schema_and_hashes"
        manifest, provenance = await asyncio.to_thread(validate_bundle, root / "bundle")
        checks.append(check(phase))
        await _record_checks(sessions, identity, checks)
        phase = "dataset_binding"
        dataset = None
        async with sessions() as session:
            if manifest["task"] == "binary_classification":
                source_hash = manifest.get(
                    "dataset_sha256", manifest.get("dataset_sha256_expected")
                )
                from app.services.dataset_deletion import visible_datasets

                dataset = await session.scalar(
                    visible_datasets()
                    .where(
                        DatasetRecord.dataset_id == manifest.get("dataset_id"),
                        DatasetRecord.sha256 == source_hash,
                    )
                    .order_by(DatasetRecord.created_at.desc())
                )
                if not dataset:
                    raise UploadError(
                        "Upload the exact manifest dataset version before uploading this model"
                    )
                if manifest.get("target") != dataset.descriptor["target_column"] or str(
                    manifest.get("positive_class")
                ) != str(dataset.descriptor["positive_class"]):
                    raise UploadError(
                        "Model target or positive class disagrees with the dataset descriptor"
                    )
                if not set(manifest["feature_order"]).issubset(dataset.columns):
                    raise UploadError(
                        "Model features do not exist in the uploaded dataset"
                    )
                if set(manifest["feature_order"]) & {
                    dataset.descriptor["target_column"],
                    *dataset.descriptor["audit_only_columns"],
                    "row_id",
                }:
                    raise UploadError(
                        "Target/audit-only columns cannot be model features"
                    )
        if dataset:
            await asyncio.to_thread(
                verify_development_fixture,
                root / "bundle/fixtures/development_inputs.csv",
                manifest["feature_order"],
                dataset,
            )
        checks.append(
            check(
                phase,
                reason="Verified dataset hash, features and development-only fixture membership"
                if dataset
                else "Not applicable: synthetic survival model",
            )
        )
        await _record_checks(sessions, identity, checks)
        phase = "immutable_model_version"
        async with sessions() as session:
            previous = await session.scalar(
                select(InventoryRecord).where(
                    InventoryRecord.name == manifest["name"],
                    InventoryRecord.model_version == manifest["model_version"],
                )
            )
            if previous:
                if previous.bundle_sha256 != upload.archive_sha256:
                    raise UploadError(
                        "Model name/version already exists with different bundle content"
                    )
                existing_model = await session.get(ModelRecord, previous.model_id)
                restored = existing_model is not None and existing_model.lifecycle_status == "deleted"
                if restored:
                    from app.services.model_deletion import restore

                    await restore(session, existing_model, upload.actor_id, "identical bundle uploaded again")
                    await session.commit()
                checks.append(
                    check(
                        phase,
                        reason="Identical version was deleted and is now restored"
                        if restored
                        else "Identical version already registered",
                    )
                )
                checks.append(
                    check(
                        "reproduction",
                        reason="Existing upload retained; reproduction not rerun",
                        blocking=False,
                    )
                )
                await _record_checks(
                    sessions,
                    identity,
                    checks,
                    status="completed",
                    model_id=previous.model_id,
                    finished_at=now(),
                )
                return
        fingerprint, hashes, rows = await asyncio.to_thread(
            build_snapshot, root / "bundle", manifest
        )
        model = ModelRecord(
            id=str(uuid4()),
            name=manifest["name"],
            version=manifest["model_version"],
            language=manifest["language"],
            dataset_id=manifest.get("dataset_id") or "",
            purpose=str(manifest.get("purpose") or "Not provided in upload")[:1000],
            owner=manifest.get("model_owner"),
            lifecycle_status="development",
        )
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
        all_hashes = {
            str(p.relative_to(root / "bundle")): digest(p)
            for p in (root / "bundle").rglob("*")
            if p.is_file()
        }
        inv = InventoryRecord(
            model_id=model.id,
            name=model.name,
            model_version=model.version,
            upload_id=identity,
            dataset_version_id=dataset.id if dataset else None,
            manifest=manifest,
            provenance={
                **provenance,
                "bundle_file_hashes": all_hashes,
                "uploaded_by": upload.actor_id,
            },
            bundle_sha256=upload.archive_sha256,
        )
        async with sessions() as session:
            session.add(model)
            await session.flush()
            session.add(package)
            session.add(inv)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise UploadError(
                    "Model version was registered concurrently; inspect existing inventory"
                ) from None
        checks.append(check(phase))
        await _record_checks(sessions, identity, checks)
        phase = "reproduction"
        reproduction = {
            "status": "NOT_APPLICABLE",
            "reason": "Synthetic survival documentation only",
        }
        if manifest["task"] == "binary_classification":
            from app.services.legacy_scoring import score_snapshot, validate_predictions

            try:
                result = await score_snapshot(package, str(uuid4()))
                raw = (
                    settings.storage_path / result["prediction_reference"]
                ).read_bytes()
                ids = read_inputs(
                    root / "bundle/fixtures/development_inputs.csv",
                    manifest["feature_order"],
                )
                reference = validate_predictions(
                    (root / "bundle/fixtures/reference_predictions.csv").read_bytes(),
                    ids,
                )

                def numbers(data):
                    return [
                        float(r["positive_probability"])
                        for r in csv.DictReader(io.StringIO(data.decode()))
                    ]

                worst = max(
                    abs(a - b)
                    for a, b in zip(numbers(raw), numbers(reference), strict=True)
                )
                reproduction = {
                    **result,
                    "status": "PASS" if worst <= 1e-7 else "FAIL",
                    "max_absolute_difference": worst,
                    "absolute_tolerance": 1e-7,
                    "scope": "development_reproduction_not_official_parity",
                }
                checks.append(
                    check(
                        phase,
                        worst <= 1e-7,
                        f"Maximum development prediction difference {worst:g}; tolerance 1e-7; not official parity",
                        False,
                    )
                )
            except Exception:
                reproduction = {
                    "status": "NOT_ASSESSABLE",
                    "reason": "Scoring unavailable or incompatible; inspect configured Docker image and package",
                }
                checks.append(check(phase, False, reproduction["reason"], False))
        else:
            checks.append(
                dict(
                    name=phase,
                    status="not_applicable",
                    reason="Synthetic survival model; no binary scoring performed",
                    blocking=False,
                )
            )

        async with sessions() as session:
            saved = await session.get(InventoryRecord, model.id)
            saved.provenance = {**saved.provenance, "reproduction": reproduction}
            session.add(
                AuditRecord(
                    actor_id=upload.actor_id,
                    resource_id=model.id,
                    action="model_uploaded",
                    payload={
                        "upload_id": identity,
                        "bundle_sha256": upload.archive_sha256,
                        "reproduction": reproduction,
                    },
                )
            )
            run = runningRecord(
                id=str(uuid4()),
                model_id=model.id,
                model_version=model.version,
                dataset_id=model.dataset_id,
                operation="document",
                status="queued",
            )
            session.add(run)
            await session.flush()
            session.add(RunExecutionRecord(run_id=run.id, actor_id=upload.actor_id))
            saved.document_run_id = run.id
            if (
                not settings.openrouter_api_key
                or not settings.openrouter_api_key.get_secret_value()
            ):
                run.status = "failed"
                run.stage = "configuration_required"
                run.finished_at = now()
                run.error = (
                    "Configure OPENROUTER_API_KEY, then regenerate the model document."
                )
            await session.commit()
        if run.status == "queued":
            from app.services.run_service import publish_run

            try:
                await asyncio.to_thread(publish_run, run.id)
            except Exception:
                async with sessions() as session:
                    await session.execute(
                        update(runningRecord)
                        .where(
                            runningRecord.id == run.id, runningRecord.status == "queued"
                        )
                        .values(
                            status="failed",
                            stage="dispatch_failed",
                            error="Document dispatch failed; retry generation.",
                            finished_at=now(),
                        )
                    )
                    await session.commit()
        await _record_checks(
            sessions,
            identity,
            checks,
            status="completed",
            model_id=model.id,
            error=None,
            finished_at=now(),
        )
    except Exception as exc:
        reason = (
            str(exc)
            if isinstance(exc, UploadError)
            else "Upload processing failed; inspect package structure or worker configuration"
        )
        checks.append(check(phase, False, reason))
        await _record_checks(
            sessions, identity, checks, status="failed", error=reason, finished_at=now()
        )


def verify_development_fixture(fixture, features, dataset):
    """Uploaded fixtures cannot smuggle final-test rows into agent execution."""
    with fixture.open(newline="", encoding="utf-8") as f:
        needed = {r["row_id"]: r for r in csv.DictReader(f)}
    for partition in ("train", "validation"):
        path = dataset_root(dataset) / "partitions" / f"{partition}.csv"
        if digest(path) != dataset.statistics["partition_sha256"][partition]:
            raise UploadError("Stored dataset partition changed")
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                expected = needed.get(row["row_id"])
                if expected is not None:
                    try:
                        if any(
                            not math.isclose(
                                float(expected[k]),
                                float(row[k]),
                                rel_tol=1e-12,
                                abs_tol=1e-12,
                            )
                            for k in features
                        ):
                            raise ValueError()
                    except (KeyError, ValueError, TypeError):
                        raise UploadError(
                            "Development fixture features differ from source data"
                        ) from None
                    del needed[row["row_id"]]
    if needed:
        raise UploadError(
            "Development fixture contains unknown or protected-test row IDs"
        )
