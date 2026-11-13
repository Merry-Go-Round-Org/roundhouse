"""P2 contract verification on an isolated test database; no worker/LLM/Docker used."""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.db.session import SessionFactory
from app.db.tables import ModelRecord, PackageRecord, ReviewRecord, runningRecord
from app.services.artifact_service import save_artifact
from app.services import run_service
from app.services.identity import Actor
from app.services.monitoring_service import MonitorJob
from app.services.upload_service import UploadRecord, DatasetRecord, InventoryRecord


def seed_model(task="binary_classification", gate3_approved=True):
    """Registered package and immutable dataset version: test data only."""
    model_id, upload_id, version_id = (str(uuid4()) for _ in range(3))
    name = "p2-test-" + uuid4().hex
    hash_ = "a" * 64

    async def write():
        async with SessionFactory() as session:
            session.add(ModelRecord(
                id=model_id, name=name, version="1.0.0",
                language="Python", dataset_id="uci_credit",
                purpose="P2 monitoring API contract fixture",
            ))
            session.add(UploadRecord(
                id=upload_id, status="completed", actor_id="local-operator",
                archive_sha256=hash_, checks=[], files=[], model_id=model_id,
                error=None,
            ))
            session.add(DatasetRecord(
                id=version_id, dataset_id="uci_credit", version=uuid4().hex,
                sha256=hash_, split_sha256=hash_,
                descriptor={"dataset_id": "uci_credit", "name": "isolated fixture"},
                columns=["row_id", "AGE"],
                statistics={"row_count": 2},
            ))
            await session.flush()
            session.add(InventoryRecord(
                model_id=model_id, name=name, model_version="1.0.0",
                upload_id=upload_id, dataset_version_id=version_id,
                manifest={
                    "task": task,
                    "feature_order": ["AGE"],
                    "artifact_sha256": hash_,
                },
                provenance={}, bundle_sha256=hash_,
            ))
            session.add(PackageRecord(
                id=str(uuid4()), model_id=model_id, name=name,
                version="1.0.0", dataset_id="uci_credit", language="Python",
                sha256=hash_, input_sha256=hash_,
                file_hashes={}, n_rows=2,
            ))
            if task == "binary_classification" and gate3_approved:
                # Explicit persisted Gate 3 sign-off, not a spoofed lifecycle label.
                run = runningRecord(
                    id=str(uuid4()), model_id=model_id, model_version="1.0.0",
                    dataset_id="uci_credit", operation="validate",
                    status="completed", stage="accepted",
                )
                session.add(run)
                await session.flush()
                artifact = await save_artifact(
                    session, run, b'{"validation":"test evidence"}',
                    "fixture.json", "application/json",
                )
                session.add(ReviewRecord(
                    id=str(uuid4()), run_id=run.id,
                    artifact_id=artifact.id, artifact_version=artifact.version,
                    status="decided", requested_by="test-originator",
                    reviewer_id="independent-test-approver", decision="accepted",
                    rationale="Isolated test sign-off; no real validation implied",
                    conditions=[], decided_at=datetime.now(timezone.utc),
                ))
            await session.commit()

    asyncio.run(write())
    return model_id


def test_simulation_dispatch_and_persisted_monitor_job(monkeypatch):
    monkeypatch.setattr(run_service, "publish_run", lambda _: None)
    with TestClient(app) as client:
        model_id = seed_model()
        path = f"/api/v1/models/{model_id}/monitoring"
        assert client.get(path).json() == []
        payload = {"feature": "AGE", "factor": 1.5,
                   "window_label": "Explicit test partition simulation"}
        rejected = client.post(path + "/simulate",
                               json={**payload, "feature": "UNKNOWN"})
        assert rejected.status_code == 422
        rejected = client.post(path + "/simulate",
                               json={**payload, "factor": 0})
        assert rejected.status_code == 422
        response = client.post(path + "/simulate", json=payload)
        assert response.status_code == 202, response.text
        run = response.json()
        assert run["operation"] == "monitor"
        assert run["model_id"] == model_id
        assert run["status"] == "queued"
        assert client.get(f"/api/v1/runs/{run['id']}").json()["status"] == "queued"

        async def inspect():
            async with SessionFactory() as session:
                job = await session.get(MonitorJob, run["id"])
                assert job.payload == {
                    "mode": "simulated",
                    "window_label": payload["window_label"],
                    "shift": {"feature": "AGE", "factor": 1.5},
                }
        asyncio.run(inspect())
        portfolio = client.get("/api/v1/monitoring").json()
        entry = next(row for row in portfolio if row["model_id"] == model_id)
        assert entry["latest"] is None
        assert client.get(path).json() == []
        assert client.get("/api/v1/dashboard").json()["source"] == "persisted_records"
        assert client.post(path, json={"psi": 0.8}).status_code == 410


