"""The scoring service: every official score on the platform runs here.

It scores an artifact in a fresh container on the data the backend chooses, and trusted code
outside the container checks the output and compares or measures it. No agent takes part, so
the input may be final-test data. Agents see only what this module returns as aggregates.

Uses:

- reproduction check after Gate 0: run a bundle's scorer on the development partition. Its
  output becomes the official reference predictions.
- comparison with the reference: score another artifact, such as an intake wrapper, on the
  development partition and compare it with the reference. This is not official parity: Gate 2
  parity for rebuilds is the backend's `app/services/parity_service.run_parity`, the one check the
  agent's `run_parity` tool and the human reviewer share. This module does not replace it.
- validation: score the validation and final-test partitions, and the validator's challenger,
  and build the metrics bundle with a `MetricSuite`. The metric code and the frozen policy are
  the legacy owner's (Wit's), so the suite is passed in, not written here.

The artifact's CLI is the one rebuilds and wrappers already follow:
`score.py --input INPUT.csv --output OUTPUT.csv [--model-dir DIR]`, writing exactly
`row_id,positive_probability`, one row per input row, in input order.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import math
import os
import posixpath
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from harness.sandbox import (
    AsyncioProcessRunner,
    ProcessRunner,
    SandboxConfig,
    SandboxSlots,
    bind_mount_args,
)
from harness.schemas import SandboxUnavailable

CheckStatus = Literal["PASS", "FAIL", "REVIEW", "NOT_ASSESSABLE", "NOT_APPLICABLE"]

ARTIFACT_DIR = "/artifact"
INPUT_FILE = "/input/input.csv"
OUTPUT_DIR = "/output"
OUTPUT_FILE = f"{OUTPUT_DIR}/predictions.csv"
PREDICTION_COLUMNS = ["row_id", "positive_probability"]
ROW_ID_COLUMN = "row_id"

_TIMEOUT_EXIT_CODES = (124, 137)
_HOST_TIMEOUT_MARGIN_S = 15.0
_MAX_ERROR_CHARS = 2_000
_MAX_PREDICTION_BYTES = 512 * 1024 * 1024
PROTECTED_ERROR_TEXT = "The scorer failed on protected data. Its output is not shown."


# ---------------------------------------------------------------- jobs and results
class ScoringJob(BaseModel):
    """One scoring run. Every path is a host path chosen by the backend."""

    model_config = ConfigDict(frozen=True)

    artifact_path: str  # a folder, mounted read-only at /artifact
    entry_point: str = "score.py"  # relative to the artifact folder
    input_path: str  # a CSV file, mounted read-only
    # Adds `--model-dir /artifact`, for an agent-written wrapper inside a bundle that keeps the
    # archive's original paths.
    pass_model_dir: bool = False
    model_subdir: str = ""

    @field_validator("model_subdir")
    @classmethod
    def _model_subdir(cls, value: str) -> str:
        if value.startswith("/") or "\\" in value or ".." in value.split("/") or "\x00" in value:
            raise ValueError("model_subdir must remain inside the artifact")
        return value
    # Final-test data: the scorer's own output is never kept, because it could quote rows.
    protected: bool = False
    timeout_s: float = Field(600.0, gt=0, le=3_600)

    @field_validator("entry_point")
    @classmethod
    def _relative_entry(cls, value: str) -> str:
        if not value or value.startswith("/") or "\\" in value or ".." in value.split("/"):
            raise ValueError("entry_point must be a relative path inside the artifact")
        if not value.endswith((".py", ".R", ".r")):
            raise ValueError("entry_point must be a Python or R script")
        return posixpath.normpath(value)


class Predictions(BaseModel):
    row_ids: list[str]
    probabilities: list[float]
    sha256: str

    @model_validator(mode="after")
    def _same_length(self) -> Predictions:
        if len(self.row_ids) != len(self.probabilities):
            raise ValueError("there must be one probability per row ID")
        return self


class ScoringRun(BaseModel):
    status: Literal["ok", "not_executable", "invalid_output"]
    predictions: Predictions | None = None
    error: str | None = None
    input_sha256: str
    artifact_sha256: str
    duration_s: float


class ParityResult(BaseModel):
    status: CheckStatus
    tolerance: float
    rows: int = 0
    max_abs_diff: float | None = None
    mismatched_rows: int = 0
    worst_row_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class ReproductionCheck(BaseModel):
    status: CheckStatus
    run: ScoringRun
    comparison: ParityResult | None = None
    # The official reference predictions for parity, when the check produced them.
    reference: Predictions | None = None
    reason: str | None = None


# ---------------------------------------------------------------- metrics bundle
Number = float | int | None


class Check(BaseModel):
    """One check's aggregates. There is deliberately no place for row-level data."""

    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,100}$")
    status: CheckStatus
    section: int | None = Field(None, ge=1, le=11)  # report section, from the template
    partition: str | None = None
    values: dict[str, Number] = Field(default_factory=dict, max_length=50)
    interval: tuple[float, float] | None = None
    settings: dict[str, str | float | int | bool | None] = Field(default_factory=dict, max_length=30)
    note: str | None = Field(None, max_length=1_000)

    @field_validator("values")
    @classmethod
    def _finite(cls, values: dict[str, Number]) -> dict[str, Number]:
        for name, value in values.items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{name} is not a finite number; use None and a note")
        return values


