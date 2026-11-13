"""Backend safety boundary checks; no network, model deserialisation or LLM calls."""

import asyncio
import io
import stat
import zipfile
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.main import app
from app.db.session import SessionFactory
from app.db.tables import ModelRecord, runningRecord, MonitoringRecord
from app.services import proposal_service as proposals, intake_service as intake
from app.services.identity import Actor


@pytest.fixture(autouse=True)
def isolated_native_database(monkeypatch, tmp_path):
    import sys
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.db import session as database
    from app import main as entrypoint
    from app.config import settings

    url = f"sqlite+aiosqlite:///{tmp_path / 'native-test.db'}"
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionFactory", factory)
    monkeypatch.setattr(entrypoint, "engine", engine)
    monkeypatch.setattr(entrypoint, "SessionFactory", factory)
    monkeypatch.setattr(sys.modules[__name__], "SessionFactory", factory)
    monkeypatch.setattr(settings, "database_url", url)
    yield
    asyncio.run(engine.dispose())


def test_archive_rejects_traversal_symlinks_duplicates_and_bombs(tmp_path):
    for index, names in enumerate((["../escape"], ["/absolute"], ["x", "x"])):
        path = tmp_path / f"bad{index}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name in names:
                archive.writestr(name, b"untrusted")
        with pytest.raises(ValueError):
            intake.unpack(path, tmp_path / str(index))
    path = tmp_path / "link.zip"
    with zipfile.ZipFile(path, "w") as archive:
        item = zipfile.ZipInfo("link")
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(item, "target")
    with pytest.raises(ValueError):
        intake.unpack(path, tmp_path / "symlink")
    path = tmp_path / "large.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("model.pkl", b"x" * 100)
    with pytest.raises(ValueError):
        intake.unpack(path, tmp_path / "large", max_bytes=20)


def test_rebuilt_scorer_rejects_bad_inputs_before_calling_candidate(tmp_path):
    import subprocess
    import sys
    from app.services.candidate_service import guard_candidate_scorer

    (tmp_path / "manifest.json").write_text('{"feature_order": ["amount"]}')
    (tmp_path / "score.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[sys.argv.index('--output') + 1]).write_text('scored')\n"
    )
    guard_candidate_scorer(tmp_path)
    source = tmp_path / "input.csv"
    output = tmp_path / "output.csv"
    command = [
        sys.executable, str(tmp_path / "score.py"),
        "--input", str(source), "--output", str(output),
    ]
    for malformed in (
        "row_id,amount\na,10\na,20\n",
        "row_id,amount,default payment next month\na,10,0\n",
        "amount\n10\n",
        "row_id,amount\na\n",
    ):
        source.write_text(malformed)
        result = subprocess.run(command, capture_output=True)
        assert result.returncode == 2, result.stderr.decode()
        assert not output.exists()
    source.write_text("row_id,amount\na,10\nb,20\n")
    subprocess.run(command, check=True)
    assert output.read_text() == "scored"


def test_archive_without_manifest_and_shared_nested_budget(tmp_path):
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as archive:
        archive.writestr("model.pkl", b"never loaded")
    path = tmp_path / "models.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("nested.zip", nested.getvalue())
    files = intake.unpack(path, tmp_path / "safe")
    assert {f["path"] for f in files} == {"nested.zip", "nested.zip.unpacked/model.pkl"}
    with pytest.raises(ValueError):
        intake.unpack(path, tmp_path / "limited", max_files=1)


