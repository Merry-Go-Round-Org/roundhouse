"""Registry boundaries and development scoring lifecycle, with no live credentials."""

import asyncio
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.services import package_service as packages, legacy_scoring as scoring


def test_snapshot_integrity_and_minimal_data(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    manifest, fingerprint, hashes, rows = packages.snapshot_package("credit_logistic_r")
    assert rows == 100
    assert manifest["dataset_id"] == "uci_credit"
    assert not any(
        "reference_predictions" in p or "test" in p or "train" in p for p in hashes
    )
    root = packages.verify_snapshot(fingerprint, hashes)
    (root / "package/score.R").write_text("tampered")
    with pytest.raises(packages.PackageError, match="changed"):
        packages.verify_snapshot(fingerprint, hashes)
    with pytest.raises(packages.PackageError):
        packages.snapshot_package("../../.env")


def test_container_boundaries_and_output_validation():
    command = scoring.create_command(
        "test", "sha256:" + "a" * 64, "/tmp/snapshot", "Python"
    )
    for flag in ["--network=none", "--read-only", "--cap-drop=ALL", "--user=1000:1000"]:
        assert flag in command
    assert "type=bind,src=/tmp/snapshot,dst=/inputs,readonly" in command
    assert not any("OPENROUTER" in item for item in command)
    good = b"row_id,positive_probability\na,0.2\nb,0.8\n"
    assert scoring.validate_predictions(good, ["a", "b"]) == good
    for bad in [
        good.replace(b"0.2", b"nan"),
        good.replace(b"0.8", b"1.2"),
        good.replace(b"b,", b"a,"),
    ]:
        with pytest.raises(scoring.ScoringError):
            scoring.validate_predictions(bad, ["a", "b"])
    from app.config import FROZEN_SCORING_IMAGE

    frozen = scoring.create_command(
        "test", FROZEN_SCORING_IMAGE, "/tmp/snapshot", "Python"
    )
    assert FROZEN_SCORING_IMAGE in frozen and "--pull=never" in frozen
    for image in [
        "mutable:latest",
        "ghcr.io/merry-go-round-org/sandbox:latest",
        FROZEN_SCORING_IMAGE[:-1] + "f",
    ]:
        with pytest.raises(scoring.ScoringError):
            scoring.create_command("test", image, "/tmp/snapshot", "Python")


def test_package_api_and_scoring_lifecycle(monkeypatch):
    from app.workers.tasks import execute_scoring
    from app.db.session import SessionFactory

    sent = []
    monkeypatch.setattr(
        execute_scoring, "apply_async", lambda **kwargs: sent.append(kwargs)
    )
    with TestClient(app) as client:
        model = client.post(
            "/api/v1/models",
            json={
                "name": "Registered credit",
                "version": "1.0.0",
                "dataset_id": "uci_credit",
                "language": "R",
                "purpose": "Development scoring",
            },
        ).json()
        path = f"/api/v1/models/{model['id']}/packages"
        created = client.post(path, json={"package_name": "credit_logistic_r"})
        assert created.status_code == 201, created.text
        package = created.json()
        assert (
            client.post(path, json={"package_name": "credit_logistic_r"}).json()["id"]
            == package["id"]
        )
        assert client.get(path).json()[0]["sha256"] == package["sha256"]
        scoring_path = f"/api/v1/packages/{package['id']}/scoring-runs"
        monkeypatch.setattr(settings, "scoring_image", None)
        assert client.post(scoring_path).status_code == 503
        monkeypatch.setattr(settings, "scoring_image", "sha256:" + "a" * 64)
        accepted = client.post(scoring_path)
        assert accepted.status_code == 202, accepted.text
        run_id = accepted.json()["id"]
        calls = []

        async def successful(package, execution_id):
            calls.append(execution_id)
            return {"scope": "development_fixture_scoring", "n_rows": 100}

        monkeypatch.setattr(scoring, "score_snapshot", successful)

        async def process():
            await scoring.process_scoring(run_id, SessionFactory)
            await scoring.process_scoring(run_id, SessionFactory)

        asyncio.run(process())
        result = client.get(f"/api/v1/scoring-runs/{run_id}").json()
        assert result["status"] == "completed"
        assert result["started_at"] and result["finished_at"]
        assert calls == [run_id]

        async def failure(package, execution_id):
            raise RuntimeError("SECRET")

        monkeypatch.setattr(scoring, "score_snapshot", failure)
        run_id = client.post(scoring_path).json()["id"]
        asyncio.run(scoring.process_scoring(run_id, SessionFactory))
        result = client.get(f"/api/v1/scoring-runs/{run_id}").json()
        assert result["status"] == "failed" and "SECRET" not in result["error"]


def test_input_labels_and_symlink_rejected(tmp_path):
    inputs = tmp_path / "input.csv"
    inputs.write_text("row_id,feature,Class\na,1,0\n")
    with pytest.raises(packages.PackageError):
        packages.read_inputs(inputs, ["feature"])
    (tmp_path / "linked.csv").symlink_to(inputs)
    with pytest.raises(packages.PackageError):
        packages.checked_file(tmp_path, "linked.csv")


def test_registration_rejects_supplied_code_hash_mismatch(monkeypatch, tmp_path):
    import shutil
    import json

    source = settings.legacy_models_root
    root = tmp_path / "legacy"
    name = "credit_logistic_python"
    shutil.copytree(source / name, root / name)
    (root / "shared").mkdir()
    for file in ["score_python.py", "pipeline.py"]:
        shutil.copyfile(source / "shared" / file, root / "shared" / file)
    manifest_path = root / name / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["shared_pipeline_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(settings, "legacy_models_root", root)
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    with pytest.raises(packages.PackageError, match="Code checksum mismatch"):
        packages.snapshot_package(name)


def test_demo_history_download_and_persistence(monkeypatch, tmp_path):
    import hashlib
    from app.db.session import SessionFactory
    from app.workers.tasks import execute_scoring

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "scoring_image", "sha256:" + "a" * 64)
    monkeypatch.setattr(execute_scoring, "apply_async", lambda **kwargs: None)
    content = b"row_id,positive_probability\nexample,0.25\n"

    async def score(package, run_id):
        relative = f"scoring/{run_id}/predictions.csv"
        path = tmp_path / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        return {
            "package_id": package.id,
            "prediction_reference": relative,
            "predictions_sha256": hashlib.sha256(content).hexdigest(),
            "n_rows": 1,
            "scope": "development_fixture_scoring",
        }

    monkeypatch.setattr(scoring, "score_snapshot", score)
    with TestClient(app) as client:
        model = client.post(
            "/api/v1/models",
            json={
                "name": "Download test",
                "version": "1.0.0",
                "language": "R",
                "dataset_id": "uci_credit",
                "purpose": "Test journey",
            },
        ).json()
        model_path = f"/api/v1/models/{model['id']}/packages"
        package = client.post(
            model_path, json={"package_name": "credit_logistic_r"}
        ).json()
        history_path = f"/api/v1/packages/{package['id']}/scoring-runs"
        assert client.get(history_path).json() == []
        assert client.get(history_path + "?limit=0").status_code == 422
        assert client.get(history_path + "?offset=-1").status_code == 422
        assert client.get("/api/v1/packages/missing/scoring-runs").status_code == 404
        run = client.post(history_path).json()
        path = f"/api/v1/scoring-runs/{run['id']}"
        assert run["created_at"].endswith("+00:00")
        assert client.get(path + "/predictions").status_code == 409
        asyncio.run(scoring.process_scoring(run["id"], SessionFactory))
        response = client.get(
            path + "/predictions", headers={"Origin": "http://localhost:5173"}
        )
        assert response.status_code == 200 and response.content == content
        assert response.headers["content-type"].startswith("text/csv")
        assert run["id"] in response.headers["content-disposition"]
        assert (
            "Content-Disposition" in response.headers["access-control-expose-headers"]
        )
        assert client.get(history_path).json()[0]["status"] == "completed"

        # Dispatch errors persist as a visible failed history entry, without secrets.
        def unavailable(**kwargs):
            raise RuntimeError("SECRET")

        monkeypatch.setattr(execute_scoring, "apply_async", unavailable)
        failed = client.post(history_path)
        assert failed.status_code == 503
        failed_id = failed.json()["detail"]["run_id"]
        assert (
            client.get(f"/api/v1/scoring-runs/{failed_id}").json()["status"] == "failed"
        )
        assert client.get(history_path + "?limit=1").json()[0]["id"] == failed_id
        assert (
            client.get(history_path + "?limit=1&offset=1").json()[0]["id"] == run["id"]
        )
    # New app lifecycle, same persistent database: browser can rediscover IDs.
    with TestClient(app) as client:
        assert client.get(model_path).json()[0]["id"] == package["id"]
        assert len(client.get(history_path).json()) == 2
        assert client.get(path + "/predictions").content == content
        artifact = tmp_path / f"scoring/{run['id']}/predictions.csv"
        artifact.write_bytes(b"altered")
        assert client.get(path + "/predictions").status_code == 409
        artifact.unlink()
        assert client.get(path + "/predictions").status_code == 404
        artifact.symlink_to(tmp_path / "outside.csv")
        (tmp_path / "outside.csv").write_bytes(content)
        assert client.get(path + "/predictions").status_code == 404
        assert client.get("/api/v1/scoring-runs/missing/predictions").status_code == 404