class MetricsBundle(BaseModel):
    """Every official validation number, with the policy and data it came from."""

    policy_version: str
    created_at: datetime
    artifact_sha256: str
    data_sha256: dict[str, str]  # by partition
    checks: list[Check]

    def write(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2), encoding="utf-8")


class MetricSuite(Protocol):
    """The legacy owner's metric code under the frozen policy. It reads labels from the
    partition file itself, and returns aggregates only.
    """

    def evaluate(
        self,
        partition: str,
        input_path: str,
        model: Predictions,
        challenger: Predictions | None,
    ) -> list[Check]: ...


# ---------------------------------------------------------------- comparisons
def compare_predictions(
    candidate: Predictions, reference: Predictions, tolerance: float
) -> ParityResult:
    """Row-level comparison. Rows must match in number, ID and order before any value counts."""
    if len(candidate.row_ids) != len(reference.row_ids):
        return ParityResult(
            status="FAIL",
            tolerance=tolerance,
            rows=len(candidate.row_ids),
            reason=f"{len(candidate.row_ids)} rows against {len(reference.row_ids)} in the reference",
        )
    if candidate.row_ids != reference.row_ids:
        first = next(
            i for i, (a, b) in enumerate(zip(candidate.row_ids, reference.row_ids)) if a != b
        )
        return ParityResult(
            status="FAIL",
            tolerance=tolerance,
            rows=len(candidate.row_ids),
            reason=f"row IDs differ from the reference, first at position {first}",
        )
    diffs = [abs(a - b) for a, b in zip(candidate.probabilities, reference.probabilities)]
    worst = sorted(range(len(diffs)), key=lambda i: diffs[i], reverse=True)[:5]
    mismatched = sum(1 for d in diffs if d > tolerance)
    return ParityResult(
        status="PASS" if mismatched == 0 else "FAIL",
        tolerance=tolerance,
        rows=len(diffs),
        max_abs_diff=max(diffs, default=0.0),
        mismatched_rows=mismatched,
        worst_row_ids=[candidate.row_ids[i] for i in worst if diffs[i] > tolerance],
    )


def read_predictions(path: str | Path, input_path: str | Path | None = None) -> Predictions:
    """Parse and check a predictions file. Raises ValueError with a short reason."""
    path = Path(path)
    size = path.stat().st_size
    if size > _MAX_PREDICTION_BYTES:
        raise ValueError(f"the predictions file is too large: {size} bytes")
    data = path.read_bytes()
    row_ids: list[str] = []
    probabilities: list[float] = []
    reader = csv.reader(data.decode("utf-8").splitlines())
    header = next(reader, None)
    if header != PREDICTION_COLUMNS:
        raise ValueError(f"the header must be exactly {','.join(PREDICTION_COLUMNS)}")
    for line, row in enumerate(reader, start=2):
        if len(row) != 2:
            raise ValueError(f"line {line} does not have two columns")
        try:
            value = float(row[1])
        except ValueError:
            raise ValueError(f"line {line}: the probability is not a number") from None
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"line {line}: the probability is outside 0 to 1")
        row_ids.append(row[0])
        probabilities.append(value)
    if len(set(row_ids)) != len(row_ids):
        raise ValueError("row IDs are repeated")
    if input_path is not None:
        expected = _input_row_ids(Path(input_path))
        if len(expected) != len(row_ids):
            raise ValueError(f"{len(row_ids)} predictions for {len(expected)} input rows")
        if expected and all(e is not None for e in expected) and expected != row_ids:
            raise ValueError("row IDs do not match the input's row_id column in order")
    return Predictions(
        row_ids=row_ids, probabilities=probabilities, sha256=hashlib.sha256(data).hexdigest()
    )


def _input_row_ids(path: Path) -> list[str | None]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        has_ids = reader.fieldnames is not None and ROW_ID_COLUMN in reader.fieldnames
        return [row[ROW_ID_COLUMN] if has_ids else None for row in reader]


