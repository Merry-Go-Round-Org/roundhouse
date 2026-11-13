"""Opt-in live check of the AI-native kinds: real OpenRouter, real Docker. It costs money.

Each kind runs twice through the backend path (`ai_native_service.create_run` and `execute_run`,
the input registry and resolver, the real gateway and export checks), once on a clean case and once
on a seeded defect. It records completion, output validity, turns, cost and failure reasons, and
judges each result against what the case should produce. A valid output is not a correct one: the
judgement column is separate from the validity column.

From the repository root:

    backend/.venv/bin/python backend/tests/live_ai_native_check.py \\
        --key-file ../openrouter_key.json --out /tmp/live-ai-native --budget 4

The fixtures are built by `harness/dev/ai_native_fixtures.py` in the sandbox image, from the local
UCI partitions under `backend/storage/datasets/*/partitions` (or `--partitions`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
IMAGE = "merry-go-round/agent-sandbox:0.2"


def parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--budget", type=float, default=4.0)
    parser.add_argument("--partitions")
    parser.add_argument("--only", nargs="*", help="scenario names to run")
    return parser.parse_args()


ARGS = parse()
OUT = Path(ARGS.out).resolve()
OUT.mkdir(parents=True, exist_ok=True)
os.environ.update(
    STORAGE_DIR=str(OUT / "storage"),
    DATABASE_URL="sqlite+aiosqlite:///" + str(OUT / "app.db"),
    OPENROUTER_API_KEY=json.loads(Path(ARGS.key_file).read_text())["key"],
    ALLOW_LOCAL_DEMO_AUTH="true",
)
os.environ.setdefault("HARNESS_MAX_CONCURRENT_SANDBOXES", "2")
os.environ.setdefault("HARNESS_SANDBOX_MEMORY", "3g")

from sqlalchemy import func, select  # noqa: E402

import app.db.auth_tables  # noqa: E402,F401  users: the gateway checks the actor's role
from app.db.session import SessionFactory, initialize_database  # noqa: E402
from app.services import ai_native_service as ai  # noqa: E402
from app.services.harness_adapter import mount_resolver  # noqa: E402
from contracts.harness import Actor  # noqa: E402
from harness.config import HarnessSettings  # noqa: E402
from harness.db import AgentTask, LlmCall  # noqa: E402
from harness.runner import open_harness  # noqa: E402
from harness.sandbox import SandboxConfig, SandboxMounts  # noqa: E402

OPERATOR = Actor(id="local-operator", role="operator")
POLICY = "1.0.0"


def build_fixtures() -> Path:
    fixtures = OUT / "fixtures"
    if (fixtures / "fixtures.json").is_file():
        return fixtures
    fixtures.mkdir(exist_ok=True)
    partitions = Path(ARGS.partitions) if ARGS.partitions else next(
        (ROOT / "backend/storage/datasets").glob("*/partitions"))
    subprocess.run([
        "docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/tmp", "-v", f"{ROOT / 'legacy_models'}:/legacy:ro", "-v", f"{ROOT}:/repo:ro",
        "-v", f"{partitions}:/partitions:ro", "-v", f"{fixtures}:/out", IMAGE,
        "python", "/repo/backend/harness/dev/ai_native_fixtures.py",
    ], check=True)  # fmt: skip
    return fixtures


# ---------------------------------------------------------------- judgements
def _json(files, name):
    return json.loads(files[name]) if name in files else {}


def judge(name, run, files, proposals):
    text = lambda n: files.get(n, b"").decode("utf-8", "replace")  # noqa: E731
    types = [p.type for p in proposals]
    blob = json.dumps([[p.type, p.args, p.rationale] for p in proposals]).lower()
    if name == "survey_clean":
        survey = _json(files, "survey.json")
        cands = [" ".join(c["files"]) for c in survey.get("candidates", [])]
        ok = not survey.get("personal_data_suspected") and any(
            "credit_logistic_python/model.joblib" in c for c in cands) and any("credit_logistic_r/" in c for c in cands)
        return ok, f"{len(cands)} candidates, personal data {survey.get('personal_data_suspected')}"
    if name == "survey_pii":
        survey = _json(files, "survey.json")
        ok = bool(survey.get("personal_data_suspected")) and "crm_export" in json.dumps(survey.get("personal_data_evidence"))
        return ok, f"personal data {survey.get('personal_data_suspected')}"
    if name in ("intake_clean", "intake_broken"):
        intake = _json(files, "intake.json")
        mapping = (intake.get("dataset_mapping") or {}).get("dataset_id")
        if name == "intake_clean":
            return intake.get("executable") is True and mapping == "uci_credit", f"executable {intake.get('executable')}, dataset {mapping}"
        return intake.get("executable") is False, f"executable {intake.get('executable')}: {str(intake.get('execution_error'))[:120]}"
    if name.startswith("validation"):
        rec = (run.outcome or {}).get("recommendation") or ""
        findings = [p for p in proposals if p.type == "open_finding"]
        severities = sorted({p.args.get("severity") for p in findings})
        if name == "validation_clean":
            ok = rec.lower().startswith("approve")
        else:
            leak = any(w in (text("validation_narrative.md").lower() + blob) for w in ("leak", "pay_next"))
            high = any(p.args.get("severity") == "high" for p in findings)
            ok = rec.lower().startswith("reject") or (leak and high)
        return ok, f"recommendation {rec!r}, {len(findings)} findings {severities}"
    if name.startswith("triage"):
        triage = _json(files, "triage.json")
        outcome = triage.get("outcome")
        if name == "triage_clean":
            return outcome == "benign" and types == ["mark_drift_benign"], f"{outcome}, proposals {types}"
        issues = " ".join(triage.get("data_quality_issues", [])).lower()
        ok = outcome == "problem" and any(w in issues for w in ("cent", "unit", "scale", "100", "bill_amt"))
        return ok, f"{outcome}, data quality issues {len(triage.get('data_quality_issues', []))}, proposals {types}"
    if name == "investigation_clean":
        ok = bool({"start_revalidation", "start_recalibration"} & set(types)) and "report_data_incident" not in types
        return ok, f"proposals {types}"
    if name == "investigation_defect":
        return "report_data_incident" in types, f"proposals {types}"
    return False, "unknown scenario"


# ---------------------------------------------------------------- the run
async def main() -> None:
    fixtures = build_fixtures()
    await initialize_database()
    sessions = SessionFactory
    from app.services.harness_adapter import actor_role

    # Fail fast if the gateway would refuse every call as this actor.
    assert await actor_role(sessions, OPERATOR.id) == OPERATOR.role
    reg = ai.register_input
    data = await reg(sessions, "data", fixtures / "data")
    data_leaky = await reg(sessions, "data", fixtures / "data_leaky")
    archives = {n: await reg(sessions, "archive", fixtures / f"archive_{n}") for n in ("clean", "pii", "broken")}
    documents = {n: await reg(sessions, "document", fixtures / f"document_{n}") for n in ("clean", "leaky")}
    reports = {n: json.loads((fixtures / f"report_{n}.json").read_text()) for n in ("clean", "leaky")}
    metrics = {n: await ai.register_metrics_bundle(sessions, r) for n, r in reports.items()}
    validation_report = await reg(sessions, "reports", {"validation_report.json": (fixtures / "report_clean.json").read_bytes()})
    monitoring = {}
    for name in ("triage_clean", "triage_defect", "investigation_clean", "investigation_defect"):
        folder = fixtures / "monitoring" / name
        monitoring[name] = await ai.register_monitoring(
            sessions, json.loads((folder / "monitoring_summary.json").read_text()), batch=folder / "batch.csv")
    packages = {
        "pkg-clean": (fixtures / "model_clean", fixtures / "data"),
        "pkg-leaky": (fixtures / "model_leaky", fixtures / "data_leaky"),
    }
    backend_resolver = mount_resolver(sessions)

    async def resolver(info):
        # Live packages stand in for registered packages; everything else is the backend resolver.
        if info.model_package_id in packages:
            model, dev = packages[info.model_package_id]
            extra = await ai.input_mounts(sessions, info)
            return SandboxMounts(model_package_path=str(model), development_data_path=str(dev), **extra)
        return await backend_resolver(info)

    def ctx(**ids):
        return {"policy_version": POLICY, **ids}

    plans = {
        "survey_clean": ("survey", ctx(archive_id=archives["clean"]), None, {"development_data_id": data}),
        "survey_pii": ("survey", ctx(archive_id=archives["pii"]), None, {"development_data_id": data}),
        "validation_clean": ("validation", ctx(model_id="credit_logistic_python", model_package_id="pkg-clean",
                             development_data_id="dev-clean", document_id=documents["clean"],
                             metrics_bundle_id=metrics["clean"]), None, {}),
        "validation_leaky": ("validation", ctx(model_id="credit_payment_python", model_package_id="pkg-leaky",
                             development_data_id="dev-leaky", document_id=documents["leaky"],
                             metrics_bundle_id=metrics["leaky"]), None, {}),
        **{name: ("triage", ctx(model_id="credit_logistic_python", monitoring_run_id=monitoring[name],
                  development_data_id=data), None, {}) for name in ("triage_clean", "triage_defect")},
        **{name: ("investigation", ctx(model_id="credit_logistic_python", monitoring_run_id=monitoring[name],
                  development_data_id=data, document_id=documents["clean"],
                  validation_report_id=validation_report), None, {})
           for name in ("investigation_clean", "investigation_defect")},
    }
    results: dict[str, dict] = {}
    settings = HarnessSettings.from_env(global_budget_usd=ARGS.budget)

    async with open_harness(sessions, os.environ["OPENROUTER_API_KEY"], settings=settings,
                            resolve_mounts=resolver, sandbox_config=SandboxConfig.from_env()) as runner:

        async def execute(name, run_id):
            if ARGS.only and name not in ARGS.only:
                return
            started = time.monotonic()
            try:
                result = await ai.execute_run(sessions, runner, run_id)
            except Exception as exc:  # recorded, never hidden
                results[name] = {"run_id": run_id, "status": "error", "error_code": type(exc).__name__, "detail": str(exc)[:300]}
                return
            results[name] = await record(name, run_id, result, time.monotonic() - started)
            print(f"{name}: {results[name]['status']} judged {results[name]['judged_correct']} "
                  f"${results[name]['cost_usd']:.3f} {results[name]['turns']} turns", flush=True)

        async def platform_run(kind):
            """A model and its validate or monitor run: what a proposal is bound to."""
            from uuid import uuid4
            from app.db.tables import ModelRecord, MonitoringRecord, runningRecord

            async with sessions() as session:
                model = ModelRecord(name="live-" + uuid4().hex[:8], version="1", language="Python",
                                    dataset_id="uci_credit", purpose="Live AI-native check")
                session.add(model)
                await session.flush()
                run = runningRecord(model_id=model.id, model_version="1", dataset_id="uci_credit",
                                    operation="validate" if kind == "validation" else "monitor",
                                    status="running")
                session.add(run)
                await session.flush()
                if kind != "validation":
                    session.add(MonitoringRecord(id=str(uuid4()), model_id=model.id, run_id=run.id,
                                                 payload={"input_scope": "uploaded_batch"}))
                await session.commit()
                return run.id

        async def create(name):
            kind, context, prompt, follow = plans[name]
            platform = await platform_run(kind) if kind in ("validation", "triage", "investigation") else None
            return await ai.create_run(sessions, kind, OPERATOR, context, prompt=prompt, follow_up=follow,
                                       platform_run_id=platform)

        ids = {name: await create(name) for name in ("survey_clean", "survey_pii")}
        await asyncio.gather(*(execute(n, i) for n, i in ids.items()))

        # Intake: the clean survey's candidate for the Python model, and the same files in the broken archive.
        fallback = {"candidate_id": "credit_logistic_python", "name": "credit logistic (Python)", "language": "Python",
                    "fitted_model": "saved_model",
                    "files": sorted(str(p.relative_to(fixtures / "archive_clean")) for p in
                                    (fixtures / "archive_clean").rglob("*")
                                    if p.is_file() and str(p).split("archive_clean/")[1].startswith(("credit_logistic_python/", "shared/")))}
        survey_run = await ai._run(sessions, ids["survey_clean"])
        intake_ids = {}
        for run_id in (survey_run.outcome or {}).get("intake_run_ids", []):
            run = await ai._run(sessions, run_id)
            if "credit_logistic_python/model.joblib" in (run.prompt or ""):
                intake_ids["intake_clean"] = run_id
        from harness.handoff import intake_prompt

        if "intake_clean" not in intake_ids:
            intake_ids["intake_clean"] = await ai.create_run(
                sessions, "intake", OPERATOR, ctx(archive_id=archives["clean"], candidate_id=fallback["candidate_id"],
                                                  development_data_id=data), prompt=intake_prompt(fallback))
        intake_ids["intake_broken"] = await ai.create_run(
            sessions, "intake", OPERATOR, ctx(archive_id=archives["broken"], candidate_id=fallback["candidate_id"],
                                              development_data_id=data), prompt=intake_prompt(fallback))
        await asyncio.gather(*(execute(n, i) for n, i in intake_ids.items()))

        for group in (("validation_clean", "validation_leaky"), ("triage_clean", "triage_defect"),
                      ("investigation_clean", "investigation_defect")):
            ids = {name: await create(name) for name in group}
            await asyncio.gather(*(execute(n, i) for n, i in ids.items()))

    (OUT / "results.json").write_text(json.dumps(results, indent=2, default=str))
    (OUT / "results.md").write_text(table(results))
    print(table(results))


async def record(name, run_id, result, seconds):
    sessions = SessionFactory
    run = await ai._run(sessions, run_id)
    files, _ = await ai.latest_exports(sessions, run_id)
    async with sessions() as session:
        from app.services.proposal_service import ProposalRecord

        proposals = list(await session.scalars(
            select(ProposalRecord)
            .join(ai.AiNativeProposalLink, ai.AiNativeProposalLink.proposal_id == ProposalRecord.id)
            .where(ai.AiNativeProposalLink.run_id == run_id)))
        thread = await session.scalar(select(AgentTask.thread_id).where(AgentTask.id == run.task_id)) if run.task_id else None
        turns = await session.scalar(select(func.count()).select_from(LlmCall).where(LlmCall.thread_id == thread)) if thread else 0
    correct, detail = judge(name, run, files, proposals) if run.status == "completed" else (False, "no accepted output")
    export_dir = OUT / "exports" / name
    for file, content in files.items():
        (export_dir / file).parent.mkdir(parents=True, exist_ok=True)
        (export_dir / file).write_bytes(content)
    (export_dir / "proposals.json").parent.mkdir(parents=True, exist_ok=True)
    (export_dir / "proposals.json").write_text(json.dumps(
        [{"type": p.type, "args": p.args, "rationale": p.rationale, "evidence": p.evidence} for p in proposals], indent=2))
    return {
        "run_id": run_id, "kind": run.kind, "status": run.status, "error_code": run.error_code,
        "output_valid": run.status == "completed", "correction_rounds": run.correction_round,
        "turns": turns, "cost_usd": round(run.usage.get("cost_usd", 0.0), 4),
        "seconds": round(seconds), "problems": run.problems[:5], "proposals": [p.type for p in proposals],
        "judged_correct": correct, "judgement": detail,
    }


def table(results):
    lines = ["| Scenario | Status | Valid output | Rounds | Turns | Cost (USD) | Judged correct | Detail or failure |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, r in results.items():
        reason = r.get("judgement") if r.get("status") == "completed" else (r.get("error_code") or "") + " " + "; ".join(r.get("problems", []))[:200]
        lines.append(f"| {name} | {r.get('status')} | {r.get('output_valid')} | {r.get('correction_rounds')} | "
                     f"{r.get('turns')} | {r.get('cost_usd')} | {r.get('judged_correct')} | {reason} |")
    total = sum(r.get("cost_usd") or 0 for r in results.values())
    lines.append(f"\nTotal cost: ${total:.3f}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    asyncio.run(main())
