"""Trusted backend tools shared by agent tasks. No tool can grant a human decision."""

import json
import re
from uuid import uuid4
from sqlalchemy import select
from contracts.harness import ToolSpec, ToolResult
from harness.sandbox import safe_relative_path
from app.db.tables import (
    PackageRecord,
    runningRecord,
    ArtifactRecord,
    ReportRecord,
    ReviewRecord,
    AuditRecord,
    ModelRecord,
)
from app.services.artifact_service import save_artifact
from app.services.legacy_scoring import ScoringError, docker
from app.services.package_service import PackageError


def spec(name, description, risk, properties=None):
    fields = properties or {}
    return ToolSpec(
        name=name,
        description=description,
        risk=risk,
        parameters={
            "type": "object",
            "properties": fields,
            "required": list(fields),
            "additionalProperties": False,
        },
    )


TOOLS = [
    spec(
        "read_template",
        "Read the authoritative nine-section model document template.",
        "read",
    ),
    spec(
        "request_run",
        "Propose a model operation; only an authenticated human confirmation starts it.",
        "write",
        {
            "operation": {
                "type": "string",
                "enum": ["document", "rebuild", "validate", "monitor"],
            }
        },
    ),
    spec(
        "request_monitoring",
        "Propose a declared monitoring simulation for human confirmation.",
        "write",
        {
            "feature": {"type": "string"},
            "factor": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
            "window_label": {"type": "string", "minLength": 1, "maxLength": 120},
        },
    ),
    ToolSpec(
        name="get_model_status",
        description=(
            "Read a model's recorded status: lifecycle stage, full version, latest runs with "
            "their status, stage and error, pending review gates, proposals and their results, "
            "latest reports with each check's status and summary, the latest official parity "
            "and aggregate fresh validation/test metrics when saved, plus the upload reproduction. "
            "Never treat a report metric as human approval. With no arguments it reads the "
            "conversation's model "
            "(and run). Pass model_id to read another model, or run_id to read one run exactly."
        ),
        risk="read",
        parameters={
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "minLength": 1, "maxLength": 36},
                "run_id": {"type": "string", "minLength": 1, "maxLength": 36},
            },
            "required": [],
            "additionalProperties": False,
        },
    ),
    spec(
        "find_models",
        "Find models by name, ignoring case. Returns each match's ID, full version and "
        "lifecycle stage, or says that none or several match.",
        "read",
        {"query": {"type": "string", "minLength": 1, "maxLength": 120}},
    ),
    spec(
        "get_datasets",
        "Read the platform's datasets: name, source file, target, known limitations and, "
        "for each uploaded version, its source, licence, columns, target column, positive "
        "class, audit-only columns, row count and train/validation/test sizes. A dataset "
        "with no uploaded versions is known to the platform but not uploaded yet.",
        "read",
    ),
    spec(
        "export_artifact",
        "Export a completed Markdown document or the self-contained candidate directory.",
        "write",
        {"path": {"type": "string"}},
    ),
    spec(
        "run_parity",
        "Run the platform's official parity check on the candidate you last exported. "
        "It is the same check a human runs at Gate 2: the candidate is scored in a fresh "
        "container and each development row is compared with the legacy model's reference "
        "predictions at a fixed absolute tolerance. Returns the result and the worst rows, "
        "plus input_contract: whether the candidate scores clean rows and rejects a missing "
        "row_id, a duplicated row_id and an extra target column, as validation will check.",
        "write",
    ),
    spec(
        "request_review",
        "Pause for human review of the latest exported artifact. Gate 2 approval of a rebuild requires a passing official parity run on that exact candidate.",
        "review",
        {"summary": {"type": "string"}},
    ),
]

CHAT_TOOLS = (
    "get_model_status",
    "find_models",
    "get_datasets",
    "request_run",
    "request_monitoring",
    "read_template",
)


