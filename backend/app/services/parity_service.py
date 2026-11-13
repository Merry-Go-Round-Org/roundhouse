"""Official migration parity: one platform check shared by agents and people.

The rebuild agent calls it through the ``run_parity`` tool and a person calls it
through ``POST /models/{id}/versions/{version}/parity``. Both run this function,
with the same data, tolerance and frozen scoring image; only the recorded
initiator differs. Given the same candidate it always gives the same result.

Method (``parity-v1``): score the candidate's development inputs in a fresh
container, then compare each row's positive-class probability with the parent
model's committed ``fixtures/reference_predictions.csv``. The candidate passes
when every absolute difference is at most ``TOLERANCE_ABSOLUTE``.
"""

import asyncio
import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column
from app.config import settings
from app.db.tables import AuditRecord, Base, PackageRecord
from app.services.package_service import read_inputs, verify_snapshot

METHOD = "parity-v1"
TOLERANCE_ABSOLUTE = 1e-8  # frozen by the legacy workstream
TOLERANCE_RELATIVE = 0.0
WORST_ROWS = 10


class ParityError(ValueError):
    """The request cannot be compared; no parity run is recorded."""


class ParityRunRecord(Base):
    __tablename__ = "parity_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_model_id: Mapped[str] = mapped_column(
        ForeignKey("models.id"), index=True
    )
    candidate_version: Mapped[str] = mapped_column(String(40))
    candidate_bundle_sha256: Mapped[str] = mapped_column(String(64))
    parent_model_id: Mapped[str] = mapped_column(ForeignKey("models.id"))
    parent_version: Mapped[str] = mapped_column(String(40))
    rebuild_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    initiator: Mapped[str] = mapped_column(String(10))  # "agent" or "human"
    actor_id: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20))  # "completed" or "failed"
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[dict] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


def probabilities(content):
    return [
        float(row["positive_probability"])
        for row in csv.DictReader(io.StringIO(content.decode("utf-8")))
    ]


def compare(row_ids, candidate, reference):
    """Per-row comparison. Pure, so the same predictions give the same result."""
    differences = [
        abs(a - b)
        for a, b in zip(probabilities(candidate), probabilities(reference), strict=True)
    ]
    above = [
        (row_ids[i], d) for i, d in enumerate(differences) if d > TOLERANCE_ABSOLUTE
    ]
    above.sort(key=lambda item: (-item[1], item[0]))
    return {
        "passed": bool(differences) and not above,
        "n_rows": len(differences),
        "n_above_tolerance": len(above),
        "max_absolute_difference": max(differences) if differences else None,
        "worst_rows": [
            {"row_id": row_id, "absolute_difference": d}
            for row_id, d in above[:WORST_ROWS]
        ],
    }


async def candidate_for_artifact(session, artifact_id):
    """The candidate model that a rebuild run exported as this artifact, if any."""
    record = await session.scalar(
        select(AuditRecord)
        .where(AuditRecord.action == "candidate_exported")
        .where(AuditRecord.payload["artifact_id"].as_string() == artifact_id)
        .order_by(AuditRecord.created_at.desc())
    )
    return record.payload["candidate_model_id"] if record else None


async def latest_candidate(session, run_id):
    """The most recent candidate exported by a rebuild run, if any."""
    record = await session.scalar(
        select(AuditRecord)
        .where(
            AuditRecord.resource_id == run_id,
            AuditRecord.action == "candidate_exported",
        )
        .order_by(AuditRecord.created_at.desc())
    )
    return record.payload["candidate_model_id"] if record else None


async def passed_parity(session, candidate_model_id):
    """The latest passing parity run for this exact candidate bundle, if any."""
    from app.services.upload_service import InventoryRecord

    if not candidate_model_id:
        return None
    inventory = await session.get(InventoryRecord, candidate_model_id)
    if inventory is None:
        return None
    return await session.scalar(
        select(ParityRunRecord)
        .where(
            ParityRunRecord.candidate_model_id == candidate_model_id,
            ParityRunRecord.candidate_bundle_sha256 == inventory.bundle_sha256,
            ParityRunRecord.status == "completed",
            ParityRunRecord.passed.is_(True),
        )
        .order_by(ParityRunRecord.created_at.desc())
    )