def test_intake_wrapper_download_checks_the_reviewed_folder_hash(monkeypatch, tmp_path):
    from app.config import settings
    from app.services.package_service import digest

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    identity = str(uuid4())
    folder = intake.root(identity) / "wrappers" / "candidate"
    folder.mkdir(parents=True)
    (folder / "score.py").write_bytes(b"# reviewed scorer\n")
    (folder / "helpers.py").write_bytes(b"# reviewed helper\n")
    hashes = {name: digest(folder / name) for name in ("helpers.py", "score.py")}
    with TestClient(app) as client:
        async def seed():
            async with SessionFactory() as session:
                session.add(intake.ArchiveRecord(
                    id=identity, actor_id="test-developer", sha256="a" * 64,
                    status="awaiting_review", files=[], plan={"candidates": [{
                        "candidate_id": "candidate", "scoring": {
                            "wrapper": True, "wrapper_files": sorted(hashes),
                            "wrapper_sha256": intake.plan_hash(hashes),
                        },
                    }]},
                ))
                await session.commit()

        asyncio.run(seed())
        url = f"/api/v1/intake/archives/{identity}/file?candidate_id=candidate&path=score.py"
        response = client.get(url)
        assert response.status_code == 200, response.text
        assert response.content == b"# reviewed scorer\n"
        # Changing a helper must also invalidate the reviewed wrapper download.
        (folder / "helpers.py").write_bytes(b"# changed after review\n")
        assert client.get(url).status_code == 409


def test_proposal_role_staleness_idempotency_and_execution():
    with TestClient(app):

        async def scenario():
            async with SessionFactory() as session:
                model = ModelRecord(
                    name="proposal-" + uuid4().hex,
                    version="1",
                    language="R",
                    dataset_id="uci_credit",
                    purpose="Test",
                )
                session.add(model)
                await session.flush()
                run = runningRecord(
                    model_id=model.id,
                    model_version="1",
                    dataset_id="uci_credit",
                    operation="monitor",
                    status="completed",
                )
                session.add(run)
                await session.flush()
                event = MonitoringRecord(
                    id=str(uuid4()),
                    model_id=model.id,
                    run_id=run.id,
                    payload={"band": "amber"},
                )
                session.add(event)
                await session.commit()
                draft = {
                    "type": "mark_drift_benign",
                    "args": {"reason": "Small batch"},
                    "rationale": "Review batch size",
                    "evidence": [{"kind": "monitoring_run", "ref": run.id}],
                }
                record = await proposals.create(
                    session, run.id, draft, "agent", "key-" + run.id
                )
                duplicate = await proposals.create(
                    session, run.id, draft, "agent", "key-" + run.id
                )
                assert duplicate.id == record.id
                await session.commit()
                decision = proposals.Decision(
                    decision="approved", rationale="Reviewed the evidence"
                )
                with pytest.raises(HTTPException) as error:
                    await proposals.decide(
                        session, record.id, decision, Actor("developer", "developer")
                    )
                assert error.value.status_code == 403
                await proposals.decide(
                    session, record.id, decision, Actor("reviewer", "validator")
                )
                await proposals.execute(
                    session, record.id, Actor("reviewer", "validator")
                )
                await session.refresh(event)
                assert event.payload["benign_review"]["proposal_id"] == record.id
                with pytest.raises(HTTPException) as error:
                    await proposals.execute(
                        session, record.id, Actor("reviewer", "validator")
                    )
                assert error.value.status_code == 409
                await session.rollback()
                await session.refresh(run)
                await session.refresh(model)
                second = await proposals.create(session, run.id, draft, "agent")
                await session.commit()
                model.version = "2"
                await session.commit()
                with pytest.raises(HTTPException) as error:
                    await proposals.decide(
                        session, second.id, decision, Actor("reviewer", "validator")
                    )
                assert error.value.status_code == 409
                await session.refresh(second)
                assert second.status == "superseded"
                model.version = "1"
                await session.commit()
                third = await proposals.create(session, run.id, draft, "agent")
                await session.commit()
                await proposals.decide(
                    session, third.id, decision, Actor("admin-1", "admin")
                )
                await session.refresh(third)
                assert third.status == "approved"

        asyncio.run(scenario())