def tools_for(kind):
    """Chat proposes work; tasks act. Only rebuild tasks can run parity."""
    if kind == "chat":
        return [s for s in TOOLS if s.name in CHAT_TOOLS]
    hidden = {"request_run", "request_monitoring"}
    if kind != "rebuild":
        hidden.add("run_parity")
    return [s for s in TOOLS if s.name not in hidden]


class Platform:
    def __init__(self, task, context, sessions):
        self.task, self.context, self.sessions = task, context, sessions

    async def list_tools(self):
        return tools_for(self.task.kind)

    async def call(self, name, args, idempotency_key):
        if self.task.actor_role not in (
            "operator",
            "reviewer",
            "developer",
            "validator",
            "approver",
            "admin",
        ):
            return ToolResult(status="denied", content="Role cannot use backend tools")
        if name not in {s.name for s in await self.list_tools()}:
            return ToolResult(status="denied", content="Tool not permitted")
        from jsonschema import validate

        try:
            validate(args, next(s.parameters for s in TOOLS if s.name == name))
        except Exception:
            return ToolResult(status="denied", content="Invalid tool arguments")
        async with self.sessions() as session:
            prior = await session.scalar(
                select(AuditRecord).where(
                    AuditRecord.resource_id == idempotency_key,
                    AuditRecord.actor_id == self.task.actor_id,
                    AuditRecord.action == "tool_result",
                )
            )
            if prior:
                return ToolResult.model_validate(prior.payload)
        try:
            result = await self._call(name, args)
        except Exception:
            result = ToolResult(
                status="error",
                content="Backend tool failed; verify package, artifact and runtime.",
            )
        async with self.sessions() as session:
            session.add(
                AuditRecord(
                    actor_id=self.task.actor_id,
                    resource_id=idempotency_key,
                    action="tool_result",
                    payload=result.model_dump(),
                )
            )
            await session.commit()
        return result

    async def _call(self, name, args):
        if name == "read_template":
            from harness.document_template import DOCUMENT_TEMPLATE

            return ToolResult(status="ok", content=DOCUMENT_TEMPLATE)
        if name == "get_datasets":
            from app.services.dataset_catalog import describe_datasets

            async with self.sessions() as session:
                data = await describe_datasets(session)
            return ToolResult(status="ok", content=json.dumps(data))
        async with self.sessions() as session:
            run = (
                await session.get(runningRecord, self.context.run_id)
                if self.context.run_id
                else None
            )
            package = (
                await session.get(PackageRecord, self.task.model_package_id)
                if self.task.model_package_id
                else None
            )
            if name == "get_model_status":
                from app.services.model_status import model_status

                data = await model_status(
                    session, self.context, args.get("model_id"), args.get("run_id")
                )
                await session.commit()
                return ToolResult(status="ok", content=json.dumps(data, default=str))
            if name == "find_models":
                from app.services.model_status import find_models

                data = await find_models(session, args["query"])
                return ToolResult(status="ok", content=json.dumps(data, default=str))
            if (
                not run
                or run.status != "running"
                or not package
                or package.model_id != run.model_id
            ):
                return ToolResult(
                    status="denied", content="No matching bound model/run/package"
                )
            if name == "run_parity":
                return await self._parity(run)
            if name == "export_artifact":
                relative = safe_relative_path(args["path"])
                if self.task.kind == "rebuild":
                    from app.services.candidate_service import export_candidate

                    return await export_candidate(
                        self.task, run, package, relative, self.sessions
                    )
                if not relative.endswith(".md"):
                    return ToolResult(
                        status="denied",
                        content="Documentation export requires Markdown",
                    )
                if not self.task.workspace_id or not re.fullmatch(
                    r"[a-zA-Z0-9-]+", self.task.task_id
                ):
                    return ToolResult(status="denied", content="Invalid workspace")
                code = (
                    "import pathlib,sys; p=pathlib.Path('/workspace')/sys.argv[1]; "
                    "assert not p.is_symlink() and p.resolve().is_relative_to('/workspace'); "
                    "assert p.is_file() and p.stat().st_size<=2000000; sys.stdout.buffer.write(p.read_bytes())"
                )
                raw = await docker(
                    "exec",
                    "mgr-task-" + self.task.task_id,
                    "python",
                    "-c",
                    code,
                    relative,
                )
                from harness.document_template import document_problems

                problems = document_problems(raw.decode("utf-8"))
                if problems:
                    return ToolResult(
                        status="error",
                        content="Document template checks failed: "
                        + "; ".join(problems),
                    )
                artifact = await save_artifact(
                    session, run, raw, "model-document.md", "text/markdown"
                )
                if self.task.kind == "document":
                    session.add(
                        ReportRecord(
                            id=str(uuid4()),
                            model_id=run.model_id,
                            run_id=run.id,
                            kind="documentation",
                            artifact_id=artifact.id,
                            payload={
                                "generated_by": "agent",
                                "package_sha256": package.sha256,
                                "draft": True,
                                "human_approval_granted": False,
                            },
                        )
                    )
                await session.commit()
                return ToolResult(
                    status="ok",
                    content=json.dumps(
                        {
                            "artifact_id": artifact.id,
                            "sha256": artifact.sha256,
                            "version": artifact.version,
                        }
                    ),
                )
            if name == "request_review":
                query = (
                    select(ArtifactRecord)
                    .where(
                        ArtifactRecord.run_id == run.id,
                        ArtifactRecord.media_type
                        == (
                            "application/zip"
                            if self.task.kind == "rebuild"
                            else "text/markdown"
                        ),
                    )
                    .order_by(ArtifactRecord.created_at.desc())
                )
                artifact = await session.scalar(query)
                if not artifact:
                    return ToolResult(
                        status="denied", content="Export an artifact first"
                    )
                review = ReviewRecord(
                    id=str(uuid4()),
                    run_id=run.id,
                    artifact_id=artifact.id,
                    artifact_version=artifact.version,
                    requested_by=self.task.actor_id,
                    # Gate 2 checks official parity when the decision is made.
                    blockers=[],
                )
                session.add(review)
                await session.commit()
                return ToolResult(
                    status="needs_review",
                    content="Human review required; no approval granted.",
                    pending_id=review.id,
                )
        return ToolResult(status="denied", content="Unknown tool")

    async def _parity(self, run):
        from app.services.parity_service import (
            ParityError,
            latest_candidate,
            parity_read,
            run_parity,
        )

        async with self.sessions() as session:
            candidate = await latest_candidate(session, run.id)
        if not candidate:
            return ToolResult(
                status="denied", content="Export the candidate directory first"
            )
        try:
            record = await run_parity(
                self.sessions,
                candidate,
                initiator="agent",
                actor_id=self.task.actor_id,
                rebuild_run_id=run.id,
            )
        except ParityError as exc:
            return ToolResult(status="error", content=str(exc))
        result = parity_read(record)
        result["input_contract"] = await self._input_contract(candidate)
        if "error" not in result["input_contract"]:
            from app.services.input_contract import save_evidence

            await save_evidence(self.sessions, record.id, result["input_contract"])
        return ToolResult(status="ok", content=json.dumps(result, default=str))

    async def _input_contract(self, candidate):
        """Malformed-input probes on the exported candidate, the same kinds validation runs.
        Reported next to parity so the agent fixes a scorer that accepts bad input before
        Gate 2; they do not change the parity result."""
        from app.services.input_contract import input_contract_checks
        from app.services.parity_service import ParityError, _lineage

        try:
            inventory, _, package, _, _ = await _lineage(self.sessions, candidate)
            return await input_contract_checks(package, inventory.manifest.get("dataset_id"))
        except (ParityError, ScoringError, PackageError, OSError) as exc:
            return {"passed": False, "error": "Input-contract probes could not run: " + str(exc)[:300]}


