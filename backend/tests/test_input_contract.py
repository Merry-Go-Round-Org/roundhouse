"""Rebuild input-contract probes: a rejection counts only after a clean control run."""

import asyncio
from types import SimpleNamespace

from app.services import input_contract


def _run(monkeypatch, tmp_path, exits):
    """Score with scripted exit codes, keyed by the probe each input represents."""
    (tmp_path / "package").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data/development_inputs.csv").write_text(
        "row_id,LIMIT_BAL\nr1,1\nr2,2\nr3,3\nr4,4\n"
    )
    seen = []

    async def score(root, columns, rows, language):
        ids = [r.get("row_id") for r in rows]
        kind = ("missing_row_id" if "row_id" not in columns else
                "target_leakage" if "default payment next month" in columns else
                "duplicated_row_id" if len(set(ids)) < len(ids) else "control")
        seen.append(kind)
        code = exits[kind]
        out = b"row_id,positive_probability\n" + b"".join(
            f"{i},0.5\n".encode() for i in ids) if kind == "control" else b""
        return code, out

    monkeypatch.setattr(input_contract, "verify_snapshot", lambda *_: tmp_path)
    monkeypatch.setattr(input_contract, "_score", score)
    package = SimpleNamespace(sha256="0" * 64, file_hashes={}, language="Python")
    return asyncio.run(input_contract.input_contract_checks(package, "uci_credit")), seen


def test_probes_pass_only_when_each_malformed_input_is_rejected(monkeypatch, tmp_path):
    result, seen = _run(monkeypatch, tmp_path, {"control": 0, "missing_row_id": 1,
                                                 "duplicated_row_id": 0, "target_leakage": 137})
    assert seen == ["control", "missing_row_id", "duplicated_row_id", "target_leakage"]
    assert result["control"]["status"] == "PASS"
    assert {c["check_id"]: c["status"] for c in result["checks"]} == {
        "reject_missing_row_id": "PASS",
        "reject_duplicated_row_id": "FAIL",
        # Killed by the timeout is not a rejection.
        "reject_target_leakage": "FAIL",
    }
    assert result["passed"] is False


def test_a_scorer_that_always_crashes_never_passes(monkeypatch, tmp_path):
    result, seen = _run(monkeypatch, tmp_path, {"control": 1, "missing_row_id": 1,
                                                 "duplicated_row_id": 1, "target_leakage": 1})
    assert seen == ["control"]
    assert result["control"]["status"] == "FAIL"
    assert {c["status"] for c in result["checks"]} == {"NOT_RUN"}
    assert result["passed"] is False


def test_a_scorer_that_enforces_its_contract_passes(monkeypatch, tmp_path):
    result, _ = _run(monkeypatch, tmp_path, {"control": 0, "missing_row_id": 1,
                                              "duplicated_row_id": 1, "target_leakage": 1})
    assert result["passed"] is True


def test_saved_evidence_is_bound_to_the_agent_parity_run_and_exact_bundle(monkeypatch, tmp_path):
    """Gate 2 reads the probe result saved with the agent's parity run (issue #32)."""
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app import main as entrypoint
    from app.config import settings
    from app.db import session as database
    from app.db.tables import ModelRecord
    from app.main import app
    from app.services.parity_service import ParityRunRecord
    from app.services.upload_service import InventoryRecord, UploadRecord

    url = f"sqlite+aiosqlite:///{tmp_path / 'evidence.db'}"
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    for target in (database, entrypoint):
        monkeypatch.setattr(target, "engine", engine)
        monkeypatch.setattr(target, "SessionFactory", sessions)
    monkeypatch.setattr(settings, "database_url", url)
    contract = {"passed": True, "control": {"status": "PASS", "exit_code": 0, "reason": "ok"},
                "checks": [{"check_id": k, "status": "PASS", "exit_code": 1, "reason": "Rejected"}
                           for k in input_contract.PROBES]}

    async def scenario():
        async with sessions() as session:
            parent = ModelRecord(name="src_" + uuid4().hex[:6], version="1.0.0", language="R",
                                 dataset_id="uci_credit", purpose="t")
            model = ModelRecord(name="cand_" + uuid4().hex[:6], version="1.0.0-rabc", language="Python",
                                dataset_id="uci_credit", purpose="t")
            upload = UploadRecord(id=str(uuid4()), actor_id="dev", archive_sha256="0" * 64)
            session.add_all([parent, model, upload])
            await session.flush()
            session.add(InventoryRecord(model_id=model.id, name=model.name, model_version=model.version,
                                        upload_id=upload.id, manifest={}, provenance={}, bundle_sha256="b" * 64))

            def parity(initiator, bundle="b" * 64):
                return ParityRunRecord(id=str(uuid4()), candidate_model_id=model.id, candidate_version=model.version,
                                       candidate_bundle_sha256=bundle, parent_model_id=parent.id, parent_version="1.0.0",
                                       rebuild_run_id="run-1", initiator=initiator, actor_id="dev", status="completed",
                                       passed=True, result={"method": "parity-v1"})

            agent, older_bundle = parity("agent"), parity("agent", "c" * 64)
            session.add_all([agent, older_bundle])
            await session.commit()
            assert await input_contract.gate_2_evidence(session, model.id) is None  # never inferred
        await input_contract.save_evidence(sessions, agent.id, contract)
        await input_contract.save_evidence(sessions, older_bundle.id, {**contract, "passed": False})
        async with sessions() as session:
            session.add(parity("human"))  # a person's rerun runs no probes
            await session.commit()
            evidence = await input_contract.gate_2_evidence(session, model.id)
            saved = await session.get(ParityRunRecord, agent.id)
        assert saved.result["method"] == "parity-v1"  # parity evidence untouched
        assert evidence["passed"] is True and evidence["parity_run_id"] == agent.id
        assert evidence["candidate_bundle_sha256"] == "b" * 64 and evidence["rebuild_run_id"] == "run-1"
        assert evidence["executed_at"] and len(evidence["checks"]) == 3

    with TestClient(app):
        asyncio.run(scenario())
    asyncio.run(engine.dispose())
