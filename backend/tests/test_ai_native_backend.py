"""AI-native runs through the backend: persisted run, real resolver, real harness runner.

The LLM is scripted and Docker is replaced at the process boundary, so the run record, input
registry, mount resolver, gateway, export checks, correction rounds, proposals and triggers all
run for real. `harness/dev/live_ai_native.py` runs the same path with a real LLM and Docker.
"""

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.auth_tables  # noqa: F401  users, for the actor check
import app.services.upload_service  # noqa: F401  inventory, for proposal preconditions
from app.db.tables import AuditRecord, Base
from app.services import ai_native_service as ai
from app.services.harness_adapter import mount_resolver
from contracts.harness import Actor
from harness.db import create_tables, enable_sqlite_pragmas
from harness.dev.gateway import ScriptedLlm, text_turn, tool_turn
from harness.handoff import EXAMPLE_EXPORTS, EXAMPLE_METRICS_BUNDLE
from harness.runner import HarnessRunner
from harness.sandbox import ProcessResult
from harness.config import HarnessSettings

DEVELOPER = Actor(id="local-operator", role="operator")
VALIDATOR = Actor(id="val-1", role="validator")
SURVEY = json.loads(EXAMPLE_EXPORTS["survey"]["survey.json"])
ARCHIVE = {
    "credit/model.rds": b"RDS",
    "credit/score.R": b"# score",
    "credit/notes.md": b"# notes",
    "README.txt": b"readme",
}
TRIAGE = json.loads(EXAMPLE_EXPORTS["triage"]["triage.json"])
TRIAGE_REPORT = EXAMPLE_EXPORTS["triage"]["triage_report.md"]
MONITORING = {"kind": "model_stability_window", "band": "amber", "input_scope": "uploaded_batch",
              "score": {"psi": 0.11, "band": "amber"}, "features": {"LIMIT_BAL": {"psi": 0.14}}}


class FakeDocker:
    """Accepts every Docker command and records the `docker run` mounts."""

    def __init__(self):
        self.runs = []

    async def run(self, argv, *, timeout_s, max_output, stdin=None):
        if "run" in argv[:3]:
            self.runs.append(argv)
        if "realpath" in argv:
            return ProcessResult(returncode=0, stdout=argv[-1] + "\n")
        return ProcessResult(returncode=0, stdout="output")

    def mounts(self):
        return [a for argv in self.runs for a in argv if a.startswith("type=bind")]


class Workspace:
    """What the agent wrote, served in order: each export of a path gets the next version."""

    def __init__(self, versions):
        self.versions = {path: list(items) for path, items in versions.items()}

    async def __call__(self, task_id, relative):
        items = self.versions[relative]
        files = items.pop(0) if len(items) > 1 else items[0]
        return dict(files)


def run(coro):
    return asyncio.run(coro)


class Backend:
    def __init__(self, tmp_path):
        self.url = f"sqlite+aiosqlite:///{tmp_path / 'ai.db'}"

    async def __aenter__(self):
        self.engine = create_async_engine(self.url)
        enable_sqlite_pragmas(self.engine)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(create_tables)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        return self

    async def __aexit__(self, *_):
        await self.engine.dispose()

    def runner(self, script, docker=None):
        self.docker = docker or FakeDocker()
        self.llm = ScriptedLlm(script)
        return HarnessRunner(
            self.sessions,
            self.llm,
            settings=HarnessSettings(retry_base_delay_s=0.0),
            resolve_mounts=mount_resolver(self.sessions),
            process_runner=self.docker,
        )

    async def execute(self, run_id, script, workspace):
        runner = self.runner(script)
        try:
            return await ai.execute_run(self.sessions, runner, run_id, read_workspace=workspace)
        finally:
            await runner.shutdown()

    async def record(self, run_id):
        async with self.sessions() as session:
            return await session.get(ai.AiNativeRunRecord, run_id)


async def survey_run(backend, **follow_up):
    archive = await ai.register_input(backend.sessions, "archive", ARCHIVE)
    data = await ai.register_input(backend.sessions, "data", {"uci_credit/train.csv": b"row_id\n"})
    run_id = await ai.create_run(
        backend.sessions, "survey", DEVELOPER, {"policy_version": "1.0.0", "archive_id": archive},
        follow_up={"development_data_id": data, **follow_up},
    )
    return run_id, archive, data


