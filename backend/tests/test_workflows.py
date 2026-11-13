"""Backend workflow acceptance: reports, gates, continuation, chat, monitoring, cancellation."""

import asyncio
import json
from contextlib import asynccontextmanager
from uuid import uuid4
from fastapi.testclient import TestClient
from pydantic import SecretStr
from app.main import app
from app.config import settings
from app.db.session import SessionFactory
from app.db.tables import (
    RunExecutionRecord,
)
from app.services import harness_adapter, run_service, chat_service
from app.workers.tasks import process_run


def model_and_package(client):
    model = client.post(
        "/api/v1/models",
        json={
            "name": "Workflow",
            "version": "1.0.0",
            "language": "R",
            "dataset_id": "uci_credit",
            "purpose": "Test",
        },
    ).json()
    response = client.post(
        f"/api/v1/models/{model['id']}/packages",
        json={"package_name": "credit_logistic_r"},
    )
    assert response.status_code == 201, response.text
    return model, response.json()


def submit(client, model, operation="validate"):
    response = client.post(
        f"/api/v1/models/{model['id']}/runs",
        json={
            "model_version": "1.0.0",
            "dataset_id": "uci_credit",
            "operation": operation,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def test_validation_reports_gate_download_and_persistence(monkeypatch, tmp_path):
    monkeypatch.setattr(run_service, "publish_run", lambda _: None)
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "reviewer_token", SecretStr("test-reviewer-token"))
    monkeypatch.setattr(settings, "reviewer_id", "independent-reviewer")
    headers = {"Authorization": "Bearer test-reviewer-token"}

    async def generate(package, sessions=None):
        return {
            "checks": [
                {
                    "check_id": "test_check",
                    "status": "FAIL",
                    "required": True,
                    "summary": "Known defect",
                    "evidence_kind": "test",
                    "details": {},
                }
            ],
            "summary": {
                "blocking_check_ids": ["test_check"],
                "human_approval_granted": False,
            },
        }, {"manifest": "testhash"}

    monkeypatch.setattr(harness_adapter, "generate_validation", generate)
    with TestClient(app) as client:
        model, _ = model_and_package(client)
        run_id = submit(client, model)
        asyncio.run(process_run(run_id, SessionFactory))
        assert (
            client.get(f"/api/v1/runs/{run_id}").json()["status"] == "awaiting_review"
        )
        reports = client.get(f"/api/v1/models/{model['id']}/reports").json()
        report = reports[0]
        assert report["metrics"][0]["status"] == "red"
        assert (
            client.get(f"/api/v1/reports/{report['id']}").json()["evidence"]["summary"][
                "human_approval_granted"
            ]
            is False
        )
        download = client.get(f"/api/v1/artifacts/{report['artifact_id']}/download")
        assert (
            download.status_code == 200
            and download.json()["checks"][0]["check_id"] == "test_check"
        )
        reviews = client.get("/api/v1/reviews").json()
        review = next(r for r in reviews if r["run_id"] == run_id)
        decision = {
            "artifact_id": review["artifact_id"],
            "artifact_version": review["artifact_version"],
            "decision": "accepted",
            "rationale": "Reviewed",
        }
        url = f"/api/v1/reviews/{review['id']}/decision"
        assert client.post(url, json=decision).status_code == 403
        assert client.post(url, json=decision, headers=headers).status_code == 409
        assert (
            client.post(
                url, json={**decision, "artifact_version": "wrong"}, headers=headers
            ).status_code
            == 409
        )
        decision["decision"] = "changes_requested"
        assert client.post(url, json=decision, headers=headers).status_code == 200
        assert client.post(url, json=decision, headers=headers).status_code == 409
        assert (
            client.get(f"/api/v1/runs/{run_id}").json()["stage"] == "changes_requested"
        )
        finding = client.get(f"/api/v1/findings?model_id={model['id']}").json()[0]
        assert (
            client.patch(
                f"/api/v1/findings/{finding['id']}",
                json={"status": "in_progress", "comment": "Fixing"},
                headers=headers,
            ).status_code
            == 200
        )

        async def assignable_owner():
            from app.db.auth_tables import UserRecord
            from uuid import uuid4

            async with SessionFactory() as session:
                user = UserRecord(
                    id=str(uuid4()),
                    username="owner-" + uuid4().hex,
                    role="developer",
                    password_hash="not-used-test-fixture",
                )
                session.add(user)
                await session.commit()
                return user.id

        owner_id = asyncio.run(assignable_owner())
        assert (
            client.patch(
                f"/api/v1/findings/{finding['id']}",
                json={"owner_id": "missing-user", "comment": "Assign"},
                headers=headers,
            ).status_code
            == 422
        )
        assigned = client.patch(
            f"/api/v1/findings/{finding['id']}",
            json={
                "owner_id": owner_id,
                "due_date": "2026-11-10T09:00:00+01:00",
                "comment": "Assigned for remediation",
            },
            headers=headers,
        )
        assert assigned.status_code == 200, assigned.text
        assert assigned.json()["owner_id"] == owner_id
        assert assigned.json()["due_date"] == "2026-11-10T08:00:00+00:00"
        assert assigned.json()["status"] == "in_progress"
        assert client.get("/api/v1/dashboard").json()["reports"] >= 1
    with TestClient(app) as client:
        assert client.get(f"/api/v1/reports/{report['id']}").status_code == 200
        assert any(
            r["id"] == review["id"]
            for r in client.get("/api/v1/reviews?status=decided").json()
        )
        artifact = tmp_path / "artifacts" / report["artifact_id"] / "content"
        artifact.write_text("tampered")
        assert (
            client.get(
                f"/api/v1/artifacts/{report['artifact_id']}/download"
            ).status_code
            == 409
        )


def test_cancel_and_missing_review_cannot_resume(monkeypatch):
    monkeypatch.setattr(run_service, "publish_run", lambda _: None)
    with TestClient(app) as client:
        model, _ = model_and_package(client)
        identity = submit(client, model, "document")
        assert (
            client.post(
                f"/api/v1/runs/{identity}/resume", json={"review_id": "missing"}
            ).status_code
            == 409
        )
        response = client.post(f"/api/v1/runs/{identity}/cancel")
        assert response.status_code == 200 and response.json()["stage"] == "cancelled"
        asyncio.run(process_run(identity, SessionFactory))
        assert client.get(f"/api/v1/runs/{identity}").json()["status"] == "failed"
        assert (
            client.get(f"/api/v1/runs/{identity}/events").json()[0]["action"]
            == "cancel_requested"
        )


@asynccontextmanager
async def scripted_runtime(sessions):
    from harness.runner import HarnessRunner
    from harness.dev.gateway import ScriptedLlm, text_turn, tool_turn

    llm = ScriptedLlm(
        [tool_turn(("get_model_status", {})), text_turn("Evidence needs human review.")]
    )
    runner = HarnessRunner(sessions, llm, settings=harness_adapter.harness_settings())
    try:
        yield runner
    finally:
        await runner.shutdown()


def test_chat_persistence_stream_ownership_and_context(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", SecretStr("test-placeholder"))
    monkeypatch.setattr(chat_service, "runtime", scripted_runtime)
    monkeypatch.setattr(settings, "reviewer_token", SecretStr("different-user-token"))
    with TestClient(app) as client:
        model, _ = model_and_package(client)
        conversation = client.post("/api/v1/conversations").json()["id"]
        path = f"/api/v1/conversations/{conversation}/messages"
        payload = {
            "message": "Describe this model",
            "context": {"model_id": model["id"]},
        }
        response = client.post(path, json=payload)
        assert response.status_code == 200 and response.headers[
            "content-type"
        ].startswith("text/event-stream")
        events = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert events[-1]["event"] == "done", response.text
        assert "human review" in "".join(e.get("text", "") for e in events)
        history = client.get(path).json()
        assert [m["role"] for m in history] == ["user", "assistant"]
        assert history[-1]["status"] == "completed"

        async def inspect_tool_result():
            from sqlalchemy import select
            from app.db.tables import AuditRecord

            async with SessionFactory() as session:
                rows = list(
                    await session.scalars(
                        select(AuditRecord)
                        .where(AuditRecord.action == "tool_output")
                        .order_by(AuditRecord.created_at.desc())
                    )
                )
                payload = next(
                    r.payload["result"]
                    for r in rows
                    if r.payload["name"] == "get_model_status"
                )
                assert payload["status"] == "ok"
                assert json.loads(payload["content"])["model"]["id"] == model["id"]

        asyncio.run(inspect_tool_result())
        assert (
            client.get(
                path, headers={"Authorization": "Bearer different-user-token"}
            ).status_code
            == 404
        )
        other, _ = model_and_package(client)
        assert (
            client.post(
                path, json={**payload, "context": {"model_id": other["id"]}}
            ).status_code
            == 409
        )
        # Continuation uses stored harness history, not browser-supplied messages.
        assert '"event": "done"' in client.post(path, json=payload).text
        assert len(client.get(path).json()) == 4
        monkeypatch.setattr(settings, "openrouter_api_key", None)
        fresh = client.post("/api/v1/conversations").json()["id"]
        assert (
            client.post(
                f"/api/v1/conversations/{fresh}/messages", json=payload
            ).status_code
            == 503
        )


def test_ai_native_contract_preserves_context_and_proposals():
    from contracts.harness import HarnessRequest, HarnessEvent, HarnessResult, ToolResult

    context = dict(
        policy_version="test-policy", archive_id="archive", candidate_id="candidate",
        document_id="document", metrics_bundle_id="metrics",
        monitoring_run_id="monitoring", validation_report_id="report",
    )
    for kind in (
        "chat", "document", "rebuild", "survey", "intake", "validation",
        "triage", "investigation",
    ):
        request = HarnessRequest(
            request_id="test-" + kind, actor={"id": "test", "role": "developer"},
            kind=kind, context=context,
        )
        restored = HarnessRequest.model_validate_json(request.model_dump_json())
        assert restored.kind == kind
        assert restored.context.model_dump(include=set(context)) == context
    event = HarnessEvent(
        task_id="test", seq=1, type="proposal_created", data={"proposal_id": "proposal"},
    )
    assert event.data["proposal_id"] == "proposal"
    assert ToolResult(status="ok", content="Created", proposal_id="proposal").proposal_id == "proposal"
    assert HarnessResult(status="completed", proposal_ids=["proposal"]).proposal_ids == ["proposal"]
    first = HarnessResult(status="completed")
    first.proposal_ids.append("first")
    assert HarnessResult(status="completed").proposal_ids == []


def test_old_aggregate_monitoring_is_retired():
    with TestClient(app) as client:
        model, _ = model_and_package(client)
        response = client.post(f"/api/v1/models/{model['id']}/monitoring", json={})
        assert response.status_code == 410


def test_running_cancellation_stops_adapter(monkeypatch):
    monkeypatch.setattr(run_service, "publish_run", lambda _: None)
    entered = asyncio.Event()
    stopped = []

    async def slow(request):
        entered.set()
        try:
            await asyncio.sleep(10)
        finally:
            stopped.append(request.run_id)

    monkeypatch.setattr(harness_adapter, "execute", slow)
    with TestClient(app) as client:
        model, _ = model_and_package(client)
        identity = submit(client, model, "document")

        async def scenario():
            worker = asyncio.create_task(process_run(identity, SessionFactory))
            await entered.wait()
            async with SessionFactory() as session:
                control = await session.get(RunExecutionRecord, identity)
                control.cancel_requested = True
                await session.commit()
            await asyncio.wait_for(worker, 2)

        asyncio.run(scenario())
        assert stopped == [identity]
        assert client.get(f"/api/v1/runs/{identity}").json()["status"] == "failed"


def test_chat_cannot_offer_write_or_review_tools():
    from types import SimpleNamespace
    from app.services.tool_gateway import Platform

    task = SimpleNamespace(kind="chat", actor_role="operator")
    gateway = Platform(task, None, SessionFactory)

    async def scenario():
        assert {t.name for t in await gateway.list_tools()} == {
            "get_model_status",
            "find_models",
            "get_datasets",
            "read_template",
            "request_run",
            "request_monitoring",
        }
        result = await gateway.call(
            "request_review", {"summary": "approve me"}, "test-key"
        )
        assert result.status == "denied"

    asyncio.run(scenario())


def test_open_harness_wiring_and_read_only_gateway(monkeypatch):
    """Exercise production open_harness wiring; replace only the provider transport."""
    from contracts.harness import HarnessRequest, Actor, RequestContext
    from harness.dev.gateway import ScriptedLlm, tool_turn, text_turn
    from harness import runner as bridge
    from app.services.tool_gateway import BackendGateway

    seen = {}

    class OfflineClient(ScriptedLlm):
        def __init__(self, key, config):
            assert key == "offline-test-key"
            seen["budget"] = config.global_budget_usd
            super().__init__(
                [tool_turn(("get_model_status", {})), text_turn("Checked context.")]
            )

        async def aclose(self):
            seen["closed"] = True

    monkeypatch.setattr(bridge, "OpenRouterClient", OfflineClient)
    monkeypatch.setattr(settings, "openrouter_api_key", SecretStr("offline-test-key"))
    monkeypatch.setattr(settings, "harness_budget_usd", 0.25)
    with TestClient(app) as client:
        model, _ = model_and_package(client)

        async def check():
            request = HarnessRequest(
                request_id=str(uuid4()),
                actor=Actor(id="local-operator", role="operator"),
                kind="chat",
                prompt="Read the current model",
                context=RequestContext(policy_version="1.0.0", model_id=model["id"]),
            )
            gateway = BackendGateway(request, SessionFactory)
            assert {s.name for s in await gateway.list_tools()} == {
                "get_model_status",
                "find_models",
                "get_datasets",
                "request_run",
                "request_monitoring",
                "read_template",
            }
            denied = await gateway.call(
                "request_review", {"summary": "approve"}, "denied-call"
            )
            assert denied.status == "denied"
            events = []

            async def emit(event):
                events.append(event)

            async with harness_adapter.runtime(SessionFactory) as runner:
                result = await runner.run(request, gateway, emit, asyncio.Event())
            assert result.status == "completed" and result.task_id
            assert result.text == "Checked context."
            assert any(e.type == "tool_result" for e in events)
            assert events[-1].type == "done"

        asyncio.run(check())
    assert seen == {"budget": 0.25, "closed": True}


def test_sqlite_harness_pragmas_preserve_backend_tables():
    from sqlalchemy import text

    with TestClient(app):

        async def check():
            async with SessionFactory() as session:
                assert (
                    await session.execute(text("PRAGMA journal_mode"))
                ).scalar() == "wal"
                assert (
                    await session.execute(text("PRAGMA busy_timeout"))
                ).scalar() == 5000
                assert (
                    await session.execute(text("PRAGMA foreign_keys"))
                ).scalar() == 1

        asyncio.run(check())


def test_chat_reads_dataset_facts_from_the_platform():
    """Chat answers dataset questions from the catalog and uploaded versions, not the guide."""
    from types import SimpleNamespace
    from app.services.tool_gateway import Platform
    from app.services.upload_service import DatasetRecord

    version = "workflow-" + uuid4().hex
    task = SimpleNamespace(kind="chat", actor_role="operator", actor_id="local-operator")
    gateway = Platform(task, None, SessionFactory)

    record_id = str(uuid4())

    async def scenario():
        try:
            await check()
        finally:
            # Other tests expect no uploaded datasets in the shared test database.
            async with SessionFactory() as session:
                await session.delete(await session.get(DatasetRecord, record_id))
                await session.commit()

    async def check():
        async with SessionFactory() as session:
            session.add(
                DatasetRecord(
                    id=record_id,
                    dataset_id="ulb_fraud",
                    version=version,
                    sha256="b" * 64,
                    split_sha256="c" * 64,
                    descriptor={
                        "dataset_id": "ulb_fraud",
                        "name": "ULB Credit Card Fraud",
                        "version": version,
                        "source_url": "https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud",
                        "license": "DbCL v1.0",
                        "sha256": "b" * 64,
                        "split_sha256": "c" * 64,
                        "row_id_rule": "zero-based row index",
                        "source_filename": "creditcard.csv",
                        "time_column": "Time",
                        "target_column": "Class",
                        "positive_class": 1,
                        "audit_only_columns": [],
                        "row_count": 4,
                        "partition_counts": {"train": 2, "validation": 1, "test": 1},
                    },
                    columns=["Time", "V1", "Amount", "Class"],
                    statistics={"positive_rates": {"test": 0.5}},
                )
            )
            await session.commit()
        result = await gateway.call("get_datasets", {}, "datasets-" + uuid4().hex)
        assert result.status == "ok"
        datasets = {d["id"]: d for d in json.loads(result.content)}
        assert set(datasets) == {"uci_credit", "ulb_fraud"}
        ulb = datasets["ulb_fraud"]
        assert "Two-day observation window" in ulb["limitations"]
        uploaded = next(v for v in ulb["uploaded_versions"] if v["version"] == version)
        assert uploaded["target_column"] == "Class"
        assert uploaded["columns"] == ["Time", "V1", "Amount", "Class"]
        assert uploaded["partition_counts"]["test"] == 1
        # Outcome rates per partition stay out, so the test partition leaks nothing but its size.
        assert "positive_rates" not in result.content

    with TestClient(app):
        asyncio.run(scenario())