async def _lineage(sessions, candidate_model_id):
    from app.services.upload_service import InventoryRecord

    async with sessions() as session:
        candidate = await session.get(InventoryRecord, candidate_model_id)
        if candidate is None:
            raise ParityError("Candidate is not an uploaded model")
        source = candidate.manifest.get("derived_from")
        if not source:
            raise ParityError("Model is not a rebuild candidate; nothing to compare")
        if candidate.manifest.get("task") != "binary_classification":
            raise ParityError("Parity applies to binary classification models only")
        parent = await session.get(InventoryRecord, source["model_id"])
        candidate_package = await session.scalar(
            select(PackageRecord).where(PackageRecord.model_id == candidate_model_id)
        )
        parent_package = await session.scalar(
            select(PackageRecord).where(PackageRecord.model_id == source["model_id"])
        )
    if parent is None or parent_package is None or candidate_package is None:
        raise ParityError("Parent or candidate package is unavailable")
    if parent_package.sha256 != source.get("package_sha256"):
        raise ParityError("Parent package changed since the candidate was exported")
    if candidate_package.input_sha256 != parent_package.input_sha256:
        raise ParityError("Candidate and parent development inputs differ")
    return candidate, parent, candidate_package, parent_package, source


async def run_parity(
    sessions, candidate_model_id, *, initiator, actor_id, rebuild_run_id=None
):
    """Run official parity once and record it. Raises ParityError if not comparable."""
    from app.services.legacy_scoring import score_snapshot, validate_predictions
    from app.services.upload_service import verify_uploaded_bundle

    if initiator not in ("agent", "human"):
        raise ValueError("initiator must be 'agent' or 'human'")
    candidate, parent, candidate_package, parent_package, source = await _lineage(
        sessions, candidate_model_id
    )
    evidence = {
        "method": METHOD,
        "scope": "development_fixture_parity",
        "tolerance_absolute": TOLERANCE_ABSOLUTE,
        "tolerance_relative": TOLERANCE_RELATIVE,
        "candidate_model_id": candidate.model_id,
        "candidate_version": candidate.model_version,
        "candidate_bundle_sha256": candidate.bundle_sha256,
        "candidate_package_sha256": candidate_package.sha256,
        "parent_model_id": parent.model_id,
        "parent_version": parent.model_version,
        "parent_package_sha256": parent_package.sha256,
        "input_sha256": parent_package.input_sha256,
        "runtime_image": settings.scoring_image,
        "limitation": "Development fixture rows only; not validation or final-test evidence.",
    }
    status, passed, error = "completed", False, None
    identity = str(uuid4())
    try:
        snapshot = await asyncio.to_thread(
            verify_snapshot, parent_package.sha256, parent_package.file_hashes
        )
        manifest = json.loads((snapshot / "package/manifest.json").read_text())
        row_ids = read_inputs(
            snapshot / "data/development_inputs.csv", manifest["feature_order"]
        )
        bundle = await asyncio.to_thread(verify_uploaded_bundle, parent)
        reference = validate_predictions(
            (bundle / "fixtures/reference_predictions.csv").read_bytes(), row_ids
        )
        scored = await score_snapshot(candidate_package, "parity-" + identity)
        predictions = (settings.storage_path / scored["prediction_reference"]).read_bytes()
        evidence.update(
            reference_predictions_sha256=hashlib.sha256(reference).hexdigest(),
            candidate_predictions_sha256=scored["predictions_sha256"],
            **compare(row_ids, predictions, reference),
        )
        passed = evidence["passed"]
    except Exception as exc:
        status, error = "failed", (str(exc) or type(exc).__name__)[:500]
        evidence.update(passed=False, error=error)
    record = ParityRunRecord(
        id=identity,
        candidate_model_id=candidate.model_id,
        candidate_version=candidate.model_version,
        candidate_bundle_sha256=candidate.bundle_sha256,
        parent_model_id=parent.model_id,
        parent_version=parent.model_version,
        rebuild_run_id=rebuild_run_id or source.get("run_id"),
        initiator=initiator,
        actor_id=actor_id,
        status=status,
        passed=passed,
        result=evidence,
        error=error,
    )
    async with sessions() as session:
        session.add(record)
        session.add(
            AuditRecord(
                actor_id=actor_id,
                resource_id=identity,
                action="parity_run",
                payload={
                    "initiator": initiator,
                    "candidate_model_id": candidate.model_id,
                    "status": status,
                    "passed": passed,
                },
            )
        )
        await session.commit()
        await session.refresh(record)
    return record


def parity_read(record):
    return {
        "id": record.id,
        "candidate_model_id": record.candidate_model_id,
        "candidate_version": record.candidate_version,
        "candidate_bundle_sha256": record.candidate_bundle_sha256,
        "parent_model_id": record.parent_model_id,
        "parent_version": record.parent_version,
        "rebuild_run_id": record.rebuild_run_id,
        "initiator": record.initiator,
        "actor_id": record.actor_id,
        "status": record.status,
        "passed": record.passed,
        "error": record.error,
        "result": record.result,
        "created_at": record.created_at.replace(tzinfo=timezone.utc)
        if record.created_at.tzinfo is None
        else record.created_at,
    }
