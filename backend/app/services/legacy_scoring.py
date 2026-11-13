"""Development-only scoring in a fresh Docker container; never execute on the host."""

import asyncio
import csv
import hashlib
import io
import json
import math
import os
import re
from uuid import uuid4
from datetime import datetime, timezone

from sqlalchemy import update
from app.config import settings, FROZEN_SCORING_IMAGE
from app.db.tables import ScoringRecord, PackageRecord
from app.services.package_service import verify_snapshot, read_inputs, PackageError


class ScoringError(RuntimeError):
    pass


async def docker(*args, timeout=20, limit=2_000_000):
    env = {
        k: os.environ[k]
        for k in (
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "TMPDIR",
        )
        if k in os.environ
    }
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        raise ScoringError(
            "Docker is unavailable. Start the configured Docker runtime."
        ) from None

    async def read(stream):
        parts, size = [], 0
        while chunk := await stream.read(65536):
            size += len(chunk)
            if size > limit:
                raise ScoringError("Scoring process output exceeded its limit.")
            parts.append(chunk)
        return b"".join(parts)

    readers = [
        asyncio.create_task(read(process.stdout)),
        asyncio.create_task(read(process.stderr)),
    ]

    async def collect():
        output = await asyncio.gather(*readers)
        return output[0], await process.wait()

    try:
        stdout, code = await asyncio.wait_for(collect(), timeout)
        if code:
            raise ScoringError(
                "Docker/scorer command failed. Check runtime and package compatibility."
            )
        return stdout
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        raise


def create_command(name, image, snapshot, language, output_limit=2_000_000):
    if image != FROZEN_SCORING_IMAGE and not re.fullmatch(
        r"sha256:[0-9a-f]{64}", image or ""
    ):
        raise ScoringError(
            "Configure SCORING_IMAGE with the approved frozen registry digest or a verified immutable local Docker image ID."
        )
    if "," in str(snapshot):
        raise ScoringError("Unsupported storage path.")
    interpreter, entry = (
        ("python", "score.py") if language == "Python" else ("Rscript", "score.R")
    )
    return [
        "create",
        "--name",
        name,
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=1000:1000",
        "--memory=2g",
        "--memory-swap=2g",
        "--cpus=1",
        "--pids-limit=128",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=128m,mode=1777",
        "--log-driver=none",
        "--mount",
        f"type=bind,src={snapshot},dst=/inputs,readonly",
        "--workdir=/inputs/package",
        "--entrypoint=/usr/bin/env",
        image,
        "-i",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "HOME=/tmp",
        "PYTHONDONTWRITEBYTECODE=1",
        "OMP_NUM_THREADS=1",
        "OPENBLAS_NUM_THREADS=1",
        "timeout",
        "--signal=KILL",
        str(settings.scoring_timeout_seconds),
        "python",
        "-c",
        "import subprocess,sys,pathlib; "
        "subprocess.run(sys.argv[1:],check=True,stdout=sys.stderr); "
        "p=pathlib.Path('/tmp/predictions.csv'); "
        f"assert p.stat().st_size <= {int(output_limit)}; "
        "sys.stdout.buffer.write(p.read_bytes())",
        interpreter,
        entry,
        "--input",
        "/inputs/data/development_inputs.csv",
        "--output",
        "/tmp/predictions.csv",
    ]


def validate_predictions(content, expected_ids):
    reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
    if reader.fieldnames != ["row_id", "positive_probability"]:
        raise ScoringError("Invalid prediction columns.")
    ids = []
    for row in reader:
        try:
            probability = float(row["positive_probability"])
            if (
                None in row
                or not math.isfinite(probability)
                or not 0 <= probability <= 1
            ):
                raise ValueError()
        except (ValueError, TypeError):
            raise ScoringError("Invalid prediction probability.") from None
        ids.append(row["row_id"])
    if ids != expected_ids:
        raise ScoringError(
            "Prediction row IDs/order do not match the development input."
        )
    return content


async def score_snapshot(package, execution_id):
    snapshot = await asyncio.to_thread(
        verify_snapshot, package.sha256, package.file_hashes
    )
    manifest = json.loads((snapshot / "package/manifest.json").read_text())
    if manifest.get("ai_native"):
        from app.services.native_scoring import score
        content = await score(package, snapshot / "data/development_inputs.csv", protected=False)
        folder = settings.storage_path / "scoring" / execution_id
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "predictions.csv").write_bytes(content)
        return {"scope": "development_fixture_scoring", "package_id": package.id,
                "package_sha256": package.sha256, "input_sha256": package.input_sha256,
                "predictions_sha256": hashlib.sha256(content).hexdigest(),
                "prediction_reference": f"scoring/{execution_id}/predictions.csv",
                "runtime_image": settings.scoring_image, "n_rows": package.n_rows,
                "limitation": "Development scoring only; not validation or migration parity."}
    ids = read_inputs(
        snapshot / "data/development_inputs.csv", manifest["feature_order"]
    )
    name = "model-score-" + uuid4().hex
    image = settings.scoring_image
    command = create_command(name, image, snapshot, package.language)
    try:
        await docker(*command)
        content = await docker(
            "start", "--attach", name, timeout=settings.scoring_timeout_seconds
        )
        exit_code = await docker("inspect", "--format={{.State.ExitCode}}", name)
        if exit_code.strip() != b"0":
            raise ScoringError("Legacy scorer exited unsuccessfully.")
        content = validate_predictions(content, ids)
        # Detect a local snapshot change during execution as well as before it.
        await asyncio.to_thread(verify_snapshot, package.sha256, package.file_hashes)
        folder = settings.storage_path / "scoring" / execution_id
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "predictions.csv").write_bytes(content)
        return {
            "scope": "development_fixture_scoring",
            "package_id": package.id,
            "package_sha256": package.sha256,
            "input_sha256": package.input_sha256,
            "predictions_sha256": hashlib.sha256(content).hexdigest(),
            "prediction_reference": f"scoring/{execution_id}/predictions.csv",
            "runtime_image": image,
            "n_rows": len(ids),
            "limitation": "Scoring only; not validation, final-test evaluation, or migration parity.",
        }
    finally:
        # Also stop containers after timeout/cancellation; no host execution fallback.
        await asyncio.shield(docker("rm", "--force", name))


async def process_scoring(execution_id, sessions):
    async with sessions() as session:
        claimed = await session.execute(
            update(ScoringRecord)
            .where(ScoringRecord.id == execution_id, ScoringRecord.status == "queued")
            .values(status="running", started_at=datetime.now(timezone.utc))
        )
        await session.commit()
        if claimed.rowcount != 1:
            return
        record = await session.get(ScoringRecord, execution_id)
        package = await session.get(PackageRecord, record.package_id)
    result, error = None, None
    try:
        result = await score_snapshot(package, execution_id)
    except (ScoringError, PackageError) as exc:
        error = str(exc)
    except TimeoutError:
        error = "Scoring exceeded its time limit."
    except Exception:
        error = "Scoring failed; check the registered package and runtime."
    async with sessions() as session:
        await session.execute(
            update(ScoringRecord)
            .where(ScoringRecord.id == execution_id, ScoringRecord.status == "running")
            .values(
                status="failed" if error else "completed",
                error=error,
                result=result,
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
