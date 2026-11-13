"""Opt-in local integration: real Docker/UCI; scripted provider, no API key/cost.
From repository root: backend/.venv/bin/python backend/tests/live_workflow_check.py
UCI_SOURCE may point to the original XLS. Uses disposable storage, never runtime DB.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from contextlib import asynccontextmanager

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
source = Path(
    os.environ.get(
        "UCI_SOURCE", str(Path.home() / "Downloads/default of credit card clients.xls")
    )
)
if not source.is_file():
    raise SystemExit("Set UCI_SOURCE to original XLS")
with tempfile.TemporaryDirectory(prefix="backend-live-check-") as temporary:
    temp = Path(temporary).resolve()
    os.environ.update(
        STORAGE_DIR=str(temp / "storage"),
        DATABASE_URL="sqlite+aiosqlite:///" + str(temp / "app.db"),
        OPENROUTER_API_KEY="",
        ALLOW_LOCAL_DEMO_AUTH="true",
        ALLOW_LEGACY_REGISTRATION="false",
        REVIEWER_TOKEN="offline-reviewer-test",
        REVIEWER_ID="independent-test-reviewer",
        SCORING_IMAGE="ghcr.io/merry-go-round-org/sandbox@sha256:8083453253c3267caaa0fe36b43a287b9a986d5784148ad40714cd9d3fc2d80e",
        RUN_TIMEOUT_SECONDS="900",
    )
    from fastapi.testclient import TestClient
    from app.main import app
    from app.db.session import SessionFactory
    from app.api import uploads
    from app.services import upload_service, harness_adapter, run_service
    from app.workers.tasks import process_run
    from harness.runner import HarnessRunner
    from harness.dev.gateway import ScriptedLlm, tool_turn, text_turn
    from harness.sandbox import SandboxConfig
    from harness.document_template import DOCUMENT_TEMPLATE
    from app.db.tables import RunExecutionRecord, runningRecord
    from app.services.monitoring_service import MonitorJob, execute_monitor
    from uuid import uuid4

    uploads.publish_upload = lambda _: None
    run_service.publish_run = lambda _: None
    document = "\n".join(
        "Controlled integration fixture; evidence content needs human authoring."
        if line.startswith("> Guide:")
        else line
        for line in DOCUMENT_TEMPLATE.splitlines()
    )

    candidate = """import argparse,csv
parser=argparse.ArgumentParser();parser.add_argument('--input');parser.add_argument('--output');args=parser.parse_args()
with open(args.input) as f: rows=list(csv.DictReader(f))
with open(args.output,'w') as f:
 w=csv.writer(f);w.writerow(['row_id','positive_probability'])
 for row in rows: w.writerow([row['row_id'],0.5])
"""

    # The fix: parameters extracted from the fitted R object inside the sandbox, then
    # replayed in plain Python. Standard library only, and it never loads model.rds.
    extract = (
        "m<-readRDS('/inputs/model/model.rds');"
        "jsonlite::write_json(list(features=m$features,center=as.numeric(m$center),"
        "scale=as.numeric(m$scale),coefficients=as.numeric(m$fit$coefficients)),"
        "'candidate/parameters.json',digits=I(17))"
    )
    fixed = """import argparse,csv,json,math,os
parser=argparse.ArgumentParser();parser.add_argument('--input');parser.add_argument('--output');args=parser.parse_args()
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'parameters.json')) as f: p=json.load(f)
features,center,scale,b=p['features'],p['center'],p['scale'],p['coefficients']
def logistic(x):
 return 1/(1+math.exp(-x)) if x>=0 else math.exp(x)/(1+math.exp(x))
with open(args.input) as f: rows=list(csv.DictReader(f))
with open(args.output,'w',newline='') as f:
 w=csv.writer(f);w.writerow(['row_id','positive_probability'])
 for row in rows:
  eta=b[0]
  for i,name in enumerate(features): eta+=b[i+1]*((float(row[name])-center[i])/scale[i])
  w.writerow([row['row_id'],repr(logistic(eta))])
