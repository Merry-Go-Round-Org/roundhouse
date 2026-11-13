"""Trusted local package registry. Never mounts the repository or test evidence."""

import csv
import hashlib
import json
import math
import re
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.config import settings
from app.db.tables import ModelRecord, PackageRecord

PACKAGE_NAMES = frozenset(
    f"{domain}_{algorithm}_python"
    for domain in ("credit", "fraud")
    for algorithm in ("logistic", "xgboost", "lightgbm")
) | {"credit_logistic_r", "fraud_logistic_r"}


class PackageError(ValueError):
    pass


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def checked_file(root, relative):
    root = Path(root)
    path = root / relative
    if (
        path.is_symlink()
        or root.is_symlink()
        or any(p.is_symlink() for p in path.parents if p.is_relative_to(root))
    ):
        raise PackageError("Symbolic links are not allowed in registered packages.")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise PackageError(f"Missing or unsafe package file: {relative}")
    return path


def read_inputs(path, features, max_rows=10000):
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        expected = ["row_id", *features]
        if (
            not reader.fieldnames
            or len(reader.fieldnames) != len(set(reader.fieldnames))
            or set(reader.fieldnames) != set(expected)
        ):
            raise PackageError(
                "Development input columns must be exactly row_id and approved features."
            )
        ids = []
        for row in reader:
            if None in row or not row["row_id"] or not row["row_id"].strip():
                raise PackageError("Invalid development row.")
            try:
                if not all(math.isfinite(float(row[f])) for f in features):
                    raise ValueError()
            except (ValueError, TypeError):
                raise PackageError(
                    "Development features must be finite numeric values."
                ) from None
            ids.append(row["row_id"])
            if len(ids) > max_rows:
                raise PackageError(f"Input exceeds {max_rows} rows.")
        if not ids or len(ids) != len(set(ids)):
            raise PackageError("Development row IDs must be unique and nonempty.")
        return ids


def snapshot_package(name):
    if name not in PACKAGE_NAMES:
        raise PackageError(
            "Unknown package; only registered binary model packages are supported."
        )
    root = settings.legacy_models_root.resolve()
    package = root / name
    manifest_file = checked_file(root, f"{name}/manifest.json")
    manifest = json.loads(manifest_file.read_text())
    language = manifest.get("language")
    entry, artifact = (
        ("score.py", "model.joblib")
        if language == "Python"
        else ("score.R", "model.rds")
    )
    if (
        language not in ("R", "Python")
        or manifest.get("entry_point") != entry
        or manifest.get("task") != "binary_classification"
    ):
        raise PackageError("Unsupported scoring contract.")
    if manifest.get("name") != name or manifest.get("dataset_id") not in (
        "uci_credit",
        "ulb_fraud",
    ):
        raise PackageError("Manifest identity mismatch.")
    artifacts = manifest.get("artifact_paths")
    if artifacts not in (artifact, [artifact]):
        raise PackageError("Unsupported artifact layout.")
    features = manifest.get("feature_order")
    if (
        not isinstance(features, list)
        or not features
        or not all(isinstance(f, str) for f in features)
        or len(features) != len(set(features))
    ):
        raise PackageError("Invalid feature list.")
    if set(features) & {"Class", "default payment next month", "row_id"}:
        raise PackageError("Labels cannot be scoring features.")
    schema = json.loads(checked_file(root, f"{name}/schema.json").read_text())
    if set(schema["input"]["required"]) != {"row_id", *features} or schema["output"][
        "required"
    ] != ["row_id", "positive_probability"]:
        raise PackageError("Schema does not match the binary scoring interface.")
    files = {
        f"package/{f}": f"{name}/{f}"
        for f in ("manifest.json", "schema.json", entry, artifact)
    }
    files["data/development_inputs.csv"] = f"{name}/fixtures/development_inputs.csv"
    if language == "Python":
        for f in ("score_python.py", "pipeline.py"):
            files[f"shared/{f}"] = f"shared/{f}"
        for f in ("requirements.txt", "requirements-additional.txt"):
            if (package / f).is_file():
                files[f"package/{f}"] = f"{name}/{f}"
    base = settings.storage_path / "packages"
    base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="staging-", dir=base))
    staging.chmod(0o755)  # Readable by the unprivileged container user.
    try:
        for target, source in files.items():
            dest = staging / target
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(checked_file(root, source), dest)
        if digest(staging / "package" / artifact) != manifest.get("artifact_sha256"):
            raise PackageError(
                "Artifact checksum mismatch or unresolved Git LFS pointer."
            )
        # Code hashes are additional provenance when the package supplies them.
        for key, relative in (
            ("shared_pipeline_sha256", "shared/pipeline.py"),
            ("scoring_code_sha256", "shared/score_python.py"),
        ):
            if (
                language == "Python"
                and manifest.get(key)
                and digest(staging / relative) != manifest[key]
            ):
                raise PackageError(f"Code checksum mismatch: {key}")
        ids = read_inputs(staging / "data/development_inputs.csv", features)
        hashes = {
            str(p.relative_to(staging)): digest(p)
            for p in sorted(staging.rglob("*"))
            if p.is_file()
        }
        fingerprint = hashlib.sha256(
            json.dumps(hashes, sort_keys=True).encode()
        ).hexdigest()
        final = base / fingerprint
        if final.exists():
            verify_snapshot(fingerprint, hashes)
        else:
            try:
                staging.rename(final)
            except OSError:
                if not final.exists():
                    raise
                verify_snapshot(fingerprint, hashes)
        return manifest, fingerprint, hashes, len(ids)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def verify_snapshot(fingerprint, hashes):
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise PackageError("Invalid snapshot reference.")
    root = settings.storage_path / "packages" / fingerprint
    actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
    if actual != set(hashes):
        raise PackageError("Snapshot file set changed.")
    for relative, expected in hashes.items():
        if digest(checked_file(root, relative)) != expected:
            raise PackageError("Registered snapshot changed.")
    return root


async def register_package(session, model_id, name):
    import asyncio

    model = await session.get(ModelRecord, model_id)
    if model is None or model.lifecycle_status == "deleted":
        raise LookupError("Model not found")
    manifest, fingerprint, hashes, rows = await asyncio.to_thread(
        snapshot_package, name
    )
    if (model.version, model.language, model.dataset_id) != (
        manifest["model_version"],
        manifest["language"],
        manifest["dataset_id"],
    ):
        raise PackageError(
            "Model version, language and dataset must match the package manifest."
        )
    existing = await session.scalar(
        select(PackageRecord).where(PackageRecord.model_id == model_id)
    )
    if existing:
        if existing.sha256 == fingerprint:
            return existing
        raise PackageError(
            "A different package is already bound to this model record; register a new model version."
        )
    record = PackageRecord(
        id=str(uuid4()),
        model_id=model_id,
        name=name,
        version=model.version,
        dataset_id=model.dataset_id,
        language=model.language,
        sha256=fingerprint,
        input_sha256=hashes["data/development_inputs.csv"],
        file_hashes=hashes,
        n_rows=rows,
    )
    session.add(record)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(PackageRecord).where(PackageRecord.model_id == model_id)
        )
        if existing and existing.sha256 == fingerprint:
            return existing
        raise PackageError(
            "A different package was registered concurrently for this model."
        ) from None
    return record