def sha256_path(path: str | Path) -> str:
    """A file's hash, or a folder's: every file's relative path and hash, in sorted order."""
    path = Path(path)
    digest = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    for file in sorted(p for p in path.rglob("*") if p.is_file() and not p.is_symlink()):
        digest.update(file.relative_to(path).as_posix().encode() + b"\0")
        digest.update(sha256_path(file).encode() + b"\0")
    return digest.hexdigest()


# ---------------------------------------------------------------- the service
class ScoringService:
    def __init__(
        self,
        *,
        config: SandboxConfig | None = None,
        runner: ProcessRunner | None = None,
        slots: SandboxSlots | None = None,
        work_dir: str | None = None,
    ) -> None:
        self.config = config or SandboxConfig()
        self.runner = runner or AsyncioProcessRunner()
        self.slots = slots
        self.work_dir = work_dir

    # ------------------------------------------------------------ one run
    async def score(self, job: ScoringJob) -> ScoringRun:
        artifact = Path(job.artifact_path)
        if not (artifact / job.entry_point).is_file():
            raise SandboxUnavailable("the entry point does not exist in the artifact")
        artifact_sha = await asyncio.to_thread(sha256_path, artifact)
        input_sha = await asyncio.to_thread(sha256_path, job.input_path)
        started = time.monotonic()
        async with self._slot(), self._output_dir() as output:
            name = f"mgr-score-{uuid4().hex[:12]}"
            result = await self._run(name, job, output)
            elapsed = time.monotonic() - started
            run = dict(input_sha256=input_sha, artifact_sha256=artifact_sha, duration_s=elapsed)
            if result.timed_out or result.returncode in _TIMEOUT_EXIT_CODES:
                return ScoringRun(
                    status="not_executable",
                    error=f"timed out after {job.timeout_s:g}s",
                    **run,
                )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-_MAX_ERROR_CHARS:]
                return ScoringRun(
                    status="not_executable",
                    error=PROTECTED_ERROR_TEXT
                    if job.protected
                    else f"exit code {result.returncode}: {detail}",
                    **run,
                )
            produced = Path(output) / Path(OUTPUT_FILE).name
            if not produced.is_file():
                return ScoringRun(
                    status="invalid_output", error="no predictions file was written", **run
                )
            try:
                predictions = await asyncio.to_thread(read_predictions, produced, job.input_path)
            except (ValueError, UnicodeDecodeError) as exc:
                reason = "the predictions file is not valid" if job.protected else str(exc)
                return ScoringRun(status="invalid_output", error=reason, **run)
            return ScoringRun(status="ok", predictions=predictions, **run)

    def run_command(self, name: str, job: ScoringJob, output_dir: str) -> list[str]:
        c = self.config
        interpreter = "python" if job.entry_point.endswith(".py") else "Rscript"
        command = [
            c.docker, "run", "--rm",
            "--name", name,
            "--network", "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(c.pids_limit),
            "--memory", c.memory,
            "--cpus", c.cpus,
            "--read-only",
            "--user", c.user,
            "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={c.tmp_size}",
            # The scorer's own folder, as when a person runs `Rscript score.R` there. Legacy
            # scorers find their model files from it. It is read-only: temporary files go to /tmp.
            "--workdir", posixpath.join(ARTIFACT_DIR, posixpath.dirname(job.entry_point)).rstrip("/"),
            *bind_mount_args(job.artifact_path, ARTIFACT_DIR),
            *bind_mount_args(job.input_path, INPUT_FILE),
        ]  # fmt: skip
        if "," in output_dir or "\n" in output_dir or "=" in output_dir:
            raise SandboxUnavailable("unsafe output path")
        command += ["--mount", f"type=bind,src={output_dir},dst={OUTPUT_DIR}"]
        argv = [
            "timeout", "--kill-after=5s", f"{job.timeout_s:g}s",
            interpreter, f"{ARTIFACT_DIR}/{job.entry_point}",
            "--input", INPUT_FILE,
            "--output", OUTPUT_FILE,
        ]  # fmt: skip
        if job.pass_model_dir:
            argv += ["--model-dir", posixpath.join(ARTIFACT_DIR, job.model_subdir).rstrip("/")]
        return [*command, c.image, *argv]

    async def _run(self, name: str, job: ScoringJob, output_dir: str) -> Any:
        command = self.run_command(name, job, output_dir)
        try:
            result = await self.runner.run(
                command,
                timeout_s=job.timeout_s + _HOST_TIMEOUT_MARGIN_S,
                max_output=self.config.max_output_chars,
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._kill(name))
            raise
        if result.timed_out:
            await self._kill(name)
        return result

    async def _kill(self, name: str) -> None:
        await self.runner.run(
            [self.config.docker, "kill", name],
            timeout_s=self.config.setup_timeout_s,
            max_output=self.config.max_output_chars,
        )

    def _slot(self) -> Any:
        return self.slots.hold() if self.slots is not None else _no_slot()

    @asynccontextmanager
    async def _output_dir(self) -> AsyncIterator[str]:
        path = tempfile.mkdtemp(prefix="mgr-score-", dir=self.work_dir)
        try:
            # The container runs as another user and must be able to write its predictions.
            os.chmod(path, 0o777)
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)

    # ------------------------------------------------------------ uses
    async def reproduction_check(
        self,
        job: ScoringJob,
        *,
        archive_reference_path: str | None = None,
        tolerance: float = 1e-8,
    ) -> ReproductionCheck:
        """Run a bundle's scorer on the development partition. When the archive held its own
        predictions, compare with them. The run's output becomes the official reference.
        """
        run = await self.score(job)
        if run.status == "not_executable":
            return ReproductionCheck(
                status="NOT_ASSESSABLE",
                run=run,
                reason="not executable in the platform environment",
            )
        if run.status == "invalid_output":
            return ReproductionCheck(status="FAIL", run=run, reason=run.error)
        assert run.predictions is not None
        if archive_reference_path is None:
            return ReproductionCheck(status="PASS", run=run, reference=run.predictions)
        try:
            archived = await asyncio.to_thread(
                read_predictions, archive_reference_path, job.input_path
            )
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            return ReproductionCheck(
                status="REVIEW",
                run=run,
                reference=run.predictions,
                reason=f"the archive's own predictions could not be read: {exc}",
            )
        comparison = compare_predictions(run.predictions, archived, tolerance)
        return ReproductionCheck(
            status=comparison.status,
            run=run,
            comparison=comparison,
            reference=run.predictions if comparison.status == "PASS" else None,
            reason=comparison.reason,
        )

    async def compare_with_reference(
        self, job: ScoringJob, reference: Predictions | None, *, tolerance: float = 1e-8
    ) -> ParityResult:
        """Score an artifact on the development partition and compare it with the reproduction
        check's reference. With no reference, the result is not assessable, never a pass.

        Not official parity: rebuild parity at Gate 2 is `app/services/parity_service.run_parity`.
        """
        if reference is None:
            return ParityResult(
                status="NOT_ASSESSABLE",
                tolerance=tolerance,
                reason="no official reference predictions: the legacy model did not run",
            )
        run = await self.score(job)
        if run.status != "ok" or run.predictions is None:
            return ParityResult(status="FAIL", tolerance=tolerance, reason=run.error)
        return compare_predictions(run.predictions, reference, tolerance)

    async def metrics_bundle(
        self,
        partitions: dict[str, ScoringJob],
        suite: MetricSuite,
        *,
        policy_version: str,
        challengers: dict[str, ScoringJob] | None = None,
    ) -> MetricsBundle:
        """Score each partition, and the challenger where given, and measure with the suite.

        A scorer that fails gives an implementation check instead of metrics, so a missing
        number is visible as NOT_ASSESSABLE rather than absent.
        """
        challengers = challengers or {}
        checks: list[Check] = []
        data_sha: dict[str, str] = {}
        artifact_sha = ""
        for partition, job in partitions.items():
            run = await self.score(job)
            artifact_sha = run.artifact_sha256
            data_sha[partition] = run.input_sha256
            checks.append(_scoring_check(f"implementation.scoring.{partition}", partition, run))
            if run.predictions is None:
                continue
            challenger = None
            if partition in challengers:
                challenger_run = await self.score(challengers[partition])
                checks.append(
                    _scoring_check(f"challenger.scoring.{partition}", partition, challenger_run)
                )
                challenger = challenger_run.predictions
            checks += suite.evaluate(partition, job.input_path, run.predictions, challenger)
        ids = [c.check_id for c in checks]
        if len(set(ids)) != len(ids):
            raise ValueError("the metric suite returned repeated check IDs")
        return MetricsBundle(
            policy_version=policy_version,
            created_at=datetime.now(timezone.utc),
            artifact_sha256=artifact_sha,
            data_sha256=data_sha,
            checks=checks,
        )


def _scoring_check(check_id: str, partition: str, run: ScoringRun) -> Check:
    status: CheckStatus = {
        "ok": "PASS",
        "invalid_output": "FAIL",
        "not_executable": "NOT_ASSESSABLE",
    }[run.status]
    rows = len(run.predictions.row_ids) if run.predictions else None
    return Check(
        check_id=check_id,
        status=status,
        section=5,
        partition=partition,
        values={"rows": rows},
        note=None if run.status == "ok" else (run.error or "")[:1_000],
    )


@asynccontextmanager
async def _no_slot() -> AsyncIterator[None]:
    yield