def test_a_survey_run_is_persisted_checked_and_queues_intake(tmp_path):
    async def main():
        async with Backend(tmp_path) as backend:
            run_id, archive, _ = await survey_run(backend)
            assert (await backend.record(run_id)).status == "queued"
            script = [tool_turn(("export_artifact", {"path": "survey.json"})), text_turn("Surveyed.")]
            result = await backend.execute(
                run_id, script, Workspace({"survey.json": [EXAMPLE_EXPORTS["survey"]]})
            )
            assert result.status == "completed", result
            assert len(result.artifact_ids) == 1 and result.proposal_ids == []
            record = await backend.record(run_id)
            assert record.status == "completed" and record.task_id == result.task_id
            assert record.usage["cost_usd"] > 0
            # Only the archive is mounted, read-only, at its place.
            mounts = backend.docker.mounts()
            assert any("dst=/inputs/archive" in m and "readonly" in m for m in mounts)
            assert not any("/inputs/data" in m or "/inputs/model" in m for m in mounts)
            # One intake per candidate, with the candidate as its prompt.
            (intake_id,) = record.outcome["intake_run_ids"]
            intake = await backend.record(intake_id)
            assert intake.status == "queued" and intake.source_run_id == run_id
            assert intake.context["candidate_id"] == "credit_glm"
            assert intake.context["archive_id"] == archive and "credit/score.R" in intake.prompt
            # Safe progress events only: no reply text, tool arguments or output.
            async with backend.sessions() as session:
                events = list(await session.scalars(select(AuditRecord).where(
                    AuditRecord.resource_id == run_id, AuditRecord.action == "ai_native_event")))
            types = [e.payload["type"] for e in events]
            assert "task_started" in types and "done" in types and "token" not in types
            assert all("Surveyed" not in json.dumps(e.payload) for e in events)
            assert all("survey.json" not in json.dumps(e.payload) for e in events)

    run(main())


def test_rejected_exports_get_a_correction_round_on_the_same_thread(tmp_path):
    bad = json.loads(json.dumps(SURVEY))
    bad["files"] = bad["files"][:-1]  # README.txt left out
    bad["unassigned_files"] = []

    async def main():
        async with Backend(tmp_path) as backend:
            run_id, _, _ = await survey_run(backend)
            script = [
                tool_turn(("export_artifact", {"path": "survey.json"})), text_turn("Done."),
                tool_turn(("export_artifact", {"path": "survey.json"})), text_turn("Fixed."),
            ]
            workspace = Workspace({"survey.json": [
                {"survey.json": json.dumps(bad).encode()}, EXAMPLE_EXPORTS["survey"],
            ]})
            result = await backend.execute(run_id, script, workspace)
            assert result.status == "completed", result
            record = await backend.record(run_id)
            assert record.correction_round == 1
            # The export tool told the agent at once, and the round prompt listed the problem.
            tool_messages = [m for r in backend.llm.requests for m in r.messages if m.get("role") == "tool"]
            assert any("cannot accept it yet" in str(m.get("content")) for m in tool_messages)
            last = backend.llm.requests[2].messages
            assert any("could not accept them" in str(m.get("content")) and "README.txt" in str(m.get("content")) for m in last)
            # Both rounds are one thread: the second continues the first task.
            assert len({r.messages[0]["content"] for r in backend.llm.requests}) == 1

    run(main())


def test_a_run_fails_clearly_after_the_last_correction_round(tmp_path):
    async def main():
        async with Backend(tmp_path) as backend:
            run_id, _, _ = await survey_run(backend)
            script = [text_turn("I did nothing.")] * 3
            result = await backend.execute(run_id, script, Workspace({}))
            assert result.status == "failed" and result.error_code == "export_rejected"
            assert result.artifact_ids == [] and "survey.json was not exported" in result.text
            record = await backend.record(run_id)
            assert record.status == "failed" and record.correction_round == 2
            assert record.outcome == {}

    run(main())


