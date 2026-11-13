"""The chat's request_run under AI-native: a saved proposal must reach the agent as saved."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings
from app.db.tables import ModelRecord
from app.services.proposal_service import ProposalRecord
from app.services.tool_gateway import BackendGateway
from harness.schemas import ToolResult as HarnessToolResult


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch, tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.db import session as database
    from app import main as entrypoint

    url = f"sqlite+aiosqlite:///{tmp_path / 'proposal-result.db'}"
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionFactory", factory)
    monkeypatch.setattr(entrypoint, "engine", engine)
    monkeypatch.setattr(entrypoint, "SessionFactory", factory)
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "ai_native_enabled", True)
    yield factory
    asyncio.run(engine.dispose())


def _gateway(sessions, model_id, conversation_id):
    request = SimpleNamespace(
        actor=SimpleNamespace(id="dev-1", role="developer"),
        context=SimpleNamespace(model_id=model_id, run_id=None, conversation_id=conversation_id),
    )
    return BackendGateway(request, sessions)


def _task(task_id):
    return SimpleNamespace(task_id=task_id)


def test_request_run_returns_a_result_the_harness_accepts_and_reuses_the_waiting_proposal(
    isolated_database,
):
    sessions = isolated_database
    with TestClient(app):

        async def scenario():
            async with sessions() as session:
                model = ModelRecord(
                    name="chat-proposal-" + uuid4().hex[:8],
                    version="1.0.0-r86e7e892-9cdf95ee",
                    language="Python",
                    dataset_id="uci_credit",
                    purpose="Test",
                )
                session.add(model)
                await session.commit()
            conversation = str(uuid4())
            gateway = _gateway(sessions, model.id, conversation)

            first = await gateway._propose_operation("validate", _task(str(uuid4())), "k:1:0")
            # The harness converts every backend result into its own type. Before the fix this
            # raised, and the agent was told the result was unknown although the proposal existed.
            converted = HarnessToolResult.model_validate(first.model_dump())
            assert converted.status == "ok" and converted.proposal_id
            data = json.loads(first.content)
            assert data["status"] == "pending"
            assert data["required_role"] == "validator"
            assert data["run_started"] is False
            assert data["model_version"] == "1.0.0-r86e7e892-9cdf95ee"
            assert data["already_waiting"] is False
            # No document has passed Gate 1, so validation could not start if executed now.
            assert any(b.startswith("Gate 1:") for b in data["known_blockers"])

            # A retry on a later turn has a new idempotency key. It must not add a proposal.
            again = await gateway._propose_operation("validate", _task(str(uuid4())), "k:2:0")
            assert again.proposal_id == first.proposal_id
            assert json.loads(again.content)["already_waiting"] is True

            # A different operation is different work.
            rebuild = await gateway._propose_operation("rebuild", _task(str(uuid4())), "k:3:0")
            assert rebuild.proposal_id != first.proposal_id
            assert json.loads(rebuild.content)["required_role"] == "developer"
            assert "Rebuild requires an R model." in json.loads(rebuild.content)["known_blockers"]

            # Another conversation gets its own card.
            other = _gateway(sessions, model.id, str(uuid4()))
            elsewhere = await other._propose_operation("validate", _task(str(uuid4())), "k:4:0")
            assert elsewhere.proposal_id != first.proposal_id

            async with sessions() as session:
                from sqlalchemy import select, func

                count = await session.scalar(
                    select(func.count()).select_from(ProposalRecord).where(
                        ProposalRecord.model_id == model.id
                    )
                )
            assert count == 3

        asyncio.run(scenario())


def test_request_run_errors_are_reported_not_left_unknown(isolated_database):
    sessions = isolated_database
    with TestClient(app):

        async def scenario():
            no_model = _gateway(sessions, None, str(uuid4()))
            result = await no_model._propose_operation("document", _task(str(uuid4())), "k")
            assert result.status == "error" and "No model is selected" in result.content

            missing = _gateway(sessions, str(uuid4()), str(uuid4()))
            result = await missing._propose_operation("document", _task(str(uuid4())), "k2")
            assert result.status == "error" and result.content.startswith("Proposal not recorded")

            monitor = await no_model._propose_operation("monitor", _task(str(uuid4())), "k3")
            assert monitor.status == "error" and "request_monitoring" in monitor.content

        asyncio.run(scenario())


def test_contract_result_rejects_the_shape_the_harness_rejects():
    from contracts.harness import ToolResult

    with pytest.raises(ValueError):
        ToolResult(status="needs_confirmation", content="x", pending_id="p", proposal_id="p")
    with pytest.raises(ValueError):
        ToolResult(status="needs_review", content="x")


def test_model_status_reports_recorded_stage_runs_checks_and_lookup_scope(isolated_database):
    from app.db.tables import ArtifactRecord, ReportRecord, runningRecord
    from app.services.model_status import find_models, model_status

    sessions = isolated_database
    with TestClient(app):

        async def scenario():
            async with sessions() as session:
                model = ModelRecord(
                    name="Status_Model_" + uuid4().hex[:6],
                    version="1.0.0-r86e7e892-9cdf95ee",
                    language="Python",
                    dataset_id="uci_credit",
                    purpose="Test",
                    lifecycle_status="development",
                )
                session.add(model)
                await session.flush()
                running = runningRecord(
                    model_id=model.id,
                    model_version=model.version,
                    dataset_id="uci_credit",
                    operation="rebuild",
                    status="running",
                    stage="thinking",
                )
                failed = runningRecord(
                    model_id=model.id,
                    model_version=model.version,
                    dataset_id="uci_credit",
                    operation="validate",
                    status="failed",
                    stage="failed",
                    error="AI validation run failed: turn_limit_exceeded",
                )
                revised = runningRecord(
                    model_id=model.id,
                    model_version=model.version,
                    dataset_id="uci_credit",
                    operation="document",
                    status="failed",
                    stage="changes_requested",
                )
                session.add_all([running, failed, revised])
                await session.flush()
                artifact = ArtifactRecord(
                    id=str(uuid4()),
                    model_id=model.id,
                    run_id=failed.id,
                    version="1",
                    filename="report.json",
                    media_type="application/json",
                    sha256="0" * 64,
                    size=1,
                )
                session.add(artifact)
                await session.flush()
                session.add(
                    ReportRecord(
                        id=str(uuid4()),
                        model_id=model.id,
                        run_id=failed.id,
                        kind="documentation",
                        artifact_id=artifact.id,
                        payload={"draft": True},
                    )
                )
                session.add(
                    ReportRecord(
                        id=str(uuid4()),
                        model_id=model.id,
                        run_id=failed.id,
                        kind="validation",
                        artifact_id=artifact.id,
                        payload={
                            "summary": {"overall_status": "VALIDATION_INCOMPLETE"},
                            "checks": [
                                {
                                    "check_id": "split_provenance",
                                    "status": "REVIEW",
                                    "required": True,
                                    "summary": "Uploaded split is verified; original fit "
                                    "membership remains unverified",
                                    "details": {"large": "not returned"},
                                },
                                {
                                    "check_id": "saved_metric_consistency",
                                    "status": "NOT_ASSESSABLE",
                                    "required": True,
                                    "summary": "Historical uploaded metrics checked separately",
                                },
                            ],
                        },
                    )
                )
                await session.commit()

                # A model-bound chat has no run ID; the active run must still be visible.
                bound = SimpleNamespace(model_id=model.id, run_id=None, conversation_id="c")
                data = await model_status(session, bound)
                assert data["model"]["lifecycle_status"] == "development"
                assert data["model"]["version"] == "1.0.0-r86e7e892-9cdf95ee"
                statuses = {r["id"]: (r["status"], r["stage"], r["error"]) for r in data["latest_runs"]}
                assert statuses[running.id] == ("running", "thinking", None)
                assert "turn_limit_exceeded" in statuses[failed.id][2]
                revision = {r["id"]: r["revision_requested"] for r in data["latest_runs"]}
                assert revision == {running.id: False, failed.id: False, revised.id: True}
                validation = next(r for r in data["reports"] if r["kind"] == "validation")
                assert validation["checks"][0] == {
                    "check_id": "split_provenance",
                    "status": "REVIEW",
                    "required": True,
                    "summary": "Uploaded split is verified; original fit membership remains "
                    "unverified",
                }
                assert validation["checks"][1]["status"] == "NOT_ASSESSABLE"
                assert "NOT_ASSESSABLE means the evidence" in data["reading_rules"]

                exact = await model_status(session, bound, run_id=running.id)
                assert exact["requested_run"]["status"] == "running"

                empty = SimpleNamespace(model_id=None, run_id=None, conversation_id=None)
                assert (await model_status(session, empty))["model_lookup"] == "not_selected"
                assert (await model_status(session, empty, model_id="missing"))[
                    "model_lookup"
                ] == "not_found"
                unknown_run = await model_status(session, bound, run_id="missing-run")
                assert unknown_run["requested_run"] == {"id": "missing-run", "found": False}
                other = await model_status(session, empty, model_id=model.id)
                assert other["scope"]["looked_up_model"] is True

                found = await find_models(session, model.name.lower())
                assert found["result"] == "one_match"
                assert found["models"][0]["id"] == model.id
                assert (await find_models(session, "no-such-model-name"))["result"] == "not_found"

        asyncio.run(scenario())


def test_model_status_links_revisions_supersedes_old_drafts_and_splits_gate_3_blockers(
    isolated_database,
):
    """Flow C 2026-11-10: chat linked an independent regeneration to a changes request,
    treated a superseded draft as a Gate 1 prerequisite and offered audit-only columns."""
    from app.db.tables import ArtifactRecord, AuditRecord, ReportRecord, ReviewRecord, runningRecord
    from app.services.model_status import model_status
    from app.services.upload_service import DatasetRecord, InventoryRecord, UploadRecord

    sessions = isolated_database
    with TestClient(app):

        async def scenario():
            async with sessions() as session:
                model = ModelRecord(name="links_" + uuid4().hex[:6], version="1.0.0",
                                    language="R", dataset_id="uci_credit", purpose="Test")
                session.add(model)
                await session.flush()

                def run(operation, status, stage=None):
                    return runningRecord(model_id=model.id, model_version="1.0.0",
                                         dataset_id="uci_credit", operation=operation,
                                         status=status, stage=stage)

                original = run("document", "failed", "changes_requested")
                regeneration = run("document", "awaiting_review")
                follow_up = run("document", "completed", "accepted")
                validation = run("validate", "awaiting_review")
                session.add_all([original, regeneration, follow_up, validation])
                await session.flush()
                artifact = ArtifactRecord(id=str(uuid4()), model_id=model.id, run_id=original.id,
                                          version="1", filename="d.md", media_type="text/markdown",
                                          sha256="0" * 64, size=1)
                dataset = DatasetRecord(id=str(uuid4()), dataset_id="uci_credit", version="1",
                                        sha256="1" * 64, split_sha256="2" * 64,
                                        descriptor={"target_column": "default payment next month",
                                                    "audit_only_columns": ["SEX", "AGE"]},
                                        columns=[], statistics={})
                upload = UploadRecord(id=str(uuid4()), actor_id="dev", archive_sha256="3" * 64)
                session.add_all([artifact, dataset, upload])
                await session.flush()
                session.add(InventoryRecord(
                    model_id=model.id, name=model.name, model_version="1.0.0", upload_id=upload.id,
                    dataset_version_id=dataset.id, manifest={"feature_order": ["LIMIT_BAL", "PAY_0"]},
                    provenance={}, bundle_sha256="4" * 64, document_run_id=follow_up.id))

                def review(target, **fields):
                    return ReviewRecord(id=str(uuid4()), run_id=target.id, artifact_id=artifact.id,
                                        artifact_version="1", requested_by="dev", **fields)

                sent_back = review(original, status="decided", decision="changes_requested")
                stale = review(regeneration)
                accepted = review(follow_up, status="decided", decision="accepted")
                gate_3 = review(validation,
                                blockers=["reject_duplicated_row_id", "split_provenance"])
                session.add_all([sent_back, stale, accepted, gate_3])
                session.add(AuditRecord(actor_id="validator", resource_id=follow_up.id,
                                        action="document_revision_requested",
                                        payload={"review_id": sent_back.id}))
                session.add(ReportRecord(
                    id=str(uuid4()), model_id=model.id, run_id=validation.id, kind="validation",
                    artifact_id=artifact.id,
                    payload={"checks": [
                        {"check_id": "reject_duplicated_row_id", "status": "FAIL", "required": True},
                        {"check_id": "split_provenance", "status": "REVIEW", "required": True},
                    ]}))
                await session.commit()

                data = await model_status(
                    session, SimpleNamespace(model_id=model.id, run_id=None, conversation_id="c"))
                runs = {r["id"]: r for r in data["latest_runs"]}
                assert runs[follow_up.id]["revision_of"] == {
                    "review_id": sent_back.id, "reviewed_run_id": original.id}
                assert runs[original.id]["continued_by_run_id"] == follow_up.id
                assert "revision_of" not in runs[regeneration.id]
                assert data["gate_1"] == {"model_version": "1.0.0",
                                          "current_document_run_id": follow_up.id,
                                          "accepted": True}
                pending = {r["run_id"]: r for r in data["pending_reviews"]}
                assert pending[regeneration.id]["superseded"] is True
                assert pending[validation.id]["hard_blockers"] == ["reject_duplicated_row_id"]
                assert pending[validation.id]["limitations"] == ["split_provenance"]
                assert pending[validation.id]["allowed_decisions"] == ["changes_requested"]
                assert data["model_inputs"]["feature_order"] == ["LIMIT_BAL", "PAY_0"]
                assert data["model_inputs"]["audit_only_columns"] == ["SEX", "AGE"]
                assert "superseded" in data["reading_rules"]

        asyncio.run(scenario())
