"""Fresh-container scoring for intake bundles, including nested entry points."""

import asyncio
import csv
import io
import json
from uuid import uuid4
from app.config import settings
from app.services.package_service import verify_snapshot, PackageError, digest


def service():
    from harness.scoring import ScoringService
    from harness.sandbox import SandboxConfig

    config = SandboxConfig.from_env()
    if settings.scoring_image:
        config = config.model_copy(update={"image": settings.scoring_image})
    from app.services.resource_limits import limits

    return ScoringService(config=config, slots=limits()[0])


async def score(package, inputs, protected=True):
    from harness.scoring import ScoringJob

    snapshot = await asyncio.to_thread(
        verify_snapshot, package.sha256, package.file_hashes
    )
    manifest = json.loads((snapshot / "package/manifest.json").read_text())
    if not manifest.get("executable"):
        raise PackageError(
            "NOT_ASSESSABLE: fitted model cannot execute in the platform environment"
        )
    result = await service().score(
        ScoringJob(
            artifact_path=str(snapshot / "package"),
            entry_point=manifest["entry_point"],
            input_path=str(inputs),
            pass_model_dir=manifest.get("pass_model_dir", False),
            model_subdir=manifest.get("model_subdir", ""),
            protected=protected,
            timeout_s=settings.scoring_timeout_seconds,
        )
    )
    await asyncio.to_thread(verify_snapshot, package.sha256, package.file_hashes)
    if result.status != "ok":
        raise PackageError("NOT_ASSESSABLE: fresh-container scoring failed")
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["row_id", "positive_probability"])
    writer.writerows(
        zip(result.predictions.row_ids, result.predictions.probabilities, strict=True)
    )
    return stream.getvalue().encode()


async def reproduce(package, reference_path=None):
    from app.services.package_service import verify_snapshot

    snapshot = verify_snapshot(package.sha256, package.file_hashes)
    try:
        raw = await score(
            package, snapshot / "data/development_inputs.csv", protected=False
        )
    except Exception:
        return {
            "status": "NOT_ASSESSABLE",
            "reason": "Fresh-container development scoring failed",
        }
    identity = str(uuid4())
    folder = settings.storage_path / "scoring" / identity
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "predictions.csv").write_bytes(raw)
    status, comparison = "PASS", None
    reference = (
        reference_path or snapshot / "package/fixtures/reference_predictions.csv"
    )
    if reference.exists():
        from harness.scoring import read_predictions, compare_predictions

        try:
            comparison = compare_predictions(
                read_predictions(folder / "predictions.csv"),
                read_predictions(reference),
                1e-8,
            ).model_dump()
            status = comparison["status"]
        except (ValueError, OSError):
            status = "REVIEW"
    return {
        "status": status,
        "comparison": comparison,
        "scope": "development_reproduction",
        "execution_id": identity,
        "prediction_reference": f"scoring/{identity}/predictions.csv",
        "predictions_sha256": digest(folder / "predictions.csv"),
        "n_rows": package.n_rows,
    }