async def platform_monitor_run(backend):
    """A model, its monitor run and the monitoring record: what a proposal is bound to."""
    from uuid import uuid4
    from app.db.tables import ModelRecord, MonitoringRecord, runningRecord

    async with backend.sessions() as session:
        model = ModelRecord(name="model-" + uuid4().hex[:6], version="1", language="R",
                            dataset_id="uci_credit", purpose="Test")
        session.add(model)
        await session.flush()
        platform = runningRecord(model_id=model.id, model_version="1", dataset_id="uci_credit",
                                 operation="monitor", status="running")
        session.add(platform)
        await session.flush()
        event = MonitoringRecord(id=str(uuid4()), model_id=model.id, run_id=platform.id,
                                 payload=MONITORING)
        session.add(event)
        await session.commit()
        return model.id, platform.id, event.id


async def triage_run(backend, outcome="benign"):
    model_id, platform_id, _ = await platform_monitor_run(backend)
    monitoring = await ai.register_monitoring(backend.sessions, MONITORING)
    data = await ai.register_input(backend.sessions, "data", {"uci_credit/train.csv": b"row_id\n"})
    document = await ai.register_input(backend.sessions, "document", {"model_document.md": b"# doc"})
    report = await ai.register_input(backend.sessions, "reports", {"validation_report.json": b"{}"})
    return await ai.create_run(
        backend.sessions, "triage", DEVELOPER,
        {"policy_version": "1.0.0", "model_id": model_id, "monitoring_run_id": monitoring,
         "development_data_id": data},
        follow_up={"document_id": document, "validation_report_id": report},
        platform_run_id=platform_id,
    ), monitoring


BENIGN = {"type": "mark_drift_benign", "args": {"reason": "Seasonal mix."},
          "rationale": "PSI comes from one feature's seasonal mix.",
          "evidence": [{"kind": "monitoring_run", "ref": "this run"}]}


def test_a_benign_triage_records_a_pending_proposal_and_does_nothing_else(tmp_path):
    async def main():
        async with Backend(tmp_path) as backend:
            from fastapi import HTTPException
            from app.services import proposal_service as proposals

            run_id, _ = await triage_run(backend)
            async with backend.sessions() as session:
                link = await session.get(ai.AiNativeRunLink, run_id)
            script = [
                tool_turn(("export_artifact", {"path": "triage.json"}),
                          ("export_artifact", {"path": "triage_report.md"})),
                tool_turn(("create_proposal", BENIGN)),
                text_turn("Benign."),
            ]
            workspace = Workspace({
                "triage.json": [{"triage.json": EXAMPLE_EXPORTS["triage"]["triage.json"]}],
                "triage_report.md": [{"triage_report.md": TRIAGE_REPORT}],
            })
            result = await backend.execute(run_id, script, workspace)
            assert result.status == "completed", result
            (proposal_id,) = result.proposal_ids
            async with backend.sessions() as session:
                proposal = await session.get(proposals.ProposalRecord, proposal_id)
                events = list(await session.scalars(select(AuditRecord).where(
                    AuditRecord.resource_id == run_id, AuditRecord.action == "ai_native_event")))
                platform = await session.get(
                    __import__("app.db.tables", fromlist=["runningRecord"]).runningRecord,
                    link.platform_run_id)
            # One proposals table: bound to the platform run, its model and its monitoring record.
            assert proposal.status == "pending" and proposal.source_run_id == link.platform_run_id
            assert proposal.required_role == "validator"
            assert proposal.model_id == platform.model_id and proposal.preconditions["monitoring_id"]
            assert any(e.payload["type"] == "proposal_created" and e.payload["proposal_id"] == proposal_id for e in events)
            record = await backend.record(run_id)
            assert record.outcome == {"outcome": "benign"}
            # Only the triage inputs were mounted.
            mounts = backend.docker.mounts()
            assert any("dst=/inputs/monitoring" in m for m in mounts)
            assert any("dst=/inputs/data" in m for m in mounts)
            assert not any("/inputs/document" in m or "/inputs/reports" in m for m in mounts)
            # A person decides; the wrong role cannot, and a decision is made once.
            approve = proposals.Decision(decision="approved", rationale="Checked the evidence")
            async with backend.sessions() as session:
                with pytest.raises(HTTPException) as error:
                    await proposals.decide(session, proposal_id, approve, DEVELOPER)
                assert error.value.status_code == 403
                decided = await proposals.decide(session, proposal_id, approve, VALIDATOR)
                assert decided.status == "approved" and decided.decision["actor_id"] == "val-1"
                with pytest.raises(HTTPException) as error:
                    await proposals.decide(session, proposal_id, approve, VALIDATOR)
                assert error.value.status_code == 409

    run(main())


