"""AI-native runs: survey, intake, validation, triage and investigation.

Backend glue for the harness kinds in `harness/handoff.py`. This is the one path every AI-native
agent run takes: archive intake (`native_runner`) and the validation, triage and investigation
analyses (`native_analysis`) create and execute their agent runs here.

- A run is persisted before it starts (`create_run`), and its ID is the request ID.
- Inputs are immutable copies under `storage/ai_native/inputs`, registered with an ID that goes in
  the request context (`register_input` and the helpers below). The resolver mounts only
  registered inputs, checks their hash, and checks a metrics folder again before mounting it.
- The gateway offers `export_artifact` and, where the profile allows it, `create_proposal`.
  Exports are copied out of the task's workspace and stored by run. A proposal goes into the
  platform's one proposals table (`proposal_service`) as `pending`, bound to the platform run that
  started this agent run, and nothing runs until a person approves and executes it.
- After the task, the backend checks all exports with `handoff.check_exports`. Problems start a
  follow-up round on the same thread, up to `MAX_CORRECTION_ROUNDS`; then the run fails with the
  problems.
- Progress events are saved without reply text, tool arguments or tool output.
"""

from __future__ import annotations

import asyncio
import io
import logging
import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from sqlalchemy import JSON, ForeignKey, String, Text, select, update
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.db.tables import AuditRecord, Base
from app.services import proposal_service  # noqa: F401  registers action_proposals for the link table
from app.services import upload_service  # noqa: F401  inventory: proposal preconditions read it
from contracts.harness import (
    Actor,
    HarnessRequest,
    HarnessResult,
    HarnessUsage,
    RequestContext,
    ToolResult,
    ToolSpec,
)

INPUT_FIELDS: dict[str, str] = {
    "archive": "archive_id",
    "data": "development_data_id",
    "document": "document_id",
    "metrics": "metrics_bundle_id",
    "monitoring": "monitoring_run_id",
    "reports": "validation_report_id",
}
MOUNT_FIELDS: dict[str, str] = {
    "archive": "archive_path",
    "data": "development_data_path",
    "document": "document_path",
    "metrics": "metrics_path",
    "monitoring": "monitoring_path",
    "reports": "reports_path",
}
MAX_INPUT_BYTES = 2_000_000_000
# Monitoring windows whose rows an agent may read: never the final-test partition.
AGENT_READABLE_SCOPES = ("uploaded_batch", "validation_partition_simulation")
MAX_EXPORT_FILES = 512
MAX_EXPORT_BYTES = 64_000_000
SAFE_EVENT_KEYS = ("name", "status", "proposal_id", "error_code", "code", "turn")
PUBLIC_EVENT_TYPES = (
    "task_started",
    "tool_call",
    "tool_result",
    "proposal_created",
    "error",
    "done",
)


logger = logging.getLogger(__name__)


def now() -> datetime:
    return datetime.now(timezone.utc)


class AiNativeError(ValueError):
    pass


# ---------------------------------------------------------------- tables
class AiNativeRunRecord(Base):
    __tablename__ = "ai_native_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True, default="queued")
    actor_id: Mapped[str] = mapped_column(String(120))
    actor_role: Mapped[str] = mapped_column(String(40))
    context: Mapped[dict] = mapped_column(JSON)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Context IDs for the run this one starts when it is accepted, such as intake's data.
    follow_up: Mapped[dict] = mapped_column(JSON, default=dict)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    correction_round: Mapped[int] = mapped_column(default=0)
    problems: Mapped[list] = mapped_column(JSON, default=list)
    outcome: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=now)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class AiNativeInputRecord(Base):
    __tablename__ = "ai_native_inputs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    path: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=now)


class AiNativeExportRecord(Base):
    __tablename__ = "ai_native_exports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ai_native_runs.id"), index=True)
    task_id: Mapped[str] = mapped_column(String(36))
    export_path: Mapped[str] = mapped_column(String(200))  # what the agent exported
    file: Mapped[str] = mapped_column(String(300))  # one file in it
    media_type: Mapped[str] = mapped_column(String(60))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column()
    storage_path: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=now)


class AiNativeRunLink(Base):
    """The platform run (validate or monitor) an analysis agent run belongs to."""

    __tablename__ = "ai_native_run_links"
    run_id: Mapped[str] = mapped_column(ForeignKey("ai_native_runs.id"), primary_key=True)
    platform_run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)