"""
    rebuild_steps = [
        ("sandbox_write_file", {"path": "candidate/score.py", "content": candidate}),
        ("export_artifact", {"path": "candidate"}),
        ("run_parity", {}),  # fails: the first candidate is deliberately wrong
        ("sandbox_exec", {"argv": ["Rscript", "-e", extract], "timeout_s": 60}),
        ("sandbox_write_file", {"path": "candidate/score.py", "content": fixed}),
        ("export_artifact", {"path": "candidate"}),
        ("run_parity", {}),  # passes: the fixed candidate replays the fitted R model
        ("request_review", {"summary": "Official parity passed on the second candidate"}),
    ]

    @asynccontextmanager
    async def controlled(sessions):
        step = {"n": 0}

        def choose(req):
            prompt = " ".join(
                str(m.get("content", ""))
                for m in req.messages
                if m.get("role") == "user"
            )
            if "Keep the exported artifact unchanged" in prompt:
                return text_turn("Human review recorded.")
            if "Rebuild the fitted R model" in prompt:
                step["n"] += 1
                return tool_turn(rebuild_steps[min(step["n"], len(rebuild_steps)) - 1])
            step["n"] += 1
            if step["n"] == 1:
                return tool_turn(
                    (
                        "sandbox_write_file",
                        {"path": "model_document.md", "content": document},
                    )
                )
            if step["n"] == 2:
                return tool_turn(("export_artifact", {"path": "model_document.md"}))
            return tool_turn(
                (
                    "request_review",
                    {"summary": "Controlled integration artifact; not real AI output"},
                )
            )

        runner = HarnessRunner(
            sessions,
            ScriptedLlm(choose),
            settings=harness_adapter.harness_settings(),
            resolve_mounts=harness_adapter.mount_resolver(sessions),
            sandbox_config=SandboxConfig(image=os.environ["SCORING_IMAGE"]),
        )
        try:
            yield runner
        finally:
            await runner.shutdown()

    harness_adapter.runtime = controlled
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "legacy_models/integration/build_upload_bundles.py"),
            "--out-dir",
            str(temp / "bundles"),
        ],
        check=True,
        capture_output=True,
    )
    with TestClient(app) as client:
        with (
            source.open("rb") as original,
            (ROOT / "legacy_models/dataset_descriptors/uci/dataset.json").open(
                "rb"
            ) as descriptor,
            (ROOT / "legacy_models/evidence/uci_split_ids.csv").open("rb") as split,
        ):
            response = client.post(
                "/api/v1/datasets",
                files={
                    "source": (source.name, original),
                    "descriptor": ("dataset.json", descriptor),
                    "split": ("split.csv", split),
                },
            )
        assert response.status_code == 201, response.text
        print(
            "DATASET original UCI: 30000 rows, hashes and disjoint partitions verified",
            flush=True,
        )
        models = {}
        for name in (
            "credit_logistic_r",
            "credit_logistic_python",
            "credit_xgboost_python",
            "credit_lightgbm_python",
            "credit_cox_python",
        ):
            response = client.post(
                "/api/v1/models/uploads",
                files={
                    "bundle": (
                        name + ".zip",
                        (temp / "bundles" / f"{name}.zip").read_bytes(),
                    )
                },
            )
            assert response.status_code == 202, response.text
            upload_id = response.json()["id"]
            asyncio.run(upload_service.process_upload(upload_id, SessionFactory))
            result = client.get("/api/v1/models/uploads/" + upload_id).json()
            print("UPLOAD", name, result["status"], result.get("error"), flush=True)
            assert result["status"] == "completed", result
            models[name] = client.get("/api/v1/models/" + result["model_id"]).json()
        model = models["credit_logistic_r"]
        response = client.post(
            "/api/v1/models/" + model["id"] + "/runs",
            json={
                "operation": "document",
                "dataset_id": "uci_credit",
                "model_version": model["version"],
            },
        )
        assert response.status_code == 202, response.text
        identity = response.json()["id"]
        asyncio.run(process_run(identity, SessionFactory))
        state = client.get("/api/v1/runs/" + identity).json()
        assert state["status"] == "awaiting_review", state
        review = next(
            r for r in client.get("/api/v1/reviews").json() if r["run_id"] == identity
        )
        headers = {"Authorization": "Bearer offline-reviewer-test"}
        response = client.post(
            "/api/v1/reviews/" + review["id"] + "/open", headers=headers
        )
        assert response.status_code == 200, response.text
        response = client.post(
            "/api/v1/reviews/" + review["id"] + "/decision",
            headers=headers,
            json={
                "artifact_id": review["artifact_id"],
                "artifact_version": review["artifact_version"],
                "decision": "accepted",
                "rationale": "Scripted integration fixture, never model approval",
            },
        )
        assert response.status_code == 200, response.text
        response = client.post(
            "/api/v1/runs/" + identity + "/resume", json={"review_id": review["id"]}
        )
        assert response.status_code == 202, response.text
        asyncio.run(process_run(identity, SessionFactory))
        assert client.get("/api/v1/runs/" + identity).json()["status"] == "completed"
        print(
            "DOCUMENT real harness/Docker export, independent review and resume: passed (scripted provider)",
            flush=True,
        )
        response = client.post(
            "/api/v1/models/" + model["id"] + "/runs",
            json={
                "operation": "validate",
                "dataset_id": "uci_credit",
                "model_version": model["version"],
            },
        )
        assert response.status_code == 202, response.text
        identity = response.json()["id"]
        asyncio.run(process_run(identity, SessionFactory))
        state = client.get("/api/v1/runs/" + identity).json()
        assert state["status"] == "awaiting_review", state
        reports = client.get("/api/v1/models/" + model["id"] + "/reports").json()
        report = next(r for r in reports if r["kind"] == "validation")
        payload = client.get(
            "/api/v1/artifacts/" + report["artifact_id"] + "/download"
        ).json()
        assert payload["saved_metrics"]["source"] == "fresh_backend_execution"
        assert payload["saved_metrics"]["test"]["rows"] == 5982
        test_metrics = payload["saved_metrics"]["fresh_evaluation"]["test"]
        assert 0 <= test_metrics["ks"] <= 1
        assert len(test_metrics["uncertainty"]["ks_ci95"]) == 2
        assert len(test_metrics["uncertainty"]["gini_ci95"]) == 2
        print(
            "VALIDATION fresh scores/metrics + UCI fairness + shared PSI; blockers:",
            payload["summary"]["blocking_check_ids"],
            flush=True,
        )
        review = next(
            r for r in client.get("/api/v1/reviews").json() if r["run_id"] == identity
        )
        response = client.post(
            "/api/v1/reviews/" + review["id"] + "/decision",
            headers=headers,
            json={
                "artifact_id": review["artifact_id"],
                "artifact_version": review["artifact_version"],
                "decision": "accepted",
                "rationale": "Should remain blocked",
            },
        )
        assert response.status_code == 409, response.text

        response = client.post(
            "/api/v1/models/" + model["id"] + "/runs",
            json={
                "operation": "rebuild",
                "dataset_id": "uci_credit",
                "model_version": model["version"],
            },
        )
        assert response.status_code == 202, response.text
        rebuild = response.json()["id"]
        asyncio.run(process_run(rebuild, SessionFactory))
        rebuilt = client.get("/api/v1/runs/" + rebuild).json()
        assert rebuilt["status"] == "awaiting_review", rebuilt
        gate = next(
            r for r in client.get("/api/v1/reviews").json() if r["run_id"] == rebuild
        )
        assert gate["gate"] == 2 and gate["blockers"] == [], gate
        assert gate["approval_available"] is True, gate
        history = client.get("/api/v1/models/" + model["id"] + "/parity-runs").json()
        assert [(r["initiator"], r["passed"]) for r in history] == [
            ("agent", True),
            ("agent", False),
        ], history
        passing, failing = history
        assert passing["candidate_model_id"] == gate["candidate_model_id"], history
        assert failing["candidate_model_id"] != passing["candidate_model_id"], history
        assert passing["result"]["n_rows"] == 100, passing
        assert passing["result"]["n_above_tolerance"] == 0, passing

        def rerun(run):
            return client.post(
                "/api/v1/models/"
                + run["candidate_model_id"]
                + "/versions/"
                + run["candidate_version"]
                + "/parity"
            ).json()

        # A person reruns the same check and gets the agent's results, pass and fail alike.
        for agent_run in (passing, failing):
            human = rerun(agent_run)
            assert human["initiator"] == "human", human
            assert human["passed"] == agent_run["passed"], (human, agent_run)
            for key in ("max_absolute_difference", "candidate_predictions_sha256",
                        "reference_predictions_sha256", "n_above_tolerance"):
                assert human["result"][key] == agent_run["result"][key], (key, human, agent_run)
        assert (
            client.get(
                "/api/v1/artifacts/" + gate["artifact_id"] + "/download"
            ).headers["content-type"]
            == "application/zip"
        )
        response = client.post(
            "/api/v1/reviews/" + gate["id"] + "/decision",
            headers=headers,
            json={
                "artifact_id": gate["artifact_id"],
                "artifact_version": gate["artifact_version"],
                "decision": "accepted",
                "rationale": "Official parity passed on this exact candidate",
            },
        )
        assert response.status_code == 200, response.text
        print(
            "REBUILD agent iterated: candidate 1 failed official parity (max diff "
            + format(failing["result"]["max_absolute_difference"], ".3g")
            + "), candidate 2 passed (max diff "
            + format(passing["result"]["max_absolute_difference"], ".3g")
            + ", 0 of 100 rows above 1e-8); human reruns reproduced both; Gate 2 accepted",
            flush=True,
        )

        # Exercise actual drift calculation without pretending the above blocked validation was approved.
        async def monitoring_check():
            async with SessionFactory() as session:
                run = runningRecord(
                    id=str(uuid4()),
                    model_id=model["id"],
                    model_version=model["version"],
                    dataset_id="uci_credit",
                    operation="monitor",
                    status="running",
                )
                session.add(run)
                await session.flush()
                session.add(
                    RunExecutionRecord(run_id=run.id, actor_id="local-operator")
                )
                session.add(
                    MonitorJob(
                        run_id=run.id,
                        payload={
                            "mode": "simulated",
                            "window_label": "Explicit integration test, not production",
                            "shift": {"feature": "LIMIT_BAL", "factor": 10},
                        },
                    )
                )
                await session.commit()
            return await execute_monitor(run, SessionFactory)

        result = asyncio.run(monitoring_check())
        assert result.status == "completed"
        events = client.get("/api/v1/models/" + model["id"] + "/monitoring").json()
        assert events[0]["payload"]["simulated"] is True
        assert events[0]["payload"]["revalidation_required"] is True
        print(
            "MONITORING shared PSI: declared shift produced red and saved revalidation run; API gate was not bypassed in production",
            flush=True,
        )
        assert client.get("/api/v1/dashboard").status_code == 200
        print(
            "DONE: isolated live checks passed; no provider call and no runtime database changes",
            flush=True,
        )