def test_a_problem_triage_queues_an_investigation_with_the_triage_report(tmp_path):
    problem = dict(TRIAGE, outcome="problem")

    async def main():
        async with Backend(tmp_path) as backend:
            run_id, monitoring = await triage_run(backend)
            script = [
                tool_turn(("export_artifact", {"path": "triage.json"}),
                          ("export_artifact", {"path": "triage_report.md"})),
                text_turn("A problem."),
            ]
            workspace = Workspace({
                "triage.json": [{"triage.json": json.dumps(problem).encode()}],
                "triage_report.md": [{"triage_report.md": TRIAGE_REPORT}],
            })
            result = await backend.execute(run_id, script, workspace)
            assert result.status == "completed", result
            record = await backend.record(run_id)
            investigation = await backend.record(record.outcome["investigation_run_id"])
            assert investigation.kind == "investigation" and investigation.status == "queued"
            assert investigation.context["monitoring_run_id"] != monitoring
            async with backend.sessions() as session:
                folder = (await session.get(ai.AiNativeInputRecord, investigation.context["monitoring_run_id"])).path
            from pathlib import Path

            assert (Path(folder) / "triage_report.md").read_bytes() == TRIAGE_REPORT
            assert (Path(folder) / "monitoring_summary.json").is_file()

    run(main())


def test_a_disallowed_proposal_never_reaches_the_backend(tmp_path):
    restrict = dict(BENIGN, type="restrict_use", args={"restriction": "x", "reason": "y"})

    async def main():
        async with Backend(tmp_path) as backend:
            run_id, _ = await triage_run(backend)
            script = [tool_turn(("create_proposal", restrict)), text_turn("Done.")] + [text_turn("Done.")] * 2
            await backend.execute(run_id, script, Workspace({}))
            from app.services.proposal_service import ProposalRecord

            async with backend.sessions() as session:
                assert list(await session.scalars(select(ProposalRecord))) == []

    run(main())


def test_a_wrong_input_kind_fails_the_run_as_sandbox_unavailable(tmp_path):
    async def main():
        async with Backend(tmp_path) as backend:
            document = await ai.register_input(backend.sessions, "document", {"doc.md": b"# d"})
            run_id = await ai.create_run(
                backend.sessions, "survey", DEVELOPER, {"policy_version": "1.0.0", "archive_id": document})
            result = await backend.execute(run_id, [text_turn("x")], Workspace({}))
            assert result.status == "failed" and result.error_code == "sandbox_unavailable"
            assert backend.llm.requests == []  # no tokens spent before the sandbox

    run(main())


def test_the_resolver_refuses_changed_inputs_and_a_metrics_folder_with_rows(tmp_path):
    from types import SimpleNamespace
    from pathlib import Path

    async def main():
        async with Backend(tmp_path) as backend:
            metrics = await ai.register_input(
                backend.sessions, "metrics", {"metrics_bundle.json": json.dumps(EXAMPLE_METRICS_BUNDLE).encode()})
            info = SimpleNamespace(metrics_bundle_id=metrics, model_package_id=None)
            mounts = await ai.input_mounts(backend.sessions, info)
            assert set(mounts) == {"metrics_path"}
            (Path(mounts["metrics_path"]) / "test.csv").write_text("row_id,label\n1,1\n")
            with pytest.raises(ai.AiNativeError, match="changed after it was registered"):
                await ai.input_mounts(backend.sessions, info)
            with pytest.raises(ai.AiNativeError, match="only metrics_bundle.json"):
                await ai.register_input(backend.sessions, "metrics", {
                    "metrics_bundle.json": json.dumps(EXAMPLE_METRICS_BUNDLE).encode(), "test.csv": b"x"})
            with pytest.raises(ai.AiNativeError, match="validation-partition simulation"):
                await ai.register_monitoring(
                    backend.sessions, {**MONITORING, "input_scope": "test_partition_simulation"},
                    batch=tmp_path / "x.csv")

    run(main())