def test_missing_gate_three_signoff_cannot_enqueue_monitoring(monkeypatch):
    monkeypatch.setattr(run_service, "publish_run", lambda _: None)
    with TestClient(app) as client:
        model_id = seed_model(gate3_approved=False)
        response = client.post(
            f"/api/v1/models/{model_id}/monitoring/simulate",
            json={"feature": "AGE", "factor": 1.5, "window_label": "Not approved"},
        )
        assert response.status_code == 409
        assert "gate_3_required" in response.text
        assert client.get(f"/api/v1/models/{model_id}/monitoring").json() == []


def test_gates_use_current_approved_run_not_an_older_pending_review():
    with TestClient(app):
        model_id = seed_model(gate3_approved=False)

        async def scenario():
            async with SessionFactory() as session:
                model = await session.get(ModelRecord, model_id)
                model.language = "R"
                inventory = await session.get(InventoryRecord, model_id)
                base = datetime.now(timezone.utc)

                async def review_for(operation, offset, decision):
                    run = runningRecord(
                        id=str(uuid4()), model_id=model_id,
                        model_version=model.version, dataset_id=model.dataset_id,
                        operation=operation, status="completed" if decision else "awaiting_review",
                        created_at=base + timedelta(seconds=offset),
                    )
                    session.add(run)
                    await session.flush()
                    artifact = await save_artifact(
                        session, run, b"test evidence", "fixture.txt", "text/plain",
                    )
                    session.add(ReviewRecord(
                        id=str(uuid4()), run_id=run.id,
                        artifact_id=artifact.id, artifact_version=artifact.version,
                        status="decided" if decision else "pending",
                        requested_by="test-author", decision=decision,
                        reviewer_id="independent-reviewer" if decision else None,
                    ))
                    await session.flush()
                    return run.id

                await review_for("document", 0, None)
                approved_document = await review_for("document", 1, "accepted")
                inventory.document_run_id = approved_document
                await run_service.authorize_operation(
                    session, model_id, "rebuild", Actor("developer", "developer")
                )
                await run_service.authorize_operation(
                    session, model_id, "validate", Actor("validator", "validator")
                )

                inventory.document_run_id = await review_for("document", 2, None)
                for operation, role in (("rebuild", "developer"), ("validate", "validator")):
                    with pytest.raises(HTTPException) as error:
                        await run_service.authorize_operation(
                            session, model_id, operation, Actor(role, role)
                        )
                    assert error.value.status_code == 409
                    assert error.value.detail["code"] == "gate_1_required"

                await review_for("validate", 3, None)
                await review_for("validate", 4, "accepted_with_conditions")
                await run_service.authorize_operation(
                    session, model_id, "monitor", Actor("validator", "validator")
                )
                await review_for("validate", 5, None)
                with pytest.raises(HTTPException) as error:
                    await run_service.authorize_operation(
                        session, model_id, "monitor", Actor("validator", "validator")
                    )
                assert error.value.status_code == 409
                assert error.value.detail["code"] == "gate_3_required"

        asyncio.run(scenario())


def test_csv_monitoring_batch_enqueues_one_run(monkeypatch):
    monkeypatch.setattr(run_service, "publish_run", lambda _: None)
    with TestClient(app) as client:
        model_id = seed_model()
        path = f"/api/v1/models/{model_id}/monitoring/batches"
        result = client.post(
            path, files={"batch": ("batch.csv", b"row_id,AGE\nother-0001,45\n", "text/csv")},
            data={"window_label": "Explicit imported batch"},
        )
        assert result.status_code == 202, result.text
        run = result.json()
        assert run["model_id"] == model_id and run["status"] == "queued"
        async def inspect():
            async with SessionFactory() as session:
                job = await session.get(MonitorJob, run["id"])
                assert job.payload["mode"] == "uploaded"
                assert job.payload["window_label"] == "Explicit imported batch"
                assert len(job.payload["input_sha256"]) == 64
        asyncio.run(inspect())


def test_synthetic_task_monitoring_is_not_applicable(monkeypatch):
    monkeypatch.setattr(run_service, "publish_run", lambda _: None)
    with TestClient(app) as client:
        model_id = seed_model(task="survival_analysis")
        response = client.post(
            f"/api/v1/models/{model_id}/monitoring/simulate",
            json={"feature": "AGE", "factor": 1.5, "window_label": "Forbidden"},
        )
        assert response.status_code == 409
        assert "not_applicable" in response.text
        assert client.get(f"/api/v1/models/{model_id}/monitoring").json() == []
