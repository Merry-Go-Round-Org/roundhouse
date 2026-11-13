"""Durable API-managed chat, replay, canonical replies and explicit action decisions."""

import asyncio
import json
from contextlib import asynccontextmanager
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from app.main import app
from app.config import settings
from app.db.session import SessionFactory
from app.db.tables import ConversationRecord, ModelRecord, runningRecord
from app.db.chat_tables import ChatTurnRecord
from app.services import chat_service, run_service
from app.services.identity import Actor, actor
from app.schemas.chat import ChatMessageCreate
from contracts.harness import HarnessEvent, HarnessResult


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", SecretStr("offline-test"))
    app.dependency_overrides[actor] = lambda: Actor("chat-user", "developer")

    @asynccontextmanager
    async def controlled(sessions):
        class Runner:
            async def run(self, request, tools, emit, cancel):
                identity = str(uuid4())
                await emit(HarnessEvent(task_id=identity, seq=1, type="task_started"))
                if request.prompt in ("confirm operation", "confirm monitoring"):
                    result = await chat_service.request_action(
                        sessions,
                        request,
                        "monitor"
                        if request.prompt == "confirm monitoring"
                        else "validate",
                        request.request_id + ":tool",
                        {
                            "feature": "LIMIT_BAL",
                            "factor": 2,
                            "window_label": "Explicit chat simulation",
                        }
                        if request.prompt == "confirm monitoring"
                        else None,
                    )
                    assert result.status == "needs_confirmation", result
                    await emit(
                        HarnessEvent(
                            task_id=identity,
                            seq=2,
                            type="confirmation_required",
                            data={
                                "pending_id": result.pending_id,
                                "args": {"SECRET": "never-public"},
                            },
                        )
                    )
                    return HarnessResult(
                        status="awaiting_confirmation",
                        task_id=identity,
                        checkpoint_id=result.pending_id,
                        text="Confirmation required",
                    )
                if request.prompt == "wait for cancel":
                    await cancel.wait()
                    return HarnessResult(status="cancelled", task_id=identity)
                await emit(
                    HarnessEvent(
                        task_id=identity,
                        seq=2,
                        type="tool_call",
                        data={"name": "private", "args": {"key": "SECRET"}},
                    )
                )
                await emit(
                    HarnessEvent(
                        task_id=identity, seq=3, type="token", data={"text": "Partial"}
                    )
                )
                await asyncio.sleep(0.05)
                # Deliberately omit token deltas: canonical text must still be complete.
                return HarnessResult(
                    status="completed",
                    task_id=identity,
                    text="Complete canonical answer.",
                )

        yield Runner()

    monkeypatch.setattr(chat_service, "runtime", controlled)
    monkeypatch.setattr(run_service, "publish_run", lambda _: None)

    async def allow(*args):
        return None

    monkeypatch.setattr(run_service, "authorize_operation", allow)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(actor, None)


def wait_turn(client, cid, tid):
    import time

    for _ in range(100):
        state = client.get(f"/api/v1/conversations/{cid}/turns/{tid}").json()
        if state["status"] not in ("queued", "running"):
            return state
        time.sleep(0.02)
    raise AssertionError(state)


def test_deduplication_replay_and_final_reply(client):
    cid = client.post("/api/v1/conversations").json()["id"]
    path = f"/api/v1/conversations/{cid}/messages"
    body = {"message": "hello", "client_message_id": str(uuid4())}
    sent = client.post(path, json=body, headers={"Accept": "application/json"})
    assert sent.status_code == 202, sent.text
    tid = sent.json()["id"]
    retry = client.post(path, json=body, headers={"Accept": "application/json"})
    assert retry.json()["id"] == tid
    assert client.post(path, json={**body, "message": "different"}).status_code == 409
    state = wait_turn(client, cid, tid)
    assert state["status"] == "completed"
    final = client.get(path + "/" + state["assistant_message_id"]).json()
    assert final["content"] == "Complete canonical answer."
    events = client.get(f"/api/v1/conversations/{cid}/turns/{tid}/events").text
    assert "SECRET" not in events and "private" not in events
    frames = [
        json.loads(line[6:])
        for line in events.splitlines()
        if line.startswith("data: ")
    ]
    assert frames[-1]["event"] == "done"
    assert frames[-1]["assistant_message_id"] == final["id"]
    assert frames[-1]["text"] == final["content"]
    assert len(client.get(path).json()) == 2
    other = client.post("/api/v1/conversations").json()["id"]
    assert client.get(f"/api/v1/conversations/{other}/turns/{tid}").status_code == 404


def test_explicit_cancel_does_not_claim_completion(client):
    cid = client.post("/api/v1/conversations").json()["id"]
    turn = client.post(
        f"/api/v1/conversations/{cid}/messages",
        json={"message": "wait for cancel"},
        headers={"Accept": "application/json"},
    ).json()
    assert (
        client.post(
            f"/api/v1/conversations/{cid}/turns/{turn['id']}/cancel"
        ).status_code
        == 200
    )
    result = wait_turn(client, cid, turn["id"])
    assert result["status"] == "cancelled"