def test_a_run_needs_its_context_and_lost_runs_fail_as_interrupted(tmp_path):
    async def main():
        async with Backend(tmp_path) as backend:
            with pytest.raises(ai.AiNativeError, match="archive_id"):
                await ai.create_run(backend.sessions, "survey", DEVELOPER, {"policy_version": "1.0.0"})
            run_id, _, _ = await survey_run(backend)
            async with backend.sessions() as session:
                record = await session.get(ai.AiNativeRunRecord, run_id)
                record.status, record.started_at = "running", ai.now() - timedelta(hours=2)
                await session.commit()
            assert await ai.recover_runs(backend.sessions, 3600) == 1
            record = await backend.record(run_id)
            assert (record.status, record.error_code) == ("failed", "interrupted")
            with pytest.raises(ai.AiNativeError, match="not queued"):
                await ai.execute_run(backend.sessions, None, run_id)

    run(main())


class CountingDocker(FakeDocker):
    """Counts task containers alive at once."""

    def __init__(self):
        super().__init__()
        self.active = self.peak = 0

    async def run(self, argv, *, timeout_s, max_output, stdin=None):
        if argv[1:3] == ["run", "--detach"]:
            self.active += 1
            self.peak = max(self.peak, self.active)
        if argv[1:3] == ["rm", "--force"]:
            self.active -= 1
        await asyncio.sleep(0.01)
        return await super().run(argv, timeout_s=timeout_s, max_output=max_output, stdin=stdin)


def test_concurrent_intake_runs_respect_the_container_llm_and_archive_limits(tmp_path):
    from harness.dev.gateway import DEFAULT_USAGE

    async def main():
        async with Backend(tmp_path) as backend:
            archive = await ai.register_input(backend.sessions, "archive", ARCHIVE)
            data = await ai.register_input(backend.sessions, "data", {"uci_credit/train.csv": b"row_id\n"})
            ids = [
                await ai.create_run(
                    backend.sessions, "intake", DEVELOPER,
                    {"policy_version": "1.0.0", "archive_id": archive, "candidate_id": "credit_glm",
                     "development_data_id": data},
                    prompt="Run intake for candidate credit_glm.",
                )
                for _ in range(3)
            ]
            llm_active = llm_peak = 0

            def script(request):
                nonlocal llm_active, llm_peak
                exported = any(m.get("role") == "tool" for m in request.messages)

                async def hold():
                    nonlocal llm_active, llm_peak
                    llm_active += 1
                    llm_peak = max(llm_peak, llm_active)
                    await asyncio.sleep(0.02)
                    llm_active -= 1

                turn = text_turn("Done.") if exported else tool_turn(("export_artifact", {"path": "intake.json"}))
                return [hold, *turn]

            docker = CountingDocker()
            runner = HarnessRunner(
                backend.sessions,
                ScriptedLlm(script),
                settings=HarnessSettings(
                    retry_base_delay_s=0.0, max_concurrent_sandboxes=1, llm_concurrency=1,
                    # Room for one intake (two turns), not for three.
                    archive_budget_usd=DEFAULT_USAGE.cost_usd * 3,
                ),
                resolve_mounts=mount_resolver(backend.sessions),
                process_runner=docker,
            )
            workspace = Workspace({"intake.json": [EXAMPLE_EXPORTS["intake"]]})
            try:
                results = await asyncio.gather(*(
                    ai.execute_run(backend.sessions, runner, run_id, read_workspace=workspace)
                    for run_id in ids
                ))
            finally:
                await runner.shutdown()
            assert docker.peak == 1 and llm_peak == 1
            statuses = sorted((r.status, r.error_code) for r in results)
            assert statuses[0] == ("completed", None), statuses
            assert ("failed", "budget_exhausted") in statuses
            completed = next(r for r in results if r.status == "completed")
            record = await backend.record(next(i for i, r in zip(ids, results) if r is completed))
            assert record.outcome["executable"] is True and "intended_use" in record.outcome["gate0_confirm"]

    run(main())