class AuditedGateway:
    def __init__(self, inner, task, sessions):
        self.inner, self.task, self.sessions = inner, task, sessions

    async def list_tools(self):
        return await self.inner.list_tools()

    async def call(self, name, args, idempotency_key):
        async with self.sessions() as session:
            session.add(
                AuditRecord(
                    actor_id=self.task.actor_id,
                    resource_id=self.task.task_id,
                    action="tool_call",
                    payload={"name": name, "args": args, "key": idempotency_key},
                )
            )
            await session.commit()
        result = await self.inner.call(name, args, idempotency_key)
        async with self.sessions() as session:
            session.add(
                AuditRecord(
                    actor_id=self.task.actor_id,
                    resource_id=self.task.task_id,
                    action="tool_output",
                    payload={"name": name, "result": result.model_dump()},
                )
            )
            await session.commit()
        return result


class BackendGateway:
    """Contract gateway bound to server-selected identity and context, never agent arguments."""

    def __init__(self, request, sessions):
        self.request, self.sessions = request, sessions

    async def list_tools(self):
        tools = tools_for(self.request.kind)
        from app.config import settings
        # Chat only: the AI-native kinds propose through ai_native_service's gateway.
        if settings.ai_native_enabled and self.request.context.model_id and self.request.kind == "chat":
            from harness.proposals import ACTIONS, proposal_tool_spec

            spec = proposal_tool_spec(tuple(ACTIONS))
            tools = [*tools, ToolSpec.model_validate(spec.model_dump())]
        return tools

    async def call(self, name, args, idempotency_key):
        from app.services.harness_adapter import task_info_for_request

        if name not in {s.name for s in await self.list_tools()}:
            return ToolResult(status="denied", content="Tool not permitted")
        from app.services.harness_adapter import actor_role

        try:
            role = await actor_role(self.sessions, self.request.actor.id)
        except Exception:
            return ToolResult(
                status="denied", content="Actor is disabled or unavailable"
            )
        if role != self.request.actor.role:
            return ToolResult(
                status="denied", content="Actor role changed; start a new request"
            )
        task = await task_info_for_request(self.sessions, self.request)
        from app.config import settings
        if name == "request_run" and settings.ai_native_enabled:
            return await self._propose_operation(args.get("operation"), task, idempotency_key)
        if name == "create_proposal":
            from app.services import proposal_service
            from pydantic import ValidationError
            from fastapi import HTTPException

            from harness.proposals import ACTIONS, proposal_problems

            problems = proposal_problems(tuple(ACTIONS), args)
            if problems:
                return ToolResult(status="error", content="Proposal not recorded: " + "; ".join(problems))
            try:
                draft = proposal_service.Draft.model_validate(args)
                async with self.sessions() as session:
                    record = await proposal_service.create(session, self.request.context.run_id, draft,
                                                           self.request.actor.id, idempotency_key,
                                                           model_id=self.request.context.model_id, task_id=task.task_id,
                                                           conversation_id=self.request.context.conversation_id)
                    await session.commit()
                    return ToolResult(status="ok", content="Proposal saved for human review: " + record.id,
                                      proposal_id=record.id)
            except (ValidationError, HTTPException):
                return ToolResult(status="error", content="Invalid proposal arguments or context")
        if name == "request_monitoring":
            from app.services.chat_service import request_action

            return await request_action(
                self.sessions, self.request, "monitor", idempotency_key, args
            )
        if name == "request_run":
            from app.services.chat_service import request_action

            return await request_action(
                self.sessions, self.request, args.get("operation"), idempotency_key
            )
        platform = Platform(task, self.request.context, self.sessions)
        return await AuditedGateway(platform, task, self.sessions).call(
            name, args, idempotency_key
        )

    async def _propose_operation(self, operation, task, idempotency_key):
        """Record a start_operation proposal, or return the one already waiting for the same work.

        The result is `ok` with the proposal ID, like `create_proposal`: the proposal is saved,
        and the chat shows its card. Nothing starts until an eligible person approves it and
        then executes it as a separate step.
        """
        from fastapi import HTTPException
        from app.services import proposal_service
        from app.services.proposal_service import ProposalRecord

        if operation == "monitor":
            return ToolResult(
                status="error",
                content="Monitoring needs a feature, factor and window: use request_monitoring.",
            )
        context = self.request.context
        if not context.model_id:
            return ToolResult(
                status="error",
                content="No model is selected in this conversation, so no proposal was recorded.",
            )
        reused = None
        try:
            async with self.sessions() as session:
                model = await session.get(ModelRecord, context.model_id)
                if model is not None:
                    waiting = await session.scalars(
                        select(ProposalRecord)
                        .where(
                            ProposalRecord.model_id == model.id,
                            ProposalRecord.type == "start_operation",
                            ProposalRecord.status.in_(("pending", "approved")),
                            ProposalRecord.conversation_id == context.conversation_id,
                        )
                        .order_by(ProposalRecord.created_at.desc())
                    )
                    for candidate in list(waiting):
                        if candidate.args.get("operation") != operation:
                            continue
                        await proposal_service.reconcile(session, candidate)
                        if candidate.status in ("pending", "approved"):
                            reused = candidate
                            break
                    await session.commit()
                record = reused or await proposal_service.create(
                    session,
                    context.run_id,
                    {
                        "type": "start_operation",
                        "args": {"operation": operation},
                        "rationale": "Requested through the contextual assistant",
                        "evidence": [{"kind": "file", "ref": "model:" + str(context.model_id)}],
                    },
                    self.request.actor.id,
                    idempotency_key,
                    model_id=context.model_id,
                    task_id=task.task_id,
                    conversation_id=context.conversation_id,
                )
                await session.commit()
                model = await session.get(ModelRecord, record.model_id)
                data = proposal_result(record, model, reused=reused is not None)
                data["known_blockers"] = await operation_blockers(session, model, operation)
        except HTTPException as exc:
            return ToolResult(status="error", content=f"Proposal not recorded: {exc.detail}")
        return ToolResult(status="ok", content=json.dumps(data), proposal_id=record.id)