@pytest.mark.parametrize("operation", ["validate", "monitor"])
def test_confirmed_run_only_once_and_model_bound(client, monkeypatch, operation):
    async def seed():
        async with SessionFactory() as s:
            model = ModelRecord(
                id=str(uuid4()),
                name="Chat action fixture",
                version="1.0.0",
                language="R",
                dataset_id="uci_credit",
                purpose="Test",
            )
            s.add(model)
            if operation == "monitor":
                from app.services.upload_service import InventoryRecord, UploadRecord

                upload = UploadRecord(
                    id=str(uuid4()),
                    actor_id="chat-user",
                    archive_sha256="a" * 64,
                    checks=[],
                    files=[],
                )
                s.add(upload)
                await s.flush()
                s.add(
                    InventoryRecord(
                        model_id=model.id,
                        name=model.id,
                        model_version="1.0.0",
                        upload_id=upload.id,
                        manifest={"feature_order": ["LIMIT_BAL"]},
                        provenance={},
                        bundle_sha256="a" * 64,
                    )
                )

            await s.commit()
            return model.id

    mid = asyncio.run(seed())
    cid = client.post("/api/v1/conversations").json()["id"]
    path = f"/api/v1/conversations/{cid}"
    turn = client.post(
        path + "/messages",
        json={
            "message": "confirm monitoring"
            if operation == "monitor"
            else "confirm operation",
            "context": {"model_id": mid},
        },
        headers={"Accept": "application/json"},
    ).json()
    assert wait_turn(client, cid, turn["id"])["status"] == "awaiting_confirmation"
    assert (
        client.post(
            path + "/messages", json={"message": "yes", "context": {"model_id": mid}}
        ).status_code
        == 409
    )
    action = client.get(path + "/pending-actions").json()[0]
    assert action["run_id"] is None
    body = {
        "client_decision_id": str(uuid4()),
        "action_version": action["action_version"],
        "outcome": "approved",
        "rationale": "Explicit test confirmation",
    }
    endpoint = path + "/pending-actions/" + action["id"] + "/decision"
    assert (
        client.post(endpoint, json={**body, "action_version": "0" * 64}).status_code
        == 409
    )
    decision = client.post(endpoint, json=body)
    assert decision.status_code == 200, decision.text
    run_id = decision.json()["run_id"]
    assert run_id
    duplicate = client.post(endpoint, json=body)
    assert duplicate.status_code == 200 and duplicate.json()["run_id"] == run_id
    assert (
        client.post(endpoint, json={**body, "outcome": "rejected"}).status_code == 409
    )
    assert (
        wait_turn(client, cid, decision.json()["continuation_id"])["status"]
        == "completed"
    )

    async def verify():
        async with SessionFactory() as s:
            runs = list(
                await s.scalars(
                    select(runningRecord).where(runningRecord.model_id == mid)
                )
            )
            assert len(runs) == 1 and runs[0].id == run_id
            if operation == "monitor":
                from app.services.monitoring_service import MonitorJob

                job = await s.get(MonitorJob, run_id)
                assert job.payload["shift"] == {"feature": "LIMIT_BAL", "factor": 2}

    asyncio.run(verify())


def test_observer_disconnect_does_not_cancel_turn(client):
    async def check():
        async with SessionFactory() as s:
            c = ConversationRecord(id=str(uuid4()), actor_id="chat-user")
            s.add(c)
            await s.commit()
            turn = await chat_service.submit(
                s,
                c.id,
                ChatMessageCreate(message="hello"),
                Actor("chat-user", "developer"),
            )
        chat_service.start_turn(turn.id)
        observer = chat_service.observe(turn.id)
        first = await asyncio.wait_for(anext(observer), 2)
        assert "data:" in first
        await observer.aclose()
        if turn.id in chat_service._tasks:
            await asyncio.gather(chat_service._tasks[turn.id], return_exceptions=True)
        async with SessionFactory() as s:
            saved = await s.get(ChatTurnRecord, turn.id)
            assert saved.status == "completed"

    asyncio.run(check())


def test_equal_timestamp_messages_still_put_user_before_answer(client):
    from app.db.tables import ChatMessageRecord
    from datetime import datetime, timezone

    cid = client.post("/api/v1/conversations").json()["id"]

    async def seed():
        async with SessionFactory() as s:
            when = datetime.now(timezone.utc)
            s.add(
                ChatMessageRecord(
                    id="a" + uuid4().hex,
                    conversation_id=cid,
                    role="assistant",
                    content="Reply",
                    status="completed",
                    created_at=when,
                )
            )
            s.add(
                ChatMessageRecord(
                    id="z" + uuid4().hex,
                    conversation_id=cid,
                    role="user",
                    content="Question",
                    status="completed",
                    created_at=when,
                )
            )
            await s.commit()

    asyncio.run(seed())
    messages = client.get("/api/v1/conversations/" + cid + "/messages").json()
    assert [message["role"] for message in messages] == ["user", "assistant"]
