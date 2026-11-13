"""Integration checks with an isolated database; no Redis or harness required."""

from fastapi.testclient import TestClient
from app.main import app


def test_model_persistence_validation_and_unfinished_boundaries(monkeypatch):
    from app.services import run_service

    def unavailable(run_id):
        raise ConnectionError("secret must not appear")

    monkeypatch.setattr(run_service, "publish_run", unavailable)
    with TestClient(app) as client:
        assert client.get("/health").json()["harness_connected"] is True
        payload = {
            "name": "Credit scorecard",
            "version": "v1",
            "language": "R",
            "dataset_id": "uci_credit",
            "purpose": "Default prediction",
        }
        created = client.post("/api/v1/models", json=payload)
        assert created.status_code == 201
        model_id = created.json()["id"]
        assert client.get(f"/api/v1/models/{model_id}").json()["owner"] is None
        assert any(row["id"] == model_id for row in client.get("/api/v1/models").json())
        assert client.get("/api/v1/models/missing").status_code == 404
        assert (
            client.post(
                "/api/v1/models", json={**payload, "dataset_id": "unknown"}
            ).status_code
            == 422
        )
        assert (
            client.post("/api/v1/models", json={**payload, "name": "   "}).status_code
            == 422
        )
        assert client.get("/api/v1/models?limit=0").status_code == 422
        response = client.post(
            f"/api/v1/models/{model_id}/runs",
            json={
                "operation": "validate",
                "model_version": "v1",
                "dataset_id": "uci_credit",
            },
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "dispatch_unavailable"
        history = client.get(f"/api/v1/runs?model_id={model_id}").json()
        assert len(history) == 1
        assert history[0]["status"] == "failed"
        assert history[0]["finished_at"] is not None
        assert "secret" not in history[0]["error"]
        assert client.get("/api/v1/runs?limit=0").status_code == 422
        assert client.get("/api/v1/runs/missing").status_code == 404
        assert client.get("/api/v1/runs?model_id=missing").status_code == 404
        for overrides in [{"model_version": "v2"}, {"dataset_id": "ulb_fraud"}]:
            rejected = client.post(
                f"/api/v1/models/{model_id}/runs",
                json={
                    "operation": "validate",
                    "model_version": "v1",
                    "dataset_id": "uci_credit",
                    **overrides,
                },
            )
            assert rejected.status_code == 422
        history = client.get(f"/api/v1/runs?model_id={model_id}").json()
        assert len(history) == 1
        assert history[0]["status"] == "failed"
        assert history[0]["finished_at"] is not None
        assert "secret" not in history[0]["error"]
        assert client.post("/api/v1/conversations").status_code == 201
        assert (
            client.post(
                "/api/v1/reviews/review-1/decision",
                json={
                    "artifact_id": "a",
                    "artifact_version": "v1",
                    "decision": "accepted",
                    "rationale": "Example only",
                },
            ).status_code
            == 403
        )
        datasets = client.get("/api/v1/datasets").json()
        assert datasets == []  # Only verified uploads appear in the catalogue.
        preflight = client.options(
            "/api/v1/models",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert (
            preflight.headers["access-control-allow-origin"] == "http://localhost:5173"
        )
        assert (
            "/api/v1/conversations/{conversation_id}/messages"
            in client.get("/openapi.json").json()["paths"]
        )

    # A second app lifecycle demonstrates that records survived the connection closing.
    with TestClient(app) as client:
        assert client.get(f"/api/v1/models/{model_id}").status_code == 200


def test_accepted_execution_deduplication_results_and_recovery(monkeypatch):
    import asyncio
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import update
    from app.db.session import SessionFactory
    from app.db.tables import runningRecord, RunResultRecord
    from app.services import run_service, harness_adapter
    from app.workers.tasks import process_run, recover_runs
    from contracts.harness import HarnessResult

    published = []
    monkeypatch.setattr(run_service, "publish_run", published.append)
    calls = []

    async def execute(request):
        calls.append(request.run_id)
        return HarnessResult(status="completed", artifact_ids=["test-evidence"])

    monkeypatch.setattr(harness_adapter, "execute", execute)

    with TestClient(app) as client:
        model = client.post(
            "/api/v1/models",
            json={
                "name": "Worker test",
                "version": "v1",
                "language": "R",
                "dataset_id": "uci_credit",
                "purpose": "Isolated worker test",
            },
        ).json()

        def submit():
            response = client.post(
                f"/api/v1/models/{model['id']}/runs",
                json={
                    "operation": "validate",
                    "model_version": "v1",
                    "dataset_id": "uci_credit",
                },
            )
            assert response.status_code == 202
            assert response.json()["status"] == "queued"
            return response.json()["id"]

        run_id = submit()
        assert published == [run_id]

        async def scenario():
            # Concurrent duplicate deliveries can claim a run only once.
            await asyncio.gather(
                process_run(run_id, SessionFactory), process_run(run_id, SessionFactory)
            )
            async with SessionFactory() as session:
                record = await session.get(runningRecord, run_id)
                assert record.status == "completed"
                assert record.started_at and record.finished_at
                result = await session.get(RunResultRecord, run_id)
                assert result.payload["artifact_ids"] == ["test-evidence"]

        asyncio.run(scenario())
        assert calls == [run_id]
        assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "completed"

        failed_id = submit()

        async def broken(request):
            raise RuntimeError("DO NOT STORE SECRET")

        monkeypatch.setattr(harness_adapter, "execute", broken)
        asyncio.run(process_run(failed_id, SessionFactory))
        failure = client.get(f"/api/v1/runs/{failed_id}").json()
        assert failure["status"] == "failed"
        assert "SECRET" not in failure["error"]

        paused_id = submit()

        async def paused(request):
            return HarnessResult(
                status="awaiting_review", checkpoint_id="checkpoint-test"
            )

        monkeypatch.setattr(harness_adapter, "execute", paused)
        asyncio.run(process_run(paused_id, SessionFactory))
        pause = client.get(f"/api/v1/runs/{paused_id}").json()
        assert pause["status"] == "awaiting_review" and pause["finished_at"] is None

        timeout_id = submit()
        from app.config import settings

        monkeypatch.setattr(settings, "run_timeout_seconds", 0.01)

        async def slow(request):
            await asyncio.sleep(1)

        monkeypatch.setattr(harness_adapter, "execute", slow)
        asyncio.run(process_run(timeout_id, SessionFactory))
        timed_out = client.get(f"/api/v1/runs/{timeout_id}").json()
        assert timed_out["status"] == "failed"
        assert timed_out["error"] == "Execution exceeded its time limit."
        monkeypatch.setattr(settings, "run_timeout_seconds", 300)

        queued_id = submit()
        interrupted_id = submit()

        async def recover():
            async with SessionFactory() as session:
                old = datetime.now(timezone.utc) - timedelta(days=2)
                await session.execute(
                    update(runningRecord)
                    .where(runningRecord.id == queued_id)
                    .values(created_at=old)
                )
                await session.execute(
                    update(runningRecord)
                    .where(runningRecord.id == interrupted_id)
                    .values(status="running", started_at=old)
                )
                await session.commit()
            await recover_runs(SessionFactory)

        asyncio.run(recover())
        assert published.count(queued_id) == 2
        assert client.get(f"/api/v1/runs/{interrupted_id}").json()["status"] == "failed"


def test_every_harness_status_maps_to_a_run_status(monkeypatch):
    """Every status in the contract must store, and must keep its result for resuming."""
    import asyncio
    from typing import get_args
    from app.db.session import SessionFactory
    from app.db.tables import RunResultRecord
    from app.services import run_service, harness_adapter
    from app.workers.tasks import process_run
    from contracts.harness import HarnessResult

    monkeypatch.setattr(run_service, "publish_run", lambda run_id: None)
    # (run status, error, finished), with the harness status kept in `stage`.
    expected = {
        "completed": ("completed", None, True),
        "awaiting_review": ("awaiting_review", None, False),
        "awaiting_confirmation": ("awaiting_review", None, False),
        "failed": ("failed", "budget_exhausted", True),
        "cancelled": ("failed", "cancelled", True),
        "interrupted": ("failed", "interrupted", True),
    }
    # A new status in the contract must fail here until it has a mapping.
    assert set(expected) == set(
        get_args(HarnessResult.model_fields["status"].annotation)
    )

    with TestClient(app) as client:
        model = client.post(
            "/api/v1/models",
            json={
                "name": "Status test",
                "version": "v1",
                "language": "R",
                "dataset_id": "uci_credit",
                "purpose": "Isolated status test",
            },
        ).json()

        def run_with(result):
            async def execute(request):
                return result

            monkeypatch.setattr(harness_adapter, "execute", execute)
            run_id = client.post(
                f"/api/v1/models/{model['id']}/runs",
                json={
                    "operation": "rebuild",
                    "model_version": "v1",
                    "dataset_id": "uci_credit",
                },
            ).json()["id"]
            asyncio.run(process_run(run_id, SessionFactory))
            return client.get(f"/api/v1/runs/{run_id}").json()

        async def stored_result(run_id):
            async with SessionFactory() as session:
                return await session.get(RunResultRecord, run_id)

        for harness_status, (status, error, finished) in expected.items():
            run = run_with(
                HarnessResult(
                    status=harness_status,
                    task_id="task-test",
                    checkpoint_id="checkpoint-test",
                    error_code="budget_exhausted"
                    if harness_status == "failed"
                    else None,
                )
            )
            assert run["status"] == status, harness_status
            assert run["stage"] == harness_status
            assert run["error"] == error, harness_status
            assert (run["finished_at"] is not None) == finished, harness_status
            result = asyncio.run(stored_result(run["id"]))
            assert result.payload["status"] == harness_status
            assert result.payload["task_id"] == "task-test"

        # A pause with nothing to resume from is a worker failure, and stores no result.
        for harness_status in ("awaiting_review", "awaiting_confirmation"):
            run = run_with(HarnessResult(status=harness_status, task_id="task-test"))
            assert run["status"] == "failed", harness_status
            assert run["stage"] == "failed"
            assert asyncio.run(stored_result(run["id"])) is None


def test_celery_delivery_to_real_worker(monkeypatch):
    """Exercise broker/worker delivery, not eager mode; no real AI calls."""
    import time
    from celery.contrib.testing.worker import start_worker
    from app.workers.celery_app import celery_app

    previous = celery_app.conf.broker_url
    celery_app.conf.broker_url = "memory://"
    try:
        with (
            TestClient(app) as client,
            start_worker(
                celery_app, pool="solo", perform_ping_check=False, shutdown_timeout=10
            ),
        ):
            model = client.post(
                "/api/v1/models",
                json={
                    "name": "Broker integration",
                    "version": "v1",
                    "language": "R",
                    "dataset_id": "uci_credit",
                    "purpose": "Worker transport test",
                },
            ).json()
            response = client.post(
                f"/api/v1/models/{model['id']}/runs",
                json={
                    "operation": "validate",
                    "model_version": "v1",
                    "dataset_id": "uci_credit",
                },
            )
            assert response.status_code == 202
            run_id = response.json()["id"]
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                record = client.get(f"/api/v1/runs/{run_id}").json()
                if record["status"] == "failed":
                    break
                time.sleep(0.1)
            assert record["status"] == "failed"
            assert record["started_at"] and record["finished_at"]
            assert (
                record["error"]
                == "Register a matching legacy package before starting this operation."
            )
    finally:
        celery_app.conf.broker_url = previous


def test_openrouter_settings_are_optional_and_redacted():
    from app.config import Settings
    from pydantic import SecretStr

    missing = Settings(_env_file=None, openrouter_api_key=None)
    assert missing.openrouter_api_key is None
    empty = Settings(_env_file=None, openrouter_api_key="")
    assert empty.openrouter_api_key.get_secret_value() == ""
    configured = Settings(_env_file=None, openrouter_api_key="test-only-placeholder")
    assert isinstance(configured.openrouter_api_key, SecretStr)
    assert "test-only-placeholder" not in repr(configured)
    assert "test-only-placeholder" not in configured.model_dump_json()
    assert configured.openrouter_api_key.get_secret_value() == "test-only-placeholder"