async def operation_blockers(session, model, operation):
    """What would stop the operation from starting if it were executed now.

    The platform checks again at execution; this only tells the user before they approve.
    """
    from app.services.run_service import gate_1_accepted
    from app.services.upload_service import InventoryRecord

    if model is None:
        return []
    blockers = []
    if operation == "rebuild" and model.language != "R":
        blockers.append("Rebuild requires an R model.")
    if operation in ("rebuild", "validate"):
        inventory = await session.get(InventoryRecord, model.id)
        if not await gate_1_accepted(session, model, inventory):
            blockers.append(
                f"Gate 1: version {model.version} has no accepted model document yet. Its "
                "document review must be accepted before this operation can run; a rebuilt "
                "candidate needs its own."
            )
    return blockers


def proposal_result(record, model, *, reused=False):
    """What the assistant must tell the user about a recorded operation proposal."""
    status = record.status
    if status == "pending":
        next_step = (
            f"Nothing has started. A user with the {record.required_role} role (or an admin) "
            "approves or rejects the proposal card in this chat. Approval does not start the "
            "run: the approved card must then be executed as a separate step."
        )
    else:
        next_step = (
            "Nothing has started. The proposal is already approved: a user with the "
            f"{record.required_role} role (or an admin) executes it from its card in this chat."
        )
    return {
        "proposal_id": record.id,
        "type": record.type,
        "operation": record.args.get("operation"),
        "status": status,
        "model_id": record.model_id,
        "model_name": model.name if model else None,
        "model_version": record.preconditions.get("model_version"),
        "required_role": record.required_role,
        "already_waiting": reused,
        "run_started": False,
        "next_step": next_step,
    }