@pytest.mark.parametrize("analysis_kind", ["triage", "investigation"])
def test_new_monitoring_only_supersedes_triage_or_investigation_proposals(analysis_kind):
    from datetime import datetime, timedelta, timezone
    from app.services.native_analysis import AnalysisRecord

    with TestClient(app):

        async def scenario():
            async with SessionFactory() as session:
                model = ModelRecord(
                    name="proposal-freshness-" + uuid4().hex,
                    version="1", language="R", dataset_id="uci_credit", purpose="Test",
                )
                session.add(model)
                await session.flush()
                validation = runningRecord(
                    model_id=model.id, model_version="1", dataset_id="uci_credit",
                    operation="validate", status="completed",
                )
                triage = runningRecord(
                    model_id=model.id, model_version="1", dataset_id="uci_credit",
                    operation="monitor", status="completed",
                )
                session.add_all((validation, triage))
                await session.flush()
                session.add(AnalysisRecord(run_id=triage.id, kind=analysis_kind, inputs={}))
                now = datetime.now(timezone.utc)
                first = MonitoringRecord(
                    id=str(uuid4()), model_id=model.id, run_id=triage.id,
                    created_at=now, payload={"band": "amber"},
                )
                session.add(first)
                await session.flush()
                finding = await proposals.create(session, validation.id, {
                    "type": "open_finding",
                    "args": {
                        "title": "Review calibration", "severity": "medium",
                        "severity_rationale": "Drift in one segment", "impact": "Potential error",
                        "remediation": "Investigate", "check_ids": ["calibration"],
                    },
                    "rationale": "Validation evidence needs follow-up",
                    "evidence": [{"kind": "check", "ref": "calibration"}],
                }, "agent")
                drift = await proposals.create(session, triage.id, {
                    "type": "mark_drift_benign", "args": {"reason": "Seasonal mix"},
                    "rationale": "Investigate the amber window",
                    "evidence": [{"kind": "monitoring_run", "ref": triage.id}],
                }, "agent")
                await session.commit()
                session.add(MonitoringRecord(
                    id=str(uuid4()), model_id=model.id, run_id=triage.id,
                    created_at=now + timedelta(seconds=1), payload={"band": "red"},
                ))
                await session.commit()

                await proposals.reconcile(session, finding)
                await proposals.reconcile(session, drift)
                assert finding.status == "pending"
                assert drift.status == "superseded"
                await proposals.decide(
                    session, finding.id,
                    proposals.Decision(decision="rejected", rationale="Needs more evidence"),
                    Actor("reviewer", "validator"),
                )
                assert finding.status == "rejected"

        asyncio.run(scenario())


def test_monitoring_moves_lifecycle_on_breach_and_after_approval():
    from app.services.monitoring_service import lifecycle_after_monitoring

    assert lifecycle_after_monitoring("approved", False) == "monitored"
    assert lifecycle_after_monitoring("monitored", False) == "monitored"
    assert lifecycle_after_monitoring("approved", True) == "revalidation_due"
    assert lifecycle_after_monitoring("monitored", True) == "revalidation_due"
    # Monitoring never signs a model off: only an approved model becomes monitored.
    assert lifecycle_after_monitoring("validated", False) == "validated"
    assert lifecycle_after_monitoring("revalidation_due", False) == "revalidation_due"


def test_executed_revalidation_queues_validation_and_marks_model_due(monkeypatch):
    from app.services import run_service

    monkeypatch.setattr(run_service, "publish_run", lambda run_id: None)
    with TestClient(app):

        async def scenario():
            async with SessionFactory() as session:
                model = ModelRecord(
                    name="revalidation-" + uuid4().hex,
                    version="1",
                    language="Python",
                    dataset_id="uci_credit",
                    purpose="Test",
                    lifecycle_status="monitored",
                )
                session.add(model)
                await session.flush()
                run = runningRecord(
                    model_id=model.id,
                    model_version="1",
                    dataset_id="uci_credit",
                    operation="monitor",
                    status="completed",
                )
                session.add(run)
                await session.flush()
                session.add(
                    MonitoringRecord(
                        id=str(uuid4()),
                        model_id=model.id,
                        run_id=run.id,
                        payload={"trigger_band": "red"},
                    )
                )
                await session.commit()
                record = await proposals.create(
                    session,
                    run.id,
                    {
                        "type": "start_revalidation",
                        "args": {"reason": "Red PSI on PAY_0"},
                        "rationale": "Investigation found a real shift",
                        "evidence": [{"kind": "monitoring_run", "ref": run.id}],
                    },
                    "agent",
                )
                await session.commit()
                validator = Actor("validator-1", "validator")
                await proposals.decide(
                    session,
                    record.id,
                    proposals.Decision(decision="approved", rationale="Agreed"),
                    validator,
                )
                assert (await session.get(ModelRecord, model.id)).lifecycle_status == "monitored"
                executed = await proposals.execute(session, record.id, validator)
                validation = await session.get(runningRecord, executed.result["run_id"])
                assert validation.operation == "validate"
                await session.refresh(model)
                assert model.lifecycle_status == "revalidation_due"

        asyncio.run(scenario())


