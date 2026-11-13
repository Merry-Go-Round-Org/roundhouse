"""Contract/security checks for the new upload and authenticated review boundary."""

import asyncio
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.api.auth import hasher
from app.db.auth_tables import UserRecord
from app.db.session import SessionFactory
from app.services.upload_service import (
    validate_bundle,
    unpack_bundle,
    UploadError,
    process_upload,
)
from app.api import uploads


def account(role="admin"):
    name = "test-" + uuid4().hex

    async def create():
        async with SessionFactory() as session:
            user = UserRecord(
                id=str(uuid4()),
                username=name,
                role=role,
                password_hash=hasher.hash("test-password-long"),
            )
            session.add(user)
            await session.commit()
            return user.id

    return name, asyncio.run(create())


def dataset_files(version=None):
    raw = b"ID,LIMIT_BAL,SEX,AGE,default payment next month\n1,10,1,30,0\n2,20,2,40,1\n3,30,1,50,0\n4,40,2,60,1\n5,50,1,30,0\n6,60,2,40,1\n"
    split = b"row_id,split\nuci-000001,train\nuci-000002,train\nuci-000003,validation\nuci-000004,validation\nuci-000005,test\nuci-000006,test\n"
    descriptor = dict(
        dataset_id="uci_credit",
        name="Synthetic contract fixture",
        version=version or uuid4().hex,
        source_url="https://example.test/synthetic",
        license="Synthetic test only",
        sha256=hashlib.sha256(raw).hexdigest(),
        split_sha256=hashlib.sha256(split).hexdigest(),
        target_column="default payment next month",
        positive_class=1,
        row_id_rule="uci-{ID:06d}",
        row_count=6,
        partition_counts={"train": 2, "validation": 2, "test": 2},
        audit_only_columns=["SEX", "AGE"],
        source_filename="synthetic.csv",
    )
    return {
        "source": ("synthetic.csv", raw),
        "descriptor": ("dataset.json", json.dumps(descriptor).encode()),
        "split": ("split.csv", split),
    }


def test_example_env_supports_browser_origin(monkeypatch):
    """Regress the .env.sample override; fallback defaults alone are insufficient."""
    from app.config import Settings, BACKEND_DIR

    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    example = Settings(_env_file=BACKEND_DIR / ".env.example")
    assert "http://localhost:5173" in example.cors_origins
    assert "http://127.0.0.1:5173" in example.cors_origins
    assert "https://evil.example" not in example.cors_origins


def test_loopback_login_origin_alias_when_existing_env_lists_only_localhost(
    monkeypatch,
):
    from app.services.identity import configured_origins

    monkeypatch.setattr(settings, "cors_origins", ["http://localhost:5173"])
    assert "http://127.0.0.1:5173" in configured_origins()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "missing-user", "password": "invalid"},
            headers={"Origin": "http://127.0.0.1:5173"},
        )
        assert response.status_code == 401
        denied = client.post(
            "/api/v1/auth/login",
            json={"username": "missing-user", "password": "invalid"},
            headers={"Origin": "http://evil.example"},
        )
        assert denied.status_code == 403


