"""Gate 1/2 decisions finish or continue a paused agent run without a separate request.

Flow C retest (issue #32): accepted document and rebuild runs stayed awaiting_review because
only an explicit continuation completed them, and chat cannot request one.
"""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app import main as entrypoint
    from app.db import session as database

    url = f"sqlite+aiosqlite:///{tmp_path / 'continuation.db'}"
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for target in (database, entrypoint):
        monkeypatch.setattr(target, "engine", engine)
        monkeypatch.setattr(target, "SessionFactory", factory)
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    published = []
    from app.services import run_service

    monkeypatch.setattr(run_service, "publish_run", published.append)
    yield factory, published
    asyncio.run(engine.dispose())


async def _paused_run(sessions, operation):
    """A run whose agent task is paused at a pending review checkpoint."""
    from app.db.tables import (
        ModelRecord, ReviewRecord, RunExecutionRecord, RunResultRecord, runningRecord,
    )
    from app.services.artifact_service import save_artifact
    from harness.db import AgentTask

    async with sessions() as session:
        model = ModelRecord(name="paused_" + uuid4().hex[:6], version="1.0.0", language="R",
                            dataset_id="uci_credit", purpose="Test")
        session.add(model)
        await session.flush()
        run = runningRecord(model_id=model.id, model_version="1.0.0", dataset_id="uci_credit",
                            operation=operation, status="awaiting_review", stage="awaiting_review")
        session.add(run)
        await session.flush()
        artifact = await save_artifact(session, run, b"# Document", "model-document.md", "text/markdown")
        task_id = str(uuid4())
        session.add(AgentTask(
            id=task_id, thread_id=task_id, request_id=run.id, actor_id="dev-1", actor_role="developer",
            kind=operation, policy_version="1.0.0", context_json=json.dumps({}), profile_name="p",
            profile_hash="0" * 64, status="awaiting_review", created_at=datetime.now(timezone.utc)))
        review = ReviewRecord(id=str(uuid4()), run_id=run.id, artifact_id=artifact.id,
                              artifact_version=artifact.version, requested_by="dev-1")
        session.add(review)
        session.add(RunExecutionRecord(run_id=run.id, actor_id="dev-1", task_id=task_id))
        session.add(RunResultRecord(run_id=run.id, payload={"checkpoint_id": review.id}))
        await session.commit()
        return run.id, task_id, review.id, artifact


def _decide(sessions, review_id, artifact, decision):
    from app.services.review_service import decide

    async def go():
        async with sessions() as session:
            payload = SimpleNamespace(
                artifact_id=artifact.id, artifact_version=artifact.version, decision=decision,
                rationale="Reviewed", conditions=["Use for research"] if decision == "accepted_with_conditions" else [])
            payload.model_dump = lambda: {"decision": decision}
            return await decide(session, review_id, payload, SimpleNamespace(id="validator-1", role="validator"))

    return asyncio.run(go())


def _state(sessions, run_id, task_id):
    from app.db.tables import RunExecutionRecord, runningRecord
    from harness.db import AgentTask

    async def go():
        async with sessions() as session:
            return (await session.get(runningRecord, run_id), await session.get(AgentTask, task_id),
                    await session.get(RunExecutionRecord, run_id))

    return asyncio.run(go())


@pytest.mark.parametrize("operation", ["document", "rebuild"])
@pytest.mark.parametrize("decision", ["accepted", "accepted_with_conditions"])
def test_acceptance_completes_the_paused_run_and_task(isolated, monkeypatch, operation, decision):
    sessions, published = isolated
    from app.services import review_service

    async def parity_passed(*_args, **_kwargs):
        return None  # Gate 2's own parity guard is tested elsewhere.

    monkeypatch.setattr(review_service, "parity_blocker", parity_passed)
    with TestClient(app):
        run_id, task_id, review_id, artifact = asyncio.run(_paused_run(sessions, operation))
        _decide(sessions, review_id, artifact, decision)
        run, task, control = _state(sessions, run_id, task_id)
    assert (run.status, run.stage) == ("completed", decision)
    assert task.status == "completed" and "final" in task.result_text
    assert control.resolution is None and published == []  # no continuation, no LLM call


def test_rebuild_changes_requested_continues_with_the_comments(isolated):
    sessions, published = isolated
    with TestClient(app):
        run_id, task_id, review_id, artifact = asyncio.run(_paused_run(sessions, "rebuild"))
        _decide(sessions, review_id, artifact, "changes_requested")
        run, task, control = _state(sessions, run_id, task_id)
    assert (run.status, run.stage) == ("queued", "resuming")
    assert control.parent_task_id == task_id
    assert control.resolution["outcome"] == "rejected" and control.resolution["pending_id"] == review_id
    assert published == [run_id]