def test_proposal_schema_forbids_target_override():
    with pytest.raises(ValidationError):
        proposals.ACTIONS["mark_drift_benign"][0].model_validate(
            {"reason": "test", "model_id": "other"}
        )


def test_missing_harness_is_reported_not_faked(monkeypatch):
    with TestClient(app) as client:
        response = client.get("/api/v1/intake/capabilities")
        assert response.status_code == 200
        assert response.json()["harness_profiles"]["survey"] is True
        monkeypatch.setattr(
            intake, "capabilities", lambda: {"survey": False, "intake": False}
        )
        with pytest.raises(HTTPException) as error:
            intake.require_intake()
        assert error.value.status_code == 503


def test_gate_zero_registers_models_and_queues_documents(monkeypatch, tmp_path):
    from test_upload_auth_contracts import dataset_files
    from app.config import settings
    from app.services import native_scoring, run_service
    from app.services.package_service import digest
    from app.services.upload_service import InventoryRecord
    from sqlalchemy import select

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(run_service, "publish_run", lambda identity: None)

    async def reproduce(package, reference_path=None):
        return {
            "status": "PASS",
            "scope": "scripted_test_only",
            "n_rows": package.n_rows,
        }

    monkeypatch.setattr(native_scoring, "reproduce", reproduce)
    with TestClient(app) as client:
        dataset = client.post("/api/v1/datasets", files=dataset_files()).json()[
            "version_id"
        ]
        identity = str(uuid4())
        folder = intake.root(identity) / "files"
        folder.mkdir(parents=True)
        (folder / "score.py").write_text("# Never executed by this test")
        manifest = {
            key: {"value": None, "source": "unknown", "reason": "Not provided"}
            for key in __import__(
                "harness.outputs", fromlist=["MANIFEST_FIELDS"]
            ).MANIFEST_FIELDS
        }
        for key, value in {
            "name": "intake-" + uuid4().hex,
            "version": "1",
            "language": "python",
            "features": ["LIMIT_BAL"],
        }.items():
            manifest[key] = {
                "value": value,
                "source": "proposed",
                "reason": "Test fixture",
            }
        plan = {
            "candidates": [
                {
                    "candidate_id": "candidate",
                    "fitted_model": "parameters_in_code",
                    "manifest": manifest,
                    "files": ["score.py"],
                    "problems": [],
                    "executable": True,
                    "scoring": {"entry_point": "score.py", "wrapper": False},
                    "dataset_mapping": {
                        "dataset_id": dataset,
                        "feature_map": {"LIMIT_BAL": "LIMIT_BAL"},
                        "unmatched_features": [],
                        "rationale": "Test fixture mapping",
                    },
                }
            ],
            "unassigned_files": [],
        }

        async def scenario():
            async with SessionFactory() as session:
                archive = intake.ArchiveRecord(
                    id=identity,
                    actor_id="test-developer",
                    sha256="a" * 64,
                    status="awaiting_review",
                    files=[{"path": "score.py", "sha256": digest(folder / "score.py")}],
                    plan=plan,
                )
                session.add(archive)
                await session.commit()
                body = intake.GateZero(
                    plan_sha256=intake.plan_hash(plan),
                    candidates=[{"candidate_id": "candidate", "accept": True}],
                    rationale="Reviewed source and mapping",
                )
                with pytest.raises(HTTPException) as error:
                    await intake.confirm(
                        session, identity, body, Actor("other", "validator")
                    )
                assert error.value.status_code == 403
                stale = body.model_copy(update={"plan_sha256": "0" * 64})
                with pytest.raises(HTTPException) as error:
                    await intake.confirm(
                        session, identity, stale, Actor("test-developer", "developer")
                    )
                assert error.value.status_code == 409
                mismatched_target = body.model_copy(update={
                    "candidates": [intake.CandidateDecision(
                        candidate_id="candidate", accept=True,
                        manifest={"target": "a different target"},
                    )],
                })
                with pytest.raises(HTTPException) as error:
                    await intake.confirm(
                        session, identity, mismatched_target,
                        Actor("test-developer", "developer"),
                    )
                assert error.value.status_code == 422
                await intake.confirm(
                    session, identity, body, Actor("test-developer", "developer")
                )
            await intake.build_bundles(identity, SessionFactory)
            async with SessionFactory() as session:
                record = await session.get(intake.ArchiveRecord, identity)
                assert record.status == "completed"
                inventories = list(
                    await session.scalars(
                        select(InventoryRecord).where(
                            InventoryRecord.provenance["archive_id"].as_string()
                            == identity
                        )
                    )
                )
                assert len(inventories) == 1
                inventory = inventories[0]
                assert inventory.manifest["language"] == "Python"
                model = await session.get(ModelRecord, inventory.model_id)
                assert model.language == "Python"
                assert inventory.provenance["reproduction"]["status"] == "PASS"
                document = await session.get(runningRecord, inventory.document_run_id)
                assert document.operation == "document" and document.status == "queued"
                assert (tmp_path / "packages").exists()
                from app.db.tables import PackageRecord, ReviewRecord
                from app.services.artifact_service import save_artifact
                from app.services import ai_native_service as ai, native_analysis
                from app.services.harness_adapter import mount_resolver
                from app.services.upload_service import DatasetRecord
                import json
                from harness.handoff import EXAMPLE_METRICS_BUNDLE
                from harness.schemas import TaskInfo

                package = await session.scalar(
                    select(PackageRecord).where(
                        PackageRecord.model_id == inventory.model_id
                    )
                )
                document.status = "completed"
                artifact = await save_artifact(
                    session,
                    document,
                    b"# Accepted test document",
                    "document.md",
                    "text/markdown",
                )
                session.add(
                    ReviewRecord(
                        id=str(uuid4()),
                        run_id=document.id,
                        artifact_id=artifact.id,
                        artifact_version=artifact.version,
                        status="decided",
                        decision="accepted",
                        requested_by="test-developer",
                        reviewer_id="independent-human",
                    )
                )
                validation = runningRecord(
                    model_id=inventory.model_id,
                    model_version="1",
                    dataset_id="uci_credit",
                    operation="validate",
                    status="running",
                )
                session.add(validation)
                await session.commit()
                bound = await session.get(DatasetRecord, inventory.dataset_version_id)
                data_id = await native_analysis._data_input(SessionFactory, bound)
                document_id = await native_analysis._document_input(SessionFactory, inventory)
                metrics_id = await ai.register_input(
                    SessionFactory,
                    "metrics",
                    {"metrics_bundle.json": json.dumps(EXAMPLE_METRICS_BUNDLE).encode()},
                )
                mount = await mount_resolver(SessionFactory)(
                    TaskInfo(
                        task_id=str(uuid4()),
                        thread_id=str(uuid4()),
                        workspace_id="test",
                        kind="validation",
                        actor_id="test-validator",
                        actor_role="validator",
                        policy_version="test",
                        run_id=validation.id,
                        model_package_id=package.id,
                        development_data_id=data_id,
                        document_id=document_id,
                        metrics_bundle_id=metrics_id,
                    )
                )
                from pathlib import Path

                development = (
                    Path(mount.development_data_path) / "development.csv"
                ).read_text()
                assert "uci-000001" in development
                assert "uci-000005" not in development
                assert not (Path(mount.development_data_path) / "test.csv").exists()
                assert (
                    mount.metrics_path
                    and mount.document_path
                    and mount.model_package_path
                )
            # Duplicate bundle delivery must not create a second inventory record.
            await intake.build_bundles(identity, SessionFactory)

        asyncio.run(scenario())