def test_cookie_auth_permissions_and_revocation(monkeypatch):
    monkeypatch.setattr(settings, "allow_local_demo_auth", False)
    with TestClient(app) as client:
        name, _ = account()
        assert client.get("/api/v1/models").status_code == 401
        invalid = client.post(
            "/api/v1/auth/login", json={"username": name, "password": "wrong"}
        )
        assert invalid.status_code == 401 and "password_hash" not in invalid.text
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"username": name, "password": "test-password-long"},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        response = client.post(
            "/api/v1/auth/login",
            json={"username": name, "password": "test-password-long"},
            headers={"Origin": "http://127.0.0.1:5173"},
        )
        assert (
            response.status_code == 200 and "HttpOnly" in response.headers["set-cookie"]
        )
        identity = client.get("/api/v1/auth/me").json()
        assert identity["role"] == "admin"
        assert identity["username"] == name
        assert identity["active"] is True
        assert client.get("/api/v1/models").status_code == 200
        user = client.post(
            "/api/v1/auth/users",
            json={
                "username": "dev-" + uuid4().hex,
                "password": "developer-password",
                "role": "developer",
            },
        )
        assert user.status_code == 201 and "password" not in user.text
        assert (
            client.post(
                "/api/v1/auth/users/" + user.json()["id"] + "/disable"
            ).status_code
            == 200
        )
        assert client.post("/api/v1/auth/logout").status_code == 200
        assert client.get("/api/v1/models").status_code == 401
        name, _ = account("developer")
        client.post(
            "/api/v1/auth/login",
            json={"username": name, "password": "test-password-long"},
        )
        assert (
            client.post(
                "/api/v1/auth/users",
                json={"username": "no", "password": "123456789012", "role": "admin"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/reviews/nonexistent/decision",
                json={
                    "artifact_id": "x",
                    "artifact_version": "x",
                    "decision": "accepted",
                    "rationale": "test",
                },
            ).status_code
            == 403
        )


def test_dataset_versions_hashes_and_partition_separation(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    with TestClient(app) as client:
        files = dataset_files()
        result = client.post("/api/v1/datasets", files=files)
        assert result.status_code == 201, result.text
        value = result.json()
        identity = value["version_id"]
        folder = tmp_path / "datasets" / identity / "partitions"
        assert "uci-000005" not in (folder / "train.csv").read_text()
        assert "uci-000005" in (folder / "test.csv").read_text()
        assert (
            client.post("/api/v1/datasets", files=files).json()["version_id"]
            == identity
        )
        tampered = {
            **files,
            "source": ("synthetic.csv", files["source"][1] + b"7,7,1,20,1\n"),
        }
        assert client.post("/api/v1/datasets", files=tampered).status_code == 422
        descriptor = json.loads(files["descriptor"][1])
        descriptor["name"] = "changed identity"
        changed = {**files, "descriptor": ("dataset.json", json.dumps(descriptor))}
        assert client.post("/api/v1/datasets", files=changed).status_code == 422
        assert client.get("/api/v1/datasets/" + identity).json()["statistics"][
            "partition_counts"
        ] == {"train": 2, "validation": 2, "test": 2}


def test_zip_slip_and_symlink_rejected(tmp_path):
    for name, mode in [("../escape.py", 0), ("link.py", 0o120777 << 16)]:
        path = tmp_path / (uuid4().hex + ".zip")
        with zipfile.ZipFile(path, "w") as z:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode
            z.writestr(info, "unsafe")
        with pytest.raises(UploadError):
            unpack_bundle(path, tmp_path / uuid4().hex)


def test_missing_bundle_file_reports_relative_path_without_host_path(tmp_path):
    (tmp_path / "manifest.json").write_text("{}")
    with pytest.raises(UploadError) as error:
        validate_bundle(tmp_path)
    assert "schema.json" in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_historical_lowercase_intake_language_remains_readable():
    from app.db.tables import ModelRecord

    with TestClient(app) as client:
        async def seed():
            async with SessionFactory() as session:
                record = ModelRecord(name="old-intake-" + uuid4().hex, version="1", language="python", dataset_id="uci_credit", purpose="Demo only")
                session.add(record)
                await session.commit()
                return record.id

        identity = asyncio.run(seed())
        result = client.get(f"/api/v1/models/{identity}")
        assert result.status_code == 200
        assert result.json()["language"] == "Python"


def test_all_nine_builder_bundles_and_cox_applicability(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            sys.executable,
            str(root / "legacy_models/integration/build_upload_bundles.py"),
            "--out-dir",
            str(tmp_path / "bundles"),
        ],
        check=True,
        capture_output=True,
    )
    for path in (tmp_path / "bundles").glob("*.zip"):
        target = tmp_path / path.stem
        unpack_bundle(path, target)
        manifest, _ = validate_bundle(target)
        assert manifest["name"] == path.stem
    assert len(list((tmp_path / "bundles").glob("*.zip"))) == 9
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    monkeypatch.setattr(uploads, "publish_upload", lambda _: None)
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/models/uploads",
            files={
                "bundle": (
                    "cox.zip",
                    (tmp_path / "bundles/credit_cox_python.zip").read_bytes(),
                )
            },
        )
        assert result.status_code == 202, result.text
        asyncio.run(process_upload(result.json()["id"], SessionFactory))
        result = client.get("/api/v1/models/uploads/" + result.json()["id"]).json()
        assert result["status"] == "completed", result
        model = client.get("/api/v1/models/" + result["model_id"]).json()
        assert model["dataset_id"] is None
        assert model["inventory"]["applicability"]["validate"] == "NOT_APPLICABLE"
        response = client.post(
            "/api/v1/models/" + model["id"] + "/runs",
            json={
                "operation": "validate",
                "model_version": model["version"],
                "dataset_id": None,
            },
        )
        assert response.status_code == 409, response.text
        package = client.get("/api/v1/models/" + model["id"] + "/packages").json()[0]
        assert (
            client.post(
                "/api/v1/packages/" + package["id"] + "/scoring-runs"
            ).status_code
            == 409
        )
        # The synthetic Cox model is not a rebuild candidate: nothing to compare.
        response = client.post(
            "/api/v1/models/" + model["id"] + "/versions/" + model["version"] + "/parity"
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "parity_not_applicable"
        assert client.get("/api/v1/parity-runs/placeholder").status_code == 404


def test_authenticated_gate_three_independence_and_stale_versions(
    monkeypatch, tmp_path
):
    from app.db.tables import ModelRecord, ReviewRecord, RunResultRecord
    from app.services.artifact_service import save_artifact

    monkeypatch.setattr(settings, "allow_local_demo_auth", False)
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    with TestClient(app) as client:
        validator, validator_id = account("validator")
        approver, _ = account("approver")

        async def seed():
            async with SessionFactory() as session:
                model = ModelRecord(
                    name="Review contract fixture",
                    version="1",
                    language="Python",
                    dataset_id="uci_credit",
                    purpose="Synthetic test",
                )
                session.add(model)
                await session.flush()
                run = __import__(
                    "app.db.tables", fromlist=["runningRecord"]
                ).runningRecord(
                    model_id=model.id,
                    model_version="1",
                    dataset_id="uci_credit",
                    operation="validate",
                    status="awaiting_review",
                )
                session.add(run)
                await session.flush()
                artifact = await save_artifact(
                    session, run, b'{"fixture": true}', "test.json", "application/json"
                )
                review = ReviewRecord(
                    id=str(uuid4()),
                    run_id=run.id,
                    artifact_id=artifact.id,
                    artifact_version=artifact.version,
                    requested_by=validator_id,
                    blockers=[],
                )
                session.add(review)
                await session.flush()
                session.add(
                    RunResultRecord(run_id=run.id, payload={"checkpoint_id": review.id})
                )
                await session.commit()
                return review.id, dict(
                    artifact_id=artifact.id,
                    artifact_version=artifact.version,
                    decision="accepted",
                    rationale="Controlled fixture review",
                    conditions=[],
                )

        identity, body = asyncio.run(seed())
        client.post(
            "/api/v1/auth/login",
            json={"username": validator, "password": "test-password-long"},
        )
        assert (
            client.post(
                "/api/v1/reviews/" + identity + "/decision", json=body
            ).status_code
            == 403
        )
        client.post("/api/v1/auth/logout")
        client.post(
            "/api/v1/auth/login",
            json={"username": approver, "password": "test-password-long"},
        )
        assert (
            client.post(
                "/api/v1/reviews/" + identity + "/decision",
                json={**body, "artifact_version": "stale"},
            ).status_code
            == 409
        )
        assert client.post("/api/v1/reviews/" + identity + "/open").status_code == 200
        response = client.post("/api/v1/reviews/" + identity + "/decision", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["resolution"]["review_elapsed_seconds"] is not None
        assert (
            client.post(
                "/api/v1/reviews/" + identity + "/decision", json=body
            ).status_code
            == 409
        )


def test_document_changes_create_one_new_run_and_close_old_checkpoint(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession
    from harness.db import AgentTask
    from app.db.tables import (
        ModelRecord,
        runningRecord,
        ReviewRecord,
        RunResultRecord,
        RunExecutionRecord,
    )
    from app.services.artifact_service import save_artifact
    from app.services.review_service import decide
    from app.services.identity import Actor
    from app.schemas.reviews import ReviewDecision

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    original_get = AsyncSession.get

    async def get(session, entity, identity, *args, **kwargs):
        if entity is AgentTask and identity == "paused-document-fixture":
            return SimpleNamespace(status="awaiting_review")
        return await original_get(session, entity, identity, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", get)
    with TestClient(app):

        async def scenario():
            async with SessionFactory() as session:
                model = ModelRecord(
                    name="Revision contract fixture",
                    version="1",
                    language="R",
                    dataset_id="uci_credit",
                    purpose="Synthetic test",
                )
                session.add(model)
                await session.flush()
                run = runningRecord(
                    model_id=model.id,
                    model_version="1",
                    dataset_id="uci_credit",
                    operation="document",
                    status="awaiting_review",
                )
                session.add(run)
                await session.flush()
                artifact = await save_artifact(
                    session, run, b"# Fixture document", "model.md", "text/markdown"
                )
                review = ReviewRecord(
                    id=str(uuid4()),
                    run_id=run.id,
                    artifact_id=artifact.id,
                    artifact_version=artifact.version,
                    requested_by="test-author",
                    blockers=[],
                )
                session.add(review)
                await session.flush()
                session.add(
                    RunResultRecord(run_id=run.id, payload={"checkpoint_id": review.id})
                )
                session.add(
                    RunExecutionRecord(
                        run_id=run.id,
                        actor_id="test-author",
                        task_id="paused-document-fixture",
                    )
                )
                await session.commit()
                decision = ReviewDecision(
                    artifact_id=artifact.id,
                    artifact_version=artifact.version,
                    decision="changes_requested",
                    rationale="Explain the missing data limitations",
                )
                await decide(
                    session, review.id, decision, Actor("different-human", "validator")
                )
                await session.refresh(run)
                assert run.status == "failed" and run.stage == "changes_requested"
                rows = list(
                    await session.scalars(
                        select(runningRecord).where(runningRecord.model_id == model.id)
                    )
                )
                assert len(rows) == 2
                revision = next(r for r in rows if r.id != run.id)
                assert (
                    revision.status == "failed"
                    and revision.stage == "configuration_required"
                )

        asyncio.run(scenario())


def test_tunnel_origin_requires_operator_state_not_forwarded_headers(
    monkeypatch, tmp_path
):
    from starlette.requests import Request
    from app.services.identity import trusted_origins

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/auth/login",
            "root_path": "",
            "query_string": b"",
            "server": ("localhost", 8000),
            "headers": [
                (b"host", b"localhost:8000"),
                (b"x-forwarded-host", b"evil.example"),
            ],
        }
    )
    assert "https://evil.example" not in trusted_origins(request)
    state = tmp_path / "INTEGRATION_STATUS.json"
    state.write_text(
        json.dumps({"base_url": "https://approved.lhr.life", "tunnel_running": False})
    )
    assert "https://approved.lhr.life" not in trusted_origins(request)
    state.write_text(
        json.dumps({"base_url": "https://approved.lhr.life", "tunnel_running": True})
    )
    assert "https://approved.lhr.life" in trusted_origins(request)
    state.write_text(
        json.dumps({"base_url": "https://evil.example", "tunnel_running": True})
    )
    assert "https://evil.example" not in trusted_origins(request)


def test_admin_may_decide_gate_three_but_not_review_own_run(monkeypatch, tmp_path):
    from app.db.tables import ModelRecord, ReviewRecord, RunResultRecord, runningRecord
    from app.services.artifact_service import save_artifact

    monkeypatch.setattr(settings, "allow_local_demo_auth", False)
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    with TestClient(app) as client:
        admin, admin_id = account("admin")
        _, validator_id = account("validator")

        async def seed(requested_by):
            async with SessionFactory() as session:
                model = ModelRecord(
                    name="Admin review fixture " + uuid4().hex,
                    version="1",
                    language="Python",
                    dataset_id="uci_credit",
                    purpose="Synthetic test",
                )
                session.add(model)
                await session.flush()
                run = runningRecord(
                    model_id=model.id,
                    model_version="1",
                    dataset_id="uci_credit",
                    operation="validate",
                    status="awaiting_review",
                )
                session.add(run)
                await session.flush()
                artifact = await save_artifact(
                    session, run, b'{"fixture": true}', "test.json", "application/json"
                )
                review = ReviewRecord(
                    id=str(uuid4()),
                    run_id=run.id,
                    artifact_id=artifact.id,
                    artifact_version=artifact.version,
                    requested_by=requested_by,
                    blockers=[],
                )
                session.add(review)
                await session.flush()
                session.add(
                    RunResultRecord(run_id=run.id, payload={"checkpoint_id": review.id})
                )
                await session.commit()
                return review.id, dict(
                    artifact_id=artifact.id,
                    artifact_version=artifact.version,
                    decision="accepted",
                    rationale="Controlled fixture review",
                    conditions=[],
                )

        own, own_body = asyncio.run(seed(admin_id))
        other, other_body = asyncio.run(seed(validator_id))
        client.post(
            "/api/v1/auth/login",
            json={"username": admin, "password": "test-password-long"},
        )
        assert client.post("/api/v1/reviews/" + own + "/decision", json=own_body).status_code == 403
        response = client.post("/api/v1/reviews/" + other + "/decision", json=other_body)
        assert response.status_code == 200, response.text


def test_failed_model_upload_retry_reuses_saved_archive(monkeypatch, tmp_path):
    from app.services.upload_service import UploadRecord, upload_root

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    dispatched = []
    monkeypatch.setattr(uploads, "publish_upload", dispatched.append)
    with TestClient(app) as client:
        username, actor_id = account("developer")
        upload_id = str(uuid4())
        archive = upload_root(upload_id) / "bundle.zip"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"saved test archive")

        async def save():
            async with SessionFactory() as session:
                session.add(UploadRecord(
                    id=upload_id, status="failed", actor_id=actor_id,
                    archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                    checks=[], files=[], error="worker unavailable",
                ))
                await session.commit()

        asyncio.run(save())
        client.post("/api/v1/auth/login", json={
            "username": username, "password": "test-password-long",
        })
        response = client.post(f"/api/v1/models/uploads/{upload_id}/retry")
        assert response.status_code == 202, response.text
        assert response.json()["status"] == "queued"
        assert dispatched == [upload_id]
        assert client.post(f"/api/v1/models/uploads/{upload_id}/retry").status_code == 409
        asyncio.run(process_upload(upload_id, SessionFactory))
        finished = client.get(f"/api/v1/models/uploads/{upload_id}")
        assert finished.status_code == 200
        assert finished.json()["status"] == "failed"
        assert finished.json()["error"]


def test_archive_reads_are_scoped_to_owner(monkeypatch, tmp_path):
    from app.services.intake_service import ArchiveRecord

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    with TestClient(app) as client:
        first_name, first_id = account("developer")
        second_name, _ = account("developer")
        archive_id = str(uuid4())

        async def save():
            async with SessionFactory() as session:
                session.add(ArchiveRecord(
                    id=archive_id, actor_id=first_id,
                    sha256="0" * 64, status="failed", files=[],
                ))
                await session.commit()

        asyncio.run(save())
        client.post("/api/v1/auth/login", json={
            "username": second_name, "password": "test-password-long",
        })
        assert client.get("/api/v1/intake/archives").json() == []
        assert client.get(f"/api/v1/intake/archives/{archive_id}").status_code == 404
        client.post("/api/v1/auth/login", json={
            "username": first_name, "password": "test-password-long",
        })
        assert client.get("/api/v1/intake/archives").json()[0]["id"] == archive_id
        assert client.get(f"/api/v1/intake/archives/{archive_id}").status_code == 200


def test_artifact_download_checks_saved_hash(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from app.services.artifact_service import artifact_bytes

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    identity = str(uuid4())
    path = tmp_path / "artifacts" / identity / "content"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original evidence")
    record = SimpleNamespace(
        id=identity, media_type="text/plain", size=len(path.read_bytes()),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    assert asyncio.run(artifact_bytes(record)) == b"original evidence"
    path.write_bytes(b"altered evidence!")
    with pytest.raises(HTTPException) as error:
        asyncio.run(artifact_bytes(record))
    assert error.value.status_code == 409


def test_dataset_upload_uses_verified_canonical_split(monkeypatch, tmp_path):
    files = dataset_files()
    legacy_root = tmp_path / "legacy"
    canonical = legacy_root / "evidence" / "uci_split_ids.csv"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(files["split"][1])
    monkeypatch.setattr(settings, "legacy_models_root", legacy_root)
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    without_split = {key: value for key, value in files.items() if key != "split"}
    with TestClient(app) as client:
        response = client.post("/api/v1/datasets", files=without_split)
        assert response.status_code == 201, response.text
        assert response.json()["dataset"]["split_sha256"] == hashlib.sha256(canonical.read_bytes()).hexdigest()
        canonical.write_bytes(b"tampered")
        changed = dataset_files()
        changed.pop("split")
        assert client.post("/api/v1/datasets", files=changed).status_code == 422


def test_gate_three_limits_require_named_conditions_and_failures_block(monkeypatch, tmp_path):
    from app.db.tables import ModelRecord, ReviewRecord, RunResultRecord, ReportRecord, runningRecord
    from app.services.artifact_service import save_artifact

    monkeypatch.setattr(settings, "allow_local_demo_auth", False)
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    with TestClient(app) as client:
        _, validator_id = account("validator")
        approver, _ = account("approver")

        async def seed(status):
            async with SessionFactory() as session:
                model = ModelRecord(name="Gate 3 fixture " + uuid4().hex,
                    version="1", language="Python", dataset_id="uci_credit", purpose="Test")
                session.add(model)
                await session.flush()
                run = runningRecord(model_id=model.id, model_version="1", dataset_id="uci_credit",
                                    operation="validate", status="awaiting_review")
                session.add(run)
                await session.flush()
                artifact = await save_artifact(session, run, b'{}', "report.json", "application/json")
                check_id = "split_provenance" if status == "REVIEW" else "reject_target_leakage"
                report_id = str(uuid4())
                session.add(ReportRecord(id=report_id, model_id=model.id, run_id=run.id,
                    kind="validation", artifact_id=artifact.id,
                    payload={"checks": [{"check_id": check_id, "status": status,
                                        "required": True, "summary": "Fixture"}]}))
                review = ReviewRecord(id=str(uuid4()), run_id=run.id, artifact_id=artifact.id,
                    artifact_version=artifact.version, requested_by=validator_id, blockers=[check_id])
                session.add(review)
                session.add(RunResultRecord(run_id=run.id, payload={"checkpoint_id": review.id}))
                await session.commit()
                return review.id, artifact.id, artifact.version, report_id

        limitation_id, artifact_id, version, report_id = asyncio.run(seed("REVIEW"))
        failure_id, failed_artifact, failed_version, _ = asyncio.run(seed("FAIL"))
        client.post("/api/v1/auth/login", json={"username": approver,
                                                "password": "test-password-long"})
        limitation = client.get(f"/api/v1/reviews/{limitation_id}").json()
        assert limitation["hard_blockers"] == []
        assert limitation["limitations"] == ["split_provenance"]
        assert "accepted_with_conditions" in limitation["allowed_decisions"]
        body = {"artifact_id": artifact_id, "artifact_version": version,
                "decision": "accepted_with_conditions", "rationale": "Restricted demonstration use"}
        assert client.post(f"/api/v1/reviews/{limitation_id}/decision",
                           json={**body, "conditions": ["Inspect provenance"]}).status_code == 422
        assert client.post(f"/api/v1/reviews/{limitation_id}/decision",
                           json={**body, "decision": "accepted", "conditions": []}).status_code == 409
        accepted = client.post(f"/api/v1/reviews/{limitation_id}/decision",
            json={**body, "conditions": ["split_provenance: demonstration only"]})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["resolution"]["conditions"] == ["split_provenance: demonstration only"]
        report = client.get(f"/api/v1/reports/{report_id}")
        assert report.status_code == 200
        assert report.json()["review_resolution"]["conditions"] == ["split_provenance: demonstration only"]
        assert report.json()["evidence"]["checks"][0]["status"] == "REVIEW"
        markdown = client.get(f"/api/v1/reports/{report_id}/markdown")
        assert markdown.status_code == 200
        assert "split_provenance: demonstration only" in markdown.text
        failure = client.get(f"/api/v1/reviews/{failure_id}").json()
        assert failure["hard_blockers"] == ["reject_target_leakage"]
        assert "accepted_with_conditions" not in failure["allowed_decisions"]
        refused = client.post(f"/api/v1/reviews/{failure_id}/decision", json={
            "artifact_id": failed_artifact, "artifact_version": failed_version,
            "decision": "accepted_with_conditions", "rationale": "Cannot waive failure",
            "conditions": ["reject_target_leakage: fix scorer"],
        })
        assert refused.status_code == 409