class AiNativeProposalLink(Base):
    """Which agent run created a proposal in `action_proposals`."""

    __tablename__ = "ai_native_run_proposals"
    proposal_id: Mapped[str] = mapped_column(ForeignKey("action_proposals.id"), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ai_native_runs.id"), index=True)


# ---------------------------------------------------------------- inputs
def _storage() -> Path:
    return settings.storage_path / "ai_native"


def _copy_tree(source: Path, target: Path) -> None:
    total = 0
    for path in source.rglob("*"):
        if path.is_symlink():
            raise AiNativeError(f"inputs may not contain links: {path.relative_to(source)}")
        if path.is_file():
            total += path.stat().st_size
            if total > MAX_INPUT_BYTES:
                raise AiNativeError("the input is too large")
    shutil.copytree(source, target, symlinks=False)


async def register_input(
    sessions, kind: str, source: Path | dict[str, bytes], *, meta: dict | None = None
) -> str:
    """Copy an input into storage and return its ID, for the request context.

    `source` is a folder, or a mapping of relative file name to bytes.
    """
    from harness.metrics_bundle import bundle_mount_problems
    from harness.sandbox import safe_relative_path
    from harness.scoring import sha256_path

    if kind not in INPUT_FIELDS:
        raise AiNativeError(f"unknown input kind {kind}")
    identity = str(uuid4())
    target = _storage() / "inputs" / identity

    def write() -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(source, dict):
            target.mkdir()
            for name, content in source.items():
                path = target / safe_relative_path(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        else:
            if not Path(source).is_dir():
                raise AiNativeError("an input must be a folder")
            _copy_tree(Path(source), target)
        if kind == "metrics":
            problems = bundle_mount_problems(_read_files(target))
            if problems:
                shutil.rmtree(target, ignore_errors=True)
                raise AiNativeError("; ".join(problems))
        return sha256_path(target)

    digest = await asyncio.to_thread(write)
    async with sessions() as session:
        session.add(
            AiNativeInputRecord(
                id=identity, kind=kind, path=str(target), sha256=digest, meta=meta or {}
            )
        )
        await session.commit()
    return identity


def _read_files(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


async def register_metrics_bundle(
    sessions,
    report: dict[str, Any],
    *,
    data_sha256: dict[str, str] | None = None,
    expected_artifact_sha256: str | None = None,
    challenger: dict[str, Any] | None = None,
) -> str:
    """The validator's metrics input, from the official validation report payload."""
    from harness.metrics_bundle import bundle_from_validation_report
    from harness.validation_report import METRICS_BUNDLE_FILE

    bundle = bundle_from_validation_report(
        report,
        data_sha256=data_sha256,
        expected_artifact_sha256=expected_artifact_sha256,
        challenger=challenger,
    )
    return await register_input(
        sessions,
        "metrics",
        {METRICS_BUNDLE_FILE: bundle.model_dump_json(indent=2).encode()},
        meta={"artifact_sha256": bundle.artifact_sha256, "policy_version": bundle.policy_version},
    )


async def register_monitoring(
    sessions,
    result: dict[str, Any],
    *,
    batch: Path | None = None,
    triage_report: bytes | None = None,
) -> str:
    """The monitoring input: the stability engine's window result, which holds aggregates only,
    and the feature batch for an uploaded batch or a simulation built from the validation
    partition. A window from any other source, such as the final-test partition, never reaches an
    agent."""
    files = {"monitoring_summary.json": json.dumps(result, indent=2, allow_nan=False).encode()}
    if batch is not None:
        if result.get("input_scope") not in AGENT_READABLE_SCOPES:
            raise AiNativeError("only an uploaded batch or a validation-partition simulation may be mounted")
        files["batch.csv"] = await asyncio.to_thread(Path(batch).read_bytes)
    if triage_report is not None:
        files["triage_report.md"] = triage_report
    return await register_input(
        sessions, "monitoring", files, meta={"band": result.get("band"), "source": result.get("input_scope")}
    )


async def input_mounts(sessions, info: Any) -> dict[str, str]:
    """`SandboxMounts` fields for the AI-native inputs named in a task's trusted context."""
    from harness.metrics_bundle import bundle_mount_problems
    from harness.scoring import sha256_path

    root = _storage().resolve() / "inputs"
    mounts: dict[str, str] = {}
    for kind, field in INPUT_FIELDS.items():
        identity = getattr(info, field, None)
        if not identity:
            continue
        async with sessions() as session:
            record = await session.get(AiNativeInputRecord, identity)
        if kind == "data" and getattr(info, "model_package_id", None) and record is None:
            continue  # the package's own data, chosen by the package resolver
        if record is None or record.kind != kind:
            raise AiNativeError(f"{field} is not a registered {kind} input")
        path = Path(record.path).resolve()
        if not path.is_relative_to(root) or not path.is_dir():
            raise AiNativeError(f"{field} is outside input storage")
        if await asyncio.to_thread(sha256_path, path) != record.sha256:
            raise AiNativeError(f"{field} changed after it was registered")
        if kind == "metrics":
            problems = await asyncio.to_thread(lambda: bundle_mount_problems(_read_files(path)))
            if problems:
                raise AiNativeError("; ".join(problems))
        mounts[MOUNT_FIELDS[kind]] = str(path)
    return mounts


async def _input_path(sessions, identity: str | None, kind: str) -> Path | None:
    if not identity:
        return None
    async with sessions() as session:
        record = await session.get(AiNativeInputRecord, identity)
    return Path(record.path) if record and record.kind == kind else None


# ---------------------------------------------------------------- the workspace
_READ_SCRIPT = """import io,pathlib,sys,zipfile
base=pathlib.Path('/workspace').resolve(); p=(base/sys.argv[1])
assert not p.is_symlink() and p.resolve().is_relative_to(base), 'outside the workspace'
files=[p] if p.is_file() else sorted(x for x in p.rglob('*') if x.is_file()) if p.is_dir() else []
assert files, 'nothing at that path'
assert len(files)<=%d, 'too many files'
buf=io.BytesIO(); total=0
with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
 for f in files:
  assert not f.is_symlink() and f.resolve().is_relative_to(base), 'a link in the export'
  total+=f.stat().st_size; assert total<=%d, 'the export is too large'
  z.writestr(f.relative_to(base).as_posix(), f.read_bytes())
sys.stdout.buffer.write(buf.getvalue())
""" % (MAX_EXPORT_FILES, MAX_EXPORT_BYTES)

WorkspaceReader = Callable[[str, str], Awaitable[dict[str, bytes]]]


async def docker_read_workspace(task_id: str, relative: str) -> dict[str, bytes]:
    """Files under one workspace path, by path relative to the workspace."""
    from app.services.legacy_scoring import docker

    raw = await docker(
        "exec", "mgr-task-" + task_id, "python", "-c", _READ_SCRIPT, relative,
        timeout=60, limit=MAX_EXPORT_BYTES * 2,
    )  # fmt: skip
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


# ---------------------------------------------------------------- the gateway
EXPORT_TOOL = ToolSpec(
    name="export_artifact",
    description=(
        "Export a file or folder from your workspace as an official artifact. The platform "
        "checks it and says what to fix if it cannot accept it."
    ),
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
    risk="write",
)


class AiNativeGateway:
    """The platform tools of one AI-native run, bound to its trusted request."""

    def __init__(
        self,
        request: HarnessRequest,
        sessions,
        *,
        read_workspace: WorkspaceReader = docker_read_workspace,
        check_role: bool = True,
    ) -> None:
        from harness.handoff import KIND_SPECS

        self.request, self.sessions = request, sessions
        self.spec = KIND_SPECS[request.kind]
        self.run_id = request.context.run_id
        self.read_workspace = read_workspace
        self.check_role = check_role

    async def list_tools(self) -> list[ToolSpec]:
        from harness.proposals import proposal_tool_spec

        tools = [EXPORT_TOOL]
        if self.spec.proposals:
            spec = proposal_tool_spec(self.spec.proposals)
            tools.append(ToolSpec.model_validate(spec.model_dump()))
        return tools

    async def call(self, name: str, args: dict[str, Any], idempotency_key: str) -> ToolResult:
        if name not in {t.name for t in await self.list_tools()}:
            return ToolResult(status="denied", content="Tool not permitted")
        if self.check_role:
            from app.services.harness_adapter import actor_role

            try:
                role = await actor_role(self.sessions, self.request.actor.id)
            except Exception:
                return ToolResult(status="denied", content="Actor is disabled or unavailable")
            if role != self.request.actor.role:
                return ToolResult(status="denied", content="Actor role changed; start a new run")
        async with self.sessions() as session:
            prior = await session.scalar(
                select(AuditRecord).where(
                    AuditRecord.resource_id == idempotency_key,
                    AuditRecord.action == "ai_native_tool_result",
                )
            )
            if prior:
                return ToolResult.model_validate(prior.payload)
        try:
            if name == "export_artifact":
                result = await self._export(args)
            else:
                result = await self._propose(args, idempotency_key)
        except AiNativeError as exc:
            result = ToolResult(status="error", content=str(exc))
        except Exception as exc:
            # Log the type only: a message could quote tool arguments or data.
            logger.error("ai-native tool %s failed: %s", name, type(exc).__name__)
            result = ToolResult(status="error", content="The platform could not complete the call.")
        async with self.sessions() as session:
            session.add(
                AuditRecord(
                    actor_id=self.request.actor.id,
                    resource_id=idempotency_key,
                    action="ai_native_tool_result",
                    payload=result.model_dump(),
                )
            )
            await session.commit()
        return result

    async def _task_id(self) -> str:
        from app.services.harness_adapter import task_info_for_request

        info = await task_info_for_request(self.sessions, self.request)
        return info.task_id

    async def _export(self, args: dict[str, Any]) -> ToolResult:
        from harness.handoff import check_exports
        from harness.sandbox import safe_relative_path

        try:
            relative = safe_relative_path(args.get("path"))
        except Exception:
            return ToolResult(status="denied", content="Invalid path")
        allowed = {e.path: e for e in self.spec.exports}
        export = allowed.get(relative.rstrip("/"))
        if export is None:
            return ToolResult(
                status="denied", content="Export one of: " + ", ".join(allowed)
            )
        task_id = await self._task_id()
        files = await self.read_workspace(task_id, export.path)
        if export.kind == "file" and list(files) != [export.path]:
            return ToolResult(status="error", content=f"{export.path} must be a single file")
        saved = await save_exports(self.sessions, self.run_id, task_id, export, files)
        # Early feedback on this export alone; the full check runs when the task ends.
        context = await check_context(self.sessions, await _run(self.sessions, self.run_id))
        check = check_exports(self.request.kind, files, **context)
        own = [p for p in check.problems if p.startswith(export.path) and "was not exported" not in p]
        if own:
            return ToolResult(
                status="error",
                content="Exported, but the platform cannot accept it yet. Fix and export again:\n- "
                + "\n- ".join(own[:20]),
            )
        return ToolResult(
            status="ok", content=json.dumps({"export_id": saved[0], "files": len(saved)})
        )

    async def _propose(self, args: dict[str, Any], key: str) -> ToolResult:
        from fastapi import HTTPException
        from harness.proposals import proposal_problems
        from app.services import proposal_service

        # The backend validates again: the harness's check is not a trust boundary.
        problems = proposal_problems(self.spec.proposals, args)
        if problems:
            return ToolResult(status="error", content="Proposal not recorded: " + "; ".join(problems))
        async with self.sessions() as session:
            link = await session.get(AiNativeRunLink, self.run_id)
            if link is None:
                return ToolResult(status="error", content="This run has no platform run to bind a proposal to.")
            try:
                # The target model, version and monitoring record come from the platform run.
                record = await proposal_service.create(
                    session,
                    link.platform_run_id,
                    args,
                    self.request.actor.id,
                    key,
                    task_id=await self._task_id(),
                )
            except HTTPException as exc:
                await session.rollback()
                return ToolResult(status="error", content=f"Proposal not recorded: {exc.detail}")
            if await session.get(AiNativeProposalLink, record.id) is None:
                session.add(AiNativeProposalLink(proposal_id=record.id, run_id=self.run_id))
            await session.commit()
        return ToolResult(
            status="ok",
            content="Proposal recorded. Nothing runs until an authorised person approves it.",
            proposal_id=record.id,
        )


async def save_exports(sessions, run_id: str, task_id: str, export: Any, files: dict[str, bytes]) -> list[str]:
    import hashlib

    folder = _storage() / "runs" / run_id / uuid4().hex[:12]
    ids: list[str] = []

    def write() -> None:
        for name, content in files.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    await asyncio.to_thread(write)
    async with sessions() as session:
        for name, content in files.items():
            identity = str(uuid4())
            ids.append(identity)
            session.add(
                AiNativeExportRecord(
                    id=identity,
                    run_id=run_id,
                    task_id=task_id,
                    export_path=export.path,
                    file=name,
                    media_type=export.media_type,
                    sha256=hashlib.sha256(content).hexdigest(),
                    size=len(content),
                    storage_path=str(folder / name),
                )
            )
        await session.commit()
    return ids


# ---------------------------------------------------------------- checks
async def _run(sessions, run_id: str) -> AiNativeRunRecord:
    async with sessions() as session:
        run = await session.get(AiNativeRunRecord, run_id)
    if run is None:
        raise AiNativeError("unknown run")
    return run


async def latest_exports(sessions, run_id: str) -> tuple[dict[str, bytes], list[str]]:
    """The newest export of each path, as files, and their export IDs."""
    async with sessions() as session:
        records = list(
            await session.scalars(
                select(AiNativeExportRecord)
                .where(AiNativeExportRecord.run_id == run_id)
                .order_by(AiNativeExportRecord.created_at)
            )
        )
    def folder(record: AiNativeExportRecord) -> str:
        return record.storage_path[: -len(record.file) - 1]

    newest = {r.export_path: folder(r) for r in records}  # the last save of each path wins
    chosen = [r for r in records if folder(r) == newest[r.export_path]]
    files = {r.file: Path(r.storage_path).read_bytes() for r in chosen}
    return files, [r.id for r in chosen]


async def proposal_types(sessions, run_id: str) -> list[str]:
    from app.services.proposal_service import ProposalRecord

    async with sessions() as session:
        return list(
            await session.scalars(
                select(ProposalRecord.type)
                .join(AiNativeProposalLink, AiNativeProposalLink.proposal_id == ProposalRecord.id)
                .where(AiNativeProposalLink.run_id == run_id)
            )
        )


async def proposal_ids(sessions, run_id: str) -> list[str]:
    async with sessions() as session:
        return list(
            await session.scalars(
                select(AiNativeProposalLink.proposal_id).where(AiNativeProposalLink.run_id == run_id)
            )
        )


async def check_context(sessions, run: AiNativeRunRecord) -> dict[str, Any]:
    """What the backend knows that `check_exports` compares the exports with."""
    from harness.validation_report import METRICS_BUNDLE_FILE

    context = run.context
    out: dict[str, Any] = {}
    if run.kind == "survey":
        archive = await _input_path(sessions, context.get("archive_id"), "archive")
        if archive is not None:
            out["archive_files"] = sorted(_read_names(archive))
    if run.kind == "intake":
        out["candidate_id"] = context.get("candidate_id")
        data = await _input_path(sessions, context.get("development_data_id"), "data")
        if data is not None:
            out["dataset_ids"] = sorted(p.name for p in data.iterdir() if p.is_dir())
        if run.source_run_id:
            survey_files, _ = await latest_exports(sessions, run.source_run_id)
            survey = json.loads(survey_files.get("survey.json", b"{}") or b"{}")
            for candidate in survey.get("candidates", []):
                if candidate.get("candidate_id") == context.get("candidate_id"):
                    out["candidate_files"] = candidate.get("files", [])
    if run.kind == "validation":
        metrics = await _input_path(sessions, context.get("metrics_bundle_id"), "metrics")
        if metrics is not None:
            out["metrics_bundle"] = json.loads((metrics / METRICS_BUNDLE_FILE).read_text())
    out["proposals"] = await proposal_types(sessions, run.id)
    return out


def _read_names(root: Path) -> list[str]:
    return [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]


# ---------------------------------------------------------------- runs
async def create_run(
    sessions,
    kind: str,
    actor: Actor,
    context: dict[str, Any],
    *,
    prompt: str | None = None,
    source_run_id: str | None = None,
    follow_up: dict[str, Any] | None = None,
    platform_run_id: str | None = None,
) -> str:
    """Persist a queued run and return its ID. Refuses a request that could not start.

    `platform_run_id` is the validate or monitor run this agent run works for. Proposals are bound
    to it, so a run that may propose needs one.
    """
    from harness.handoff import request_problems

    identity = str(uuid4())
    context = {**context, "run_id": identity}
    RequestContext.model_validate(context)
    problems = request_problems(kind, context, prompt)
    if problems:
        raise AiNativeError("; ".join(problems))
    async with sessions() as session:
        session.add(
            AiNativeRunRecord(
                id=identity,
                kind=kind,
                actor_id=actor.id,
                actor_role=actor.role,
                context=context,
                prompt=prompt,
                source_run_id=source_run_id,
                follow_up=follow_up or {},
            )
        )
        if platform_run_id:
            await session.flush()
            session.add(AiNativeRunLink(run_id=identity, platform_run_id=platform_run_id))
        await session.commit()
    return identity


def _safe_event(event: Any) -> dict[str, Any]:
    data = event.data or {}
    safe = {k: data[k] for k in SAFE_EVENT_KEYS if isinstance(data.get(k), (str, int, bool))}
    if event.type == "proposal_created" and isinstance(data.get("type"), str):
        safe["proposal_type"] = data["type"]
    return {"type": event.type, "seq": event.seq, "task_id": event.task_id, **safe}


async def execute_run(
    sessions,
    runner: Any,
    run_id: str,
    *,
    cancel: Any = None,
    read_workspace: WorkspaceReader = docker_read_workspace,
    check_role: bool = True,
) -> HarnessResult:
    """Run a queued run to its end, with correction rounds, and record the outcome."""
    from harness.handoff import MAX_CORRECTION_ROUNDS, check_exports, correction_prompt

    async with sessions() as session:
        claim = await session.execute(
            update(AiNativeRunRecord)
            .where(AiNativeRunRecord.id == run_id, AiNativeRunRecord.status == "queued")
            .values(status="running", started_at=now())
        )
        await session.commit()
    if claim.rowcount != 1:
        raise AiNativeError("the run is not queued")
    run = await _run(sessions, run_id)
    cancel = cancel or asyncio.Event()
    usage = HarnessUsage()
    task_id: str | None = None
    problems: list[str] = []

    async def emit(event: Any) -> None:
        if event.type not in PUBLIC_EVENT_TYPES:
            return
        async with sessions() as session:
            session.add(
                AuditRecord(
                    actor_id=run.actor_id,
                    resource_id=run_id,
                    action="ai_native_event",
                    payload=_safe_event(event),
                )
            )
            await session.commit()

    result: HarnessResult | None = None
    for round_number in range(MAX_CORRECTION_ROUNDS + 1):
        request = HarnessRequest(
            request_id=run_id if round_number == 0 else f"{run_id}:fix-{round_number}",
            actor=Actor(id=run.actor_id, role=run.actor_role),
            kind=run.kind,
            prompt=run.prompt if round_number == 0 else correction_prompt(run.kind, problems),
            context=RequestContext.model_validate(run.context),
            resume_from=task_id if round_number else None,
        )
        gateway = AiNativeGateway(request, sessions, read_workspace=read_workspace, check_role=check_role)
        try:
            result = await runner.run(request, gateway, emit, cancel)
        except Exception as exc:  # HarnessRejected and anything before the task started
            return await _finish(sessions, run_id, "failed", getattr(exc, "code", "not_started"), problems, usage, task_id)
        task_id = result.task_id or task_id
        for name in ("input_tokens", "output_tokens", "reasoning_tokens", "unknown_cost_calls"):
            setattr(usage, name, getattr(usage, name) + getattr(result.usage, name))
        usage.cost_usd += result.usage.cost_usd
        async with sessions() as session:
            record = await session.get(AiNativeRunRecord, run_id)
            record.task_id, record.correction_round = task_id, round_number
            await session.commit()
        if result.status != "completed":
            return await _finish(
                sessions, run_id, "failed", result.error_code or result.status, problems, usage, task_id
            )
        files, _ = await latest_exports(sessions, run_id)
        check = check_exports(run.kind, files, **await check_context(sessions, run))
        problems = check.problems
        if check.ok:
            return await _accept(sessions, run, files, usage, task_id, result)
    return await _finish(sessions, run_id, "failed", "export_rejected", problems, usage, task_id)


async def _finish(sessions, run_id, status, error_code, problems, usage, task_id, outcome=None) -> HarnessResult:
    async with sessions() as session:
        record = await session.get(AiNativeRunRecord, run_id)
        record.status, record.error_code = status, error_code
        record.problems, record.usage = list(problems), usage.model_dump()
        record.outcome = outcome or record.outcome or {}
        record.finished_at = now()
        await session.commit()
    _, artifact_ids = await latest_exports(sessions, run_id)
    return HarnessResult(
        status=status,
        task_id=task_id,
        error_code=error_code if status == "failed" else None,
        text=("; ".join(problems) or None) if status == "failed" else None,
        artifact_ids=artifact_ids if status == "completed" else [],
        proposal_ids=await proposal_ids(sessions, run_id),
        usage=usage,
    )


async def _accept(sessions, run, files, usage, task_id, result) -> HarnessResult:
    """The exports passed. Record the outcome and queue what follows."""
    outcome: dict[str, Any] = {}
    follow = run.follow_up or {}
    if run.kind == "survey":
        survey = json.loads(files["survey.json"])
        outcome["personal_data_suspected"] = survey["personal_data_suspected"]
        outcome["intake_run_ids"] = []
        if not survey["personal_data_suspected"] and follow.get("development_data_id"):
            from harness.handoff import intake_prompt

            for candidate in survey["candidates"]:
                outcome["intake_run_ids"].append(
                    await create_run(
                        sessions,
                        "intake",
                        Actor(id=run.actor_id, role=run.actor_role),
                        {
                            "policy_version": run.context["policy_version"],
                            "archive_id": run.context["archive_id"],
                            "candidate_id": candidate["candidate_id"],
                            "development_data_id": follow["development_data_id"],
                        },
                        prompt=(
                            intake_prompt(candidate)
                            + "\nThe platform treats every item in intake.json problems as an unresolved blocking defect at Gate 0. "
                            "Keep genuine unresolved execution, missing required scoring files, mapping and safety defects there. "
                            "Record unknown business metadata and absent optional historical evidence in the manifest field reasons instead; "
                            "a pre-existing manifest or schema is not required for an unstructured archive. "
                            "A missing original scorer that has been replaced by a successfully tested, exported wrapper is resolved, "
                            "so explain it in scoring.self_check rather than retaining it as an unresolved problem. "
                            "Never suppress a real blocker or claim a wrapper was tested unless its execution succeeded."
                        ),
                        source_run_id=run.id,
                    )
                )
    if run.kind == "triage":
        triage = json.loads(files["triage.json"])
        outcome["outcome"] = triage["outcome"]
        if triage["outcome"] == "problem" and follow.get("document_id") and follow.get("validation_report_id"):
            summary = await _input_path(sessions, run.context["monitoring_run_id"], "monitoring")
            monitoring = await register_input(
                sessions,
                "monitoring",
                {**_read_files(summary), "triage_report.md": files.get("triage_report.md", b"")},
                meta={"triage_run_id": run.id},
            )
            outcome["investigation_run_id"] = await create_run(
                sessions,
                "investigation",
                Actor(id=run.actor_id, role=run.actor_role),
                {
                    "policy_version": run.context["policy_version"],
                    "model_id": run.context.get("model_id"),
                    "monitoring_run_id": monitoring,
                    "development_data_id": run.context["development_data_id"],
                    "document_id": follow["document_id"],
                    "validation_report_id": follow["validation_report_id"],
                },
                source_run_id=run.id,
            )
    if run.kind == "intake":
        intake = json.loads(files["intake.json"])
        from harness.outputs import Intake, fields_needing_confirmation

        outcome["executable"] = intake["executable"]
        outcome["gate0_confirm"] = fields_needing_confirmation(Intake.model_validate(intake))
    if run.kind == "validation":
        import re

        match = re.search(r"^Recommendation:\s*(.+?)\s*$", files["validation_narrative.md"].decode(), re.MULTILINE)
        outcome["recommendation"] = match.group(1) if match else None
    return await _finish(sessions, run.id, "completed", None, [], usage, task_id, outcome)


async def recover_runs(sessions, older_than_s: float) -> int:
    """A run still `running` after the longest a run can take has lost its worker, so it fails
    clearly, as `interrupted`. Start it again with a new run."""
    from datetime import timedelta

    async with sessions() as session:
        changed = await session.execute(
            update(AiNativeRunRecord)
            .where(
                AiNativeRunRecord.status == "running",
                AiNativeRunRecord.started_at < now() - timedelta(seconds=older_than_s),
            )
            .values(status="failed", error_code="interrupted", finished_at=now())
        )
        await session.commit()
    return changed.rowcount