def test_challenger_auc_ties_and_unassessable_labels():
    from app.services.native_analysis import auc

    assert auc([0, 1], [0.1, 0.9]) == 1.0
    assert auc([0, 1], [0.5, 0.5]) == 0.5
    assert auc([0, 1], [0.9, 0.1]) == 0.0
    assert auc([1, 1], [0.1, 0.9]) is None


def test_monitoring_uses_the_worst_valid_psi_with_inclusive_amber_boundaries():
    from app.services.monitoring_service import trigger_band

    assert trigger_band({"score": {"psi": 0.099}}) == "green"
    assert trigger_band({"score": {"psi": 0.1}}) == "amber"
    assert trigger_band({"score": {"psi": 0.25}}) == "amber"
    assert (
        trigger_band({"score": {"psi": 0.1}, "features": {"income": {"psi": 0.251}}})
        == "red"
    )
    assert trigger_band({"score": {"psi": float("nan")}}) == "not_assessable"


def test_native_validation_persists_checked_narrative_and_gate_three(
    monkeypatch, tmp_path
):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from app.config import settings
    from app.services import (
        native_analysis,
        harness_adapter,
        run_service,
        legacy_scoring,
    )
    from app.workers.tasks import process_run
    from test_workflows import model_and_package, submit
    from contracts.harness import HarnessResult
    from harness.validation_report import NARRATIVE_TEMPLATE

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "ai_native_enabled", True)
    monkeypatch.setattr(run_service, "publish_run", lambda identity: None)
    narrative = NARRATIVE_TEMPLATE.replace("MODEL_NAME", "Test model").replace(
        "RECOMMENDATION", "approve with conditions"
    )
    narrative = "\n".join(
        "Independent human review required." if line.startswith("> Guide:") else line
        for line in narrative.splitlines()
    )

    from app.services import ai_native_service
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    fixture = spec_from_file_location(
        "metrics_bundle_fixture",
        Path(__file__).parent / "agent_harness" / "test_metrics_bundle.py",
    )
    official = module_from_spec(fixture)
    fixture.loader.exec_module(official)
    official_report = official.report()
    official_report.setdefault(
        "summary", {"blocking_check_ids": [], "human_approval_granted": False}
    )

    async def read_workspace(task_id, relative):
        return {relative: narrative.encode()}

    async def task_info(*args):
        return SimpleNamespace(task_id="test-task")

    async def bound(sessions, run):
        return SimpleNamespace(manifest={}), SimpleNamespace()

    async def context(sessions, run, package, analysis, inventory, dataset, report):
        # The metrics bundle is built from the official report, as in production.
        return {
            "policy_version": "1.0.0",
            "model_id": run.model_id,
            "model_package_id": package.id,
            "development_data_id": await ai_native_service.register_input(
                sessions, "data", {"development.csv": b"row_id\n"}
            ),
            "document_id": await ai_native_service.register_input(
                sessions, "document", {"model_document.md": b"# Accepted"}
            ),
            "metrics_bundle_id": await ai_native_service.register_metrics_bundle(
                sessions, report
            ),
        }

    monkeypatch.setattr(harness_adapter, "task_info_for_request", task_info)
    monkeypatch.setattr(native_analysis, "_bound", bound)
    monkeypatch.setattr(native_analysis, "_context", context)

    class Runner:
        async def run(self, request, gateway, emit, cancel):
            assert request.kind == "validation"
            assert request.context.metrics_bundle_id
            gateway.read_workspace = read_workspace
            result = await gateway.call(
                "export_artifact", {"path": "validation_narrative.md"}, "test-export"
            )
            assert result.status == "ok", result.content
            return HarnessResult(status="completed", task_id="test-task")

    @asynccontextmanager
    async def runtime(sessions):
        yield Runner()

    monkeypatch.setattr(native_analysis, "runtime", runtime)

    async def generate(package, sessions):
        return official_report, {}

    monkeypatch.setattr(harness_adapter, "generate_validation", generate)
    with TestClient(app) as client:
        model, _ = model_and_package(client)
        identity = submit(client, model)
        asyncio.run(process_run(identity, SessionFactory))
        record = client.get(f"/api/v1/runs/{identity}").json()
        assert record["status"] == "awaiting_review", record
        report = client.get(f"/api/v1/models/{model['id']}/reports").json()[0]
        assert (
            report["evidence"]["agent_review"]["exports"]["validation_narrative.md"]
            == narrative
        )
        downloaded = client.get(f"/api/v1/reports/{report['id']}/markdown")
        assert (
            downloaded.status_code == 200
            and "Independent agent review" in downloaded.text
        )


