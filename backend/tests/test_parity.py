"""Official parity: one platform check shared by the rebuild agent and people.

Isolated test database; Docker scoring and file-integrity helpers are replaced
at their boundaries, so lineage, comparison, records and Gate 2 run for real.
"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from app.main import app
from app.config import settings
from app.db.session import SessionFactory, initialize_database
from app.db.tables import (
    AuditRecord,
    ModelRecord,
    PackageRecord,
    ReviewRecord,
    RunResultRecord,
    runningRecord,
)
from app.services import legacy_scoring, parity_service, upload_service
from app.services.artifact_service import save_artifact
from app.services.parity_service import TOLERANCE_ABSOLUTE, compare
from app.services.tool_gateway import Platform, tools_for
from app.services.upload_service import InventoryRecord, UploadRecord

ROWS = ["uci-000001", "uci-000002", "uci-000003"]
REFERENCE = [0.125, 0.5, 0.875]


def predictions(values):
    lines = ["row_id,positive_probability"]
    lines += [f"{row},{value!r}" for row, value in zip(ROWS, values)]
    return ("\n".join(lines) + "\n").encode()


def test_compare_is_strict_ordered_and_deterministic():
    exact = compare(ROWS, predictions(REFERENCE), predictions(REFERENCE))
    assert exact["passed"] and exact["n_above_tolerance"] == 0
    edge = [REFERENCE[0] + TOLERANCE_ABSOLUTE / 2, *REFERENCE[1:]]
    assert compare(ROWS, predictions(edge), predictions(REFERENCE))["passed"]
    off = [REFERENCE[0] + 1e-6, REFERENCE[1] + 1e-3, REFERENCE[2]]
    first = compare(ROWS, predictions(off), predictions(REFERENCE))
    assert not first["passed"] and first["n_above_tolerance"] == 2
    assert [r["row_id"] for r in first["worst_rows"]] == ["uci-000002", "uci-000001"]
    assert first == compare(ROWS, predictions(off), predictions(REFERENCE))


def test_only_rebuild_tasks_can_run_parity():
    assert "run_parity" in {s.name for s in tools_for("rebuild")}
    assert "run_parity" not in {s.name for s in tools_for("document")}
    assert "run_parity" not in {s.name for s in tools_for("chat")}


def seed(tmp_path):
    """Parent R model, its rebuild run, an exported candidate and a Gate 2 checkpoint."""
    ids = SimpleNamespace(
        parent=str(uuid4()),
        candidate=str(uuid4()),
        parent_package=str(uuid4()),
        run=str(uuid4()),
        review=str(uuid4()),
        actor="local-operator",
    )
    parent_sha, input_sha = "a" * 64, "b" * 64
    name = "parity-test-" + uuid4().hex[:8]

    async def write():
        async with SessionFactory() as session:
            for model_id, language, version in (
                (ids.parent, "R", "1.0.0"),
                (ids.candidate, "Python", "1.0.0-r1"),
            ):
                session.add(
                    ModelRecord(
                        id=model_id,
                        name=name + ("_rebuilt" if language == "Python" else ""),
                        version=version,
                        language=language,
                        dataset_id="uci_credit",
                        purpose="Parity test fixture",
                    )
                )
            await session.flush()
            for model_id, package_id, sha, bundle, manifest in (
                (
                    ids.parent,
                    ids.parent_package,
                    parent_sha,
                    "c" * 64,
                    {"task": "binary_classification", "feature_order": ["AGE"]},
                ),
                (
                    ids.candidate,
                    str(uuid4()),
                    "d" * 64,
                    "e" * 64,
                    {
                        "task": "binary_classification",
                        "feature_order": ["AGE"],
                        "derived_from": {
                            "model_id": ids.parent,
                            "model_version": "1.0.0",
                            "package_sha256": parent_sha,
                            "run_id": ids.run,
                        },
                    },
                ),
            ):
                upload = str(uuid4())
                session.add(
                    UploadRecord(
                        id=upload,
                        status="completed",
                        actor_id=ids.actor,
                        archive_sha256=bundle,
                        checks=[],
                        files=[],
                        model_id=model_id,
                    )
                )
                await session.flush()
                record = await session.get(ModelRecord, model_id)
                session.add(
                    InventoryRecord(
                        model_id=model_id,
                        name=record.name,
                        model_version=record.version,
                        upload_id=upload,
                        manifest=manifest,
                        provenance={},
                        bundle_sha256=bundle,
                    )
                )
                session.add(
                    PackageRecord(
                        id=package_id,
                        model_id=model_id,
                        name=record.name,
                        version=record.version,
                        dataset_id="uci_credit",
                        language=record.language,
                        sha256=sha,
                        input_sha256=input_sha,
                        file_hashes={},
                        n_rows=len(ROWS),
                    )
                )
            run = runningRecord(
                id=ids.run,
                model_id=ids.parent,
                model_version="1.0.0",
                dataset_id="uci_credit",
                operation="rebuild",
                status="running",
            )
            session.add(run)
            await session.flush()
            artifact = await save_artifact(
                session, run, b"candidate zip", "candidate.zip", "application/zip"
            )
            ids.artifact, ids.artifact_version = artifact.id, artifact.version
            session.add(
                AuditRecord(
                    actor_id=ids.actor,
                    resource_id=run.id,
                    action="candidate_exported",
                    payload={
                        "artifact_id": artifact.id,
                        "candidate_model_id": ids.candidate,
                    },
                )
            )
            await session.commit()

    asyncio.run(write())
    snapshot = tmp_path / "snapshot"
    (snapshot / "package").mkdir(parents=True)
    (snapshot / "data").mkdir()
    (snapshot / "package/manifest.json").write_text(
        json.dumps({"feature_order": ["AGE"]})
    )
    (snapshot / "data/development_inputs.csv").write_text(
        "row_id,AGE\n" + "".join(f"{row},30\n" for row in ROWS)
    )
    bundle = tmp_path / "bundle"
    (bundle / "fixtures").mkdir(parents=True)
    (bundle / "fixtures/reference_predictions.csv").write_bytes(predictions(REFERENCE))
    return ids, snapshot, bundle


def open_checkpoint(ids, blockers=()):
    async def write():
        async with SessionFactory() as session:
            run = await session.get(runningRecord, ids.run)
            run.status = "awaiting_review"
            session.add(
                ReviewRecord(
                    id=ids.review,
                    run_id=ids.run,
                    artifact_id=ids.artifact,
                    artifact_version=ids.artifact_version,
                    requested_by=ids.actor,
                    blockers=list(blockers),
                )
            )
            session.add(
                RunResultRecord(run_id=ids.run, payload={"checkpoint_id": ids.review})
            )
            await session.commit()

    asyncio.run(write())


@pytest.fixture
def scoring(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(settings, "reviewer_token", SecretStr("parity-reviewer-token"))
    monkeypatch.setattr(settings, "reviewer_id", "independent-reviewer")
    asyncio.run(initialize_database())
    ids, snapshot, bundle = seed(tmp_path)
    state = {"values": REFERENCE, "calls": 0}

    async def fake_score(package, execution_id):
        state["calls"] += 1
        content = predictions(state["values"])
        folder = settings.storage_path / "scoring" / execution_id
        folder.mkdir(parents=True)
        (folder / "predictions.csv").write_bytes(content)
        return {
            "predictions_sha256": "f" * 64,
            "prediction_reference": f"scoring/{execution_id}/predictions.csv",
        }

    monkeypatch.setattr(parity_service, "verify_snapshot", lambda *_: snapshot)
    monkeypatch.setattr(upload_service, "verify_uploaded_bundle", lambda _: bundle)
    monkeypatch.setattr(legacy_scoring, "score_snapshot", fake_score)
    return ids, state


def agent_parity(ids, key):
    task = SimpleNamespace(
        kind="rebuild",
        actor_role="developer",
        actor_id=ids.actor,
        model_package_id=ids.parent_package,
        task_id="parity-task",
    )
    context = SimpleNamespace(run_id=ids.run, model_id=ids.parent)
    return asyncio.run(Platform(task, context, SessionFactory).call("run_parity", {}, key))


def test_agent_and_human_run_the_same_parity_and_it_opens_gate_two(scoring):
    ids, state = scoring
    reviewer = {"Authorization": "Bearer parity-reviewer-token"}
    decision = {
        "artifact_id": None,
        "artifact_version": None,
        "decision": "accepted",
        "rationale": "Parity passed on the exact candidate",
    }
    with TestClient(app) as client:
        # The agent's first attempt is off on one row; the tool reports which.
        state["values"] = [REFERENCE[0], REFERENCE[1] + 1e-4, REFERENCE[2]]
        first = agent_parity(ids, "attempt-1")
        assert first.status == "ok", first.content
        first = json.loads(first.content)
        assert first["initiator"] == "agent" and not first["passed"]
        assert first["result"]["worst_rows"][0]["row_id"] == "uci-000002"

        # The agent fixes the candidate and the same check passes.
        state["values"] = REFERENCE
        second = json.loads(agent_parity(ids, "attempt-2").content)
        assert second["passed"] and second["rebuild_run_id"] == ids.run

        open_checkpoint(ids)
        review = client.get("/api/v1/reviews/" + ids.review).json()
        assert review["blockers"] == [] and review["approval_available"]
        assert review["candidate_model_id"] == ids.candidate

        # A person reruns parity and gets the same result from the same check.
        response = client.post(
            f"/api/v1/models/{ids.candidate}/versions/1.0.0-r1/parity"
        )
        assert response.status_code == 201, response.text
        human = response.json()
        assert human["initiator"] == "human" and human["passed"]
        assert human["result"] == second["result"]
        assert state["calls"] == 3

        fetched = client.get("/api/v1/parity-runs/" + human["id"])
        assert fetched.status_code == 200 and fetched.json()["id"] == human["id"]
        history = client.get(f"/api/v1/models/{ids.candidate}/parity-runs").json()
        assert [r["initiator"] for r in history] == ["human", "agent", "agent"]
        # The parent R model's history lists the same runs, for its Rebuild & parity tab.
        parent_history = client.get(f"/api/v1/models/{ids.parent}/parity-runs").json()
        assert [r["id"] for r in parent_history] == [r["id"] for r in history]

        decision.update(artifact_id=ids.artifact, artifact_version=ids.artifact_version)
        client.post(f"/api/v1/reviews/{ids.review}/open", headers=reviewer)
        response = client.post(
            f"/api/v1/reviews/{ids.review}/decision", headers=reviewer, json=decision
        )
        assert response.status_code == 200, response.text
        assert response.json()["resolution"]["decision"] == "accepted"


def test_gate_two_stays_blocked_until_parity_passes(scoring):
    ids, state = scoring
    reviewer = {"Authorization": "Bearer parity-reviewer-token"}
    open_checkpoint(ids)
    with TestClient(app) as client:
        review = client.get("/api/v1/reviews/" + ids.review).json()
        assert review["blockers"] == ["official_parity_not_passed"]
        assert review["approval_available"] is False
        decision = {
            "artifact_id": ids.artifact,
            "artifact_version": ids.artifact_version,
            "decision": "accepted",
            "rationale": "No parity yet",
        }
        response = client.post(
            f"/api/v1/reviews/{ids.review}/decision", headers=reviewer, json=decision
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "official_parity_not_passed"

        state["values"] = [0.0, 0.0, 0.0]
        failed = client.post(f"/api/v1/models/{ids.candidate}/versions/1.0.0-r1/parity")
        assert failed.status_code == 201 and not failed.json()["passed"]
        response = client.post(
            f"/api/v1/reviews/{ids.review}/decision", headers=reviewer, json=decision
        )
        assert response.status_code == 409

        # A pass recorded against different candidate content does not count.
        state["values"] = REFERENCE
        assert client.post(
            f"/api/v1/models/{ids.candidate}/versions/1.0.0-r1/parity"
        ).json()["passed"]

        async def change_bundle():
            async with SessionFactory() as session:
                record = await session.get(InventoryRecord, ids.candidate)
                record.bundle_sha256 = "9" * 64
                await session.commit()

        asyncio.run(change_bundle())
        review = client.get("/api/v1/reviews/" + ids.review).json()
        assert review["blockers"] == ["official_parity_not_passed"]

        # Requesting changes never needs parity.
        decision.update(decision="changes_requested", rationale="Fix the scorer")
        response = client.post(
            f"/api/v1/reviews/{ids.review}/decision", headers=reviewer, json=decision
        )
        assert response.status_code == 200, response.text


def test_scorer_failure_is_recorded_as_a_failed_run(scoring, monkeypatch):
    ids, _ = scoring

    async def broken(package, execution_id):
        raise legacy_scoring.ScoringError("Legacy scorer exited unsuccessfully.")

    monkeypatch.setattr(legacy_scoring, "score_snapshot", broken)
    with TestClient(app) as client:
        response = client.post(f"/api/v1/models/{ids.candidate}/versions/1.0.0-r1/parity")
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "failed" and not body["passed"]
        assert "unsuccessfully" in body["error"]


def test_parity_refuses_what_it_cannot_compare(scoring):
    ids, _ = scoring
    with TestClient(app) as client:
        parent = client.post(f"/api/v1/models/{ids.parent}/versions/1.0.0/parity")
        assert parent.status_code == 409
        assert parent.json()["detail"]["code"] == "parity_not_applicable"
        assert (
            client.post(f"/api/v1/models/{ids.candidate}/versions/9.9.9/parity")
        ).status_code == 409
        assert (
            client.post(f"/api/v1/models/{uuid4()}/versions/1.0.0/parity")
        ).status_code == 404
        assert client.get("/api/v1/parity-runs/" + str(uuid4())).status_code == 404


def test_checkpoint_saved_before_parity_existed_is_not_stuck(scoring):
    ids, _ = scoring
    open_checkpoint(ids, blockers=["official_parity_pending"])
    with TestClient(app) as client:
        assert client.get("/api/v1/reviews/" + ids.review).json()["blockers"] == [
            "official_parity_not_passed"
        ]
        assert client.post(
            f"/api/v1/models/{ids.candidate}/versions/1.0.0-r1/parity"
        ).json()["passed"]
        review = client.get("/api/v1/reviews/" + ids.review).json()
        assert review["blockers"] == [] and review["approval_available"]


def test_validation_report_cites_the_parity_run_on_the_exact_candidate(scoring):
    from app.services.live_validation import parity_check

    ids, state = scoring

    async def inventory(model_id):
        async with SessionFactory() as session:
            return await session.get(InventoryRecord, model_id)

    candidate = asyncio.run(inventory(ids.candidate))
    python = SimpleNamespace(language="Python")
    missing = asyncio.run(parity_check(python, candidate, candidate.manifest))
    assert missing["status"] == "NOT_ASSESSABLE" and missing["required"]
    with TestClient(app) as client:
        state["values"] = [0.0, 0.0, 0.0]
        client.post(f"/api/v1/models/{ids.candidate}/versions/1.0.0-r1/parity")
        failed = asyncio.run(parity_check(python, candidate, candidate.manifest))
        assert failed["status"] == "FAIL"
        state["values"] = REFERENCE
        passed = client.post(
            f"/api/v1/models/{ids.candidate}/versions/1.0.0-r1/parity"
        ).json()
    cited = asyncio.run(parity_check(python, candidate, candidate.manifest))
    assert cited["status"] == "PASS"
    assert cited["details"]["parity_run_id"] == passed["id"]
    parent = asyncio.run(inventory(ids.parent))
    legacy = asyncio.run(parity_check(SimpleNamespace(language="R"), parent, parent.manifest))
    assert legacy["status"] == "NOT_ASSESSABLE"