def test_proposal_api_authentication_roles_and_exact_stored_execution(monkeypatch):
    from app.config import settings
    from app.db.auth_tables import UserRecord
    from app.api.auth import hasher

    monkeypatch.setattr(settings, "allow_local_demo_auth", False)
    with TestClient(app) as client:

        async def seed():
            async with SessionFactory() as session:
                for role in ("developer", "validator"):
                    session.add(
                        UserRecord(
                            id=role,
                            username=role,
                            role=role,
                            password_hash=hasher.hash("test-password-long"),
                        )
                    )
                model = ModelRecord(
                    name="api-proposal",
                    version="1",
                    language="R",
                    dataset_id="uci_credit",
                    purpose="Test only",
                )
                session.add(model)
                await session.flush()
                run = runningRecord(
                    model_id=model.id,
                    model_version="1",
                    dataset_id="uci_credit",
                    operation="monitor",
                    status="completed",
                )
                session.add(run)
                await session.flush()
                session.add(
                    MonitoringRecord(
                        id=str(uuid4()),
                        model_id=model.id,
                        run_id=run.id,
                        payload={"test_only": True},
                    )
                )
                await session.flush()
                proposal = await proposals.create(
                    session,
                    run.id,
                    {
                        "type": "mark_drift_benign",
                        "args": {"reason": "Reviewed synthetic batch"},
                        "rationale": "Test only",
                        "evidence": [{"kind": "monitoring_run", "ref": run.id}],
                    },
                    "agent",
                )
                await session.commit()
                return proposal.id

        identity = asyncio.run(seed())
        assert client.get("/api/v1/proposals").status_code == 401
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"username": "developer", "password": "test-password-long"},
            ).status_code
            == 200
        )
        body = {"decision": "approved", "rationale": "Human evidence review"}
        assert (
            client.post(f"/api/v1/proposals/{identity}/decision", json=body).status_code
            == 403
        )
        client.post("/api/v1/auth/logout")
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"username": "validator", "password": "test-password-long"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/v1/proposals/{identity}/decision",
                json={**body, "args": {"reason": "Changed"}},
            ).status_code
            == 422
        )
        assert (
            client.post(f"/api/v1/proposals/{identity}/decision", json=body).status_code
            == 200
        )
        execution = client.post(f"/api/v1/proposals/{identity}/execute")
        assert execution.status_code == 200, execution.text
        assert execution.json()["status"] == "executed"
        assert execution.json()["args"]["reason"] == "Reviewed synthetic batch"
        assert client.post(f"/api/v1/proposals/{identity}/execute").status_code == 409


def test_archive_intake_runs_survey_and_intakes_through_the_harness_path(
    monkeypatch, tmp_path
):
    """The survey and each intake are ai_native_service runs: registered inputs, the real
    resolver and gateway, export checks. A scripted LLM and Docker stand-in replace only the
    process boundary. The accepted exports become the Gate 0 plan, wrapper included."""
    import json
    from contextlib import asynccontextmanager
    from test_upload_auth_contracts import dataset_files
    from app.config import settings
    from app.services import ai_native_service as ai, native_runner
    from app.services.harness_adapter import mount_resolver
    from harness.config import HarnessSettings
    from harness.dev.gateway import ScriptedLlm, text_turn, tool_turn
    from harness.handoff import EXAMPLE_EXPORTS
    from harness.runner import HarnessRunner
    from harness.sandbox import ProcessResult

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    archive_files = {
        "credit/model.rds": b"RDS",
        "credit/score.R": b"# score",
        "credit/notes.md": b"# notes",
        "README.txt": b"readme",
    }

    class FakeDocker:
        async def run(self, argv, *, timeout_s, max_output, stdin=None):
            if "realpath" in argv:
                return ProcessResult(returncode=0, stdout=argv[-1] + "\n")
            return ProcessResult(returncode=0, stdout="output")

    with TestClient(app) as client:
        dataset = client.post("/api/v1/datasets", files=dataset_files()).json()["version_id"]
        intake_json = json.loads(EXAMPLE_EXPORTS["intake"]["intake.json"])
        intake_json["dataset_mapping"]["dataset_id"] = dataset
        intake_json["scoring"]["wrapper"] = True
        workspace = {
            "survey.json": {"survey.json": EXAMPLE_EXPORTS["survey"]["survey.json"]},
            "intake.json": {"intake.json": json.dumps(intake_json).encode()},
            "scoring_wrapper": {
                "scoring_wrapper/score.py": b"# wrapper",
                "scoring_wrapper/helpers.py": b"# helper",
            },
        }

        async def read_workspace(task_id, relative):
            return dict(workspace[relative.rstrip("/")])

        def script(request):
            if any(m.get("role") == "tool" for m in request.messages):
                return text_turn("Done.")
            first = str(request.messages[1].get("content"))
            if "Run intake" in first:
                return tool_turn(
                    ("export_artifact", {"path": "intake.json"}),
                    ("export_artifact", {"path": "scoring_wrapper/"}),
                )
            return tool_turn(("export_artifact", {"path": "survey.json"}))

        @asynccontextmanager
        async def runtime(sessions):
            runner = HarnessRunner(
                sessions,
                ScriptedLlm(script),
                settings=HarnessSettings(retry_base_delay_s=0.0),
                resolve_mounts=mount_resolver(sessions),
                process_runner=FakeDocker(),
            )
            try:
                yield runner
            finally:
                await runner.shutdown()

        monkeypatch.setattr(native_runner, "runtime", runtime)
        monkeypatch.setattr(ai, "docker_read_workspace", read_workspace)

        identity = str(uuid4())
        folder = intake.root(identity) / "files"
        files = []
        for name, content in archive_files.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(content)
            files.append({"path": name, "sha256": __import__("hashlib").sha256(content).hexdigest()})

        async def scenario():
            async with SessionFactory() as session:
                session.add(
                    intake.ArchiveRecord(
                        id=identity, actor_id="local-operator", sha256="b" * 64,
                        status="queued", files=files,
                    )
                )
                await session.commit()
            await native_runner.process_intake(identity, SessionFactory)
            async with SessionFactory() as session:
                archive = await session.get(intake.ArchiveRecord, identity)
                assert archive.status == "awaiting_review", archive.error
                (candidate,) = archive.plan["candidates"]
                assert candidate["candidate_id"] == "credit_glm"
                assert candidate["files"] == ["credit/model.rds", "credit/score.R", "credit/notes.md"]
                assert archive.plan["unassigned_files"] == ["README.txt"]
                # Both runs are persisted harness runs.
                survey = await session.get(ai.AiNativeRunRecord, archive.plan["survey_run_id"])
                run = await session.get(ai.AiNativeRunRecord, candidate["intake_run_id"])
                assert survey.status == "completed" and survey.kind == "survey"
                assert run.status == "completed" and run.source_run_id == survey.id
                # The wrapper folder is kept as reviewed, with a hash over every file.
                assert candidate["scoring"]["wrapper_files"] == ["helpers.py", "score.py"]
                wrapper = intake.root(identity) / "wrappers" / "credit_glm"
                assert (wrapper / "helpers.py").read_bytes() == b"# helper"

        asyncio.run(scenario())
