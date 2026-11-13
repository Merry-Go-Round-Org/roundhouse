"""Real run: real OpenRouter and real Docker, on the real legacy models. It costs money.

Not used by production or by the test suite. The platform tools here stand in for the backend,
but official parity follows the design: the exported artifact is scored alone in a fresh
container, and code outside the sandbox compares the scores, with a fixed tolerance.

A rebuild runs in rounds. Each round is one task: the agent exports a candidate and calls
run_parity itself, iterating on failures, as it does against the backend. After the round the
same parity check runs once more on the latest candidate: a pass ends the loop, and a failure
starts the next round, continuing the same thread and workspace, with the parity report as its
prompt.

    python -m harness.dev.live --key-file KEY.json --legacy LEGACY_MODELS --out OUT --scenario chat
    python -m harness.dev.live --key-file KEY.json --legacy LEGACY_MODELS --out OUT \\
        --scenario rebuild --package credit_xgboost_python --rounds 5 --budget 3

LEGACY_MODELS is the `legacy_models` folder, with its fitted models and `shared/`.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import re
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from harness.config import HarnessSettings
from harness.db import create_tables, enable_sqlite_pragmas
from harness.document_template import document_problems
from harness.dev.gateway import FakeGatewayFactory
from harness.llm import OpenRouterClient
from harness.manager import TaskManager
from harness.profiles import REBUILD_ARTIFACT_DIR, REBUILD_ENTRY_POINT
from harness.sandbox import (
    AsyncioProcessRunner,
    SandboxConfig,
    SandboxGatewayFactory,
    SandboxMounts,
    safe_relative_path,
)
from harness.schemas import (
    Actor,
    AgentEvent,
    Resolution,
    TaskContext,
    TaskInfo,
    TaskKind,
    TaskRequest,
    TaskResult,
    ToolResult,
    ToolSpec,
)
from harness.store import SqlTaskStore

PARITY_ABSOLUTE_TOLERANCE = 1e-8  # frozen by the legacy workstream, with rtol=0



def _spec(name: str, description: str, risk: str, **properties: dict[str, Any]) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        risk=risk,
        parameters={"type": "object", "properties": properties, "required": list(properties)},
    )


PLATFORM_TOOLS = [
    _spec(
        "export_artifact",
        "Export a workspace file or folder as an official artifact. Returns its artifact_id.",
        "write",
        path={"type": "string", "description": "Path relative to the workspace."},
    ),
    _spec(
        "run_parity",
        "Run official parity on an exported artifact, in a fresh sandbox. The artifact is one "
        f"Python file, or a folder with {REBUILD_ENTRY_POINT} at its root, with the CLI "
        "`--input INPUT.csv --output OUTPUT.csv`, writing the columns "
        "row_id,positive_probability in input order. It has no access to your workspace.",
        "read",
        artifact_id={"type": "string"},
    ),
    _spec(
        "request_review",
        "Ask for human review of finished work. Pauses this task until a person decides.",
        "review",
        summary={"type": "string"},
    ),
]


def _isolated_run(config: SandboxConfig, *mounts: str) -> list[str]:
    command = [
        config.docker, "run", "--rm", "--network", "none", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", str(config.pids_limit),
        "--memory", config.memory, "--cpus", config.cpus, "--read-only", "--user", config.user,
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev",
    ]  # fmt: skip
    for mount in mounts:
        command += ["--mount", mount]
    return [*command, config.image]


class LivePlatform:
    """Stands in for the backend's export and parity services, for one task."""

    def __init__(self, scenario: _Scenario, task: TaskInfo) -> None:
        self._scenario = scenario
        self._task = task
        self._runner = AsyncioProcessRunner()
        self._config = SandboxConfig()

    async def list_tools(self) -> list[ToolSpec]:
        return list(PLATFORM_TOOLS)

    async def call(self, name: str, args: dict[str, Any], idempotency_key: str) -> ToolResult:
        self._scenario.platform_calls.append(name)
        if name == "export_artifact":
            return await self._export(args)
        if name == "run_parity":
            return await self._parity(args)
        if name == "request_review":
            return ToolResult(status="needs_review", content="Waiting.", pending_id=f"review-{uuid4().hex[:8]}")
        return ToolResult(status="denied", content="Unknown tool.")

    async def _export(self, args: dict[str, Any]) -> ToolResult:
        try:
            relative = safe_relative_path(args.get("path"))
        except Exception:
            return ToolResult(status="denied", content="Invalid path.")
        volume = f"mgr-workspace-{self._task.workspace_id}"
        staging = self._scenario.work / f"export-{uuid4().hex[:8]}"
        staging.mkdir(parents=True)
        error = await _copy_out_of_volume(self._runner, self._config, volume, relative, staging)
        if error:
            shutil.rmtree(staging, ignore_errors=True)
            return ToolResult(status="error", content=f"Could not export: {error}")
        exported = staging / Path(relative).name
        digest = _tree_digest(exported)
        artifact_id = f"artifact-{digest[:12]}"
        folder = self._scenario.artifacts / artifact_id
        if folder.exists():
            shutil.rmtree(staging, ignore_errors=True)
        else:
            staging.rename(folder)
        self._scenario.exported[artifact_id] = folder / Path(relative).name
        self._scenario.last_export = artifact_id
        return ToolResult(status="ok", content=json.dumps({"artifact_id": artifact_id, "sha256": digest}))

    async def _parity(self, args: dict[str, Any]) -> ToolResult:
        artifact_id = str(args.get("artifact_id"))
        if artifact_id not in self._scenario.exported:
            return ToolResult(status="error", content="Unknown artifact_id. Export the file first.")
        report = await official_parity(self._scenario, artifact_id, self._runner, self._config)
        return ToolResult(status="ok", content=json.dumps(report))


MAX_EXPORT_BYTES = 50_000_000


async def _copy_out_of_volume(
    runner: AsyncioProcessRunner, config: SandboxConfig, volume: str, relative: str, target: Path
) -> str | None:
    """Copy a file or folder out of a workspace volume with `docker cp`, which keeps bytes."""
    created = await runner.run(
        [
            config.docker, "create", "--network", "none", "--user", config.user,
            "--mount", f"type=volume,src={volume},dst=/workspace,readonly", config.image, "true",
        ],  # fmt: skip
        timeout_s=60,
        max_output=4_000,
    )
    if created.returncode != 0:
        return "the workspace is unavailable"
    container = created.stdout.strip()
    try:
        copied = await runner.run(
            [config.docker, "cp", f"{container}:/workspace/{relative}", str(target)],
            timeout_s=120,
            max_output=4_000,
        )
        if copied.returncode != 0:
            return "no such file or folder in the workspace"
    finally:
        await runner.run([config.docker, "rm", "--force", container], timeout_s=60, max_output=4_000)
    paths = [target, *target.rglob("*")]
    if any(p.is_symlink() for p in paths):
        return "symbolic links are not allowed in an artifact"
    if sum(p.stat().st_size for p in paths if p.is_file()) > MAX_EXPORT_BYTES:
        return f"the artifact is larger than {MAX_EXPORT_BYTES} bytes"
    _readable(target)
    return None


def _tree_digest(path: Path) -> str:
    """One hash for a file or a folder: relative paths and file contents, in sorted order."""
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(item.relative_to(path).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(item.read_bytes()).digest())
    return digest.hexdigest()


# A heuristic static check of the rebuild rules, so a wrapper around the legacy model is not
# mistaken for a rebuild. It is trusted code outside the sandbox, but it is not a proof.
_FORBIDDEN_SUFFIXES = {".joblib", ".pkl", ".pickle", ".rds", ".rda", ".rdata", ".ubj", ".bst", ".npy", ".npz"}
_FORBIDDEN_IMPORT = re.compile(
    r"^\s*(?:import|from)\s+(sklearn|xgboost|lightgbm|joblib|pickle|cloudpickle|dill|statsmodels"
    r"|rpy2|subprocess|ctypes|importlib)\b",
    re.MULTILINE,
)


def rebuild_rule_violations(artifact: Path) -> list[str]:
    files = [artifact] if artifact.is_file() else sorted(p for p in artifact.rglob("*") if p.is_file())
    root = artifact.parent if artifact.is_file() else artifact
    problems = []
    for path in files:
        name = path.relative_to(root).as_posix()
        if path.suffix.lower() in _FORBIDDEN_SUFFIXES:
            problems.append(f"{name}: serialized model or binary array files are not allowed")
            continue
        try:
            text_content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"{name}: not a plain-text file")
            continue
        if path.suffix == ".py":
            for match in _FORBIDDEN_IMPORT.finditer(text_content):
                problems.append(f"{name}: imports {match.group(1)}")
            if "__import__" in text_content or "os.system" in text_content:
                problems.append(f"{name}: dynamic import or shell call")
    return problems


async def official_parity(
    scenario: _Scenario, artifact_id: str, runner: AsyncioProcessRunner, config: SandboxConfig
) -> dict[str, Any]:
    """Score the exported artifact alone in a fresh container, then compare outside it."""
    artifact = scenario.exported[artifact_id]
    entry = artifact.name if artifact.is_file() else f"{artifact.name}/{REBUILD_ENTRY_POINT}"
    out = scenario.work / f"parity-{uuid4().hex[:8]}"
    out.mkdir(parents=True)
    out.chmod(0o777)
    result = await runner.run(
        [
            *_isolated_run(
                config,
                f"type=bind,src={artifact.parent},dst=/artifact,readonly",
                f"type=bind,src={scenario.data_dir},dst=/inputs/data,readonly",
                f"type=bind,src={out},dst=/out",
            ),
            "python", f"/artifact/{entry}",
            "--input", "/inputs/data/development_inputs.csv", "--output", "/out/scores.csv",
        ],  # fmt: skip
        timeout_s=300,
        max_output=4_000,
    )
    scenario.parity_runs += 1
    if result.returncode != 0:
        report: dict[str, Any] = {
            "passed": False, "error": "the artifact failed to run", "stderr": result.stderr[-1500:]
        }
    else:
        report = compare_scores(out / "scores.csv", scenario.reference)
    violations = rebuild_rule_violations(artifact)
    if violations:
        report["passed"] = False
        report["rebuild_rule_violations"] = violations[:20]
    report["artifact_id"] = artifact_id
    scenario.parity_reports.append(report)
    return report


def compare_scores(candidate: Path, reference: Path) -> dict[str, Any]:
    """Trusted comparison, outside any sandbox. The tolerance is not a parameter."""
    def read(path: Path) -> list[dict[str, str]]:
        with path.open(newline="") as handle:
            return list(csv.DictReader(handle))

    if not candidate.exists():
        return {"passed": False, "error": "no output file was written"}
    try:
        rows, expected = read(candidate), read(reference)
        if rows and list(rows[0]) != ["row_id", "positive_probability"]:
            return {"passed": False, "error": f"wrong columns: {list(rows[0])}"}
        if [r["row_id"] for r in rows] != [r["row_id"] for r in expected]:
            return {"passed": False, "error": "row_id values or their order differ"}
        differences = [
            abs(float(a["positive_probability"]) - float(b["positive_probability"]))
            for a, b in zip(rows, expected)
        ]
    except (ValueError, KeyError) as exc:
        return {"passed": False, "error": f"unreadable output: {type(exc).__name__}"}
    worst = max(differences, default=math.inf)
    # Row IDs only, never reference values, so the report does not hand over the answers.
    ranked = sorted(range(len(differences)), key=lambda i: differences[i], reverse=True)
    return {
        "passed": bool(differences) and worst <= PARITY_ABSOLUTE_TOLERANCE,
        "n_rows": len(differences),
        "n_above_tolerance": sum(d > PARITY_ABSOLUTE_TOLERANCE for d in differences),
        "max_absolute_difference": worst,
        "worst_row_ids": [rows[i]["row_id"] for i in ranked[:5] if differences[i] > PARITY_ABSOLUTE_TOLERANCE],
        "tolerance_absolute": PARITY_ABSOLUTE_TOLERANCE,
    }


class _Scenario:
    def __init__(self, work: Path, data_dir: Path | None = None, reference: Path | None = None) -> None:
        self.work = work
        self.artifacts = work / "artifacts"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        self.reference = reference
        self.exported: dict[str, Path] = {}
        self.last_export: str | None = None
        self.platform_calls: list[str] = []
        self.parity_runs = 0
        self.parity_reports: list[dict[str, Any]] = []


class _LivePlatformFactory:
    def __init__(self, scenario: _Scenario) -> None:
        self._scenario = scenario

    @asynccontextmanager
    async def open(self, task: TaskInfo) -> AsyncIterator[LivePlatform]:
        yield LivePlatform(self._scenario, task)


# ---------------------------------------------------------------- bundles
def _copy_package(source: Path, target: Path, skip: tuple[str, ...]) -> None:
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*skip), dirs_exist_ok=True)


def _readable(folder: Path) -> None:
    for path in [folder, *folder.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)


# What a Python legacy scorer imports from `shared/`. The rest of `shared/` holds the legacy
# workstream's own migration and parity tools, which would hand the agent the answer.
_SHARED_FOR_SCORING = ("score_python.py", "pipeline.py", "xls_biff_reader.py", "train_python.py")
# Precomputed answers: reference scores, and the R coefficients already extracted to JSON.
_REBUILD_HIDDEN = ("reference_predictions.csv", "r_coefficients.json", "train_execution.json")


def rebuild_bundle(legacy: Path, name: str, out: Path) -> tuple[Path, Path, Path]:
    """What the backend would mount for a rebuild, for a package in any language: the package
    beside the parts of `shared/` its scorer needs. The reference scores stay outside.
    """
    package = legacy / name
    model, data = out / "model", out / "data"
    _copy_package(package, model / name, _REBUILD_HIDDEN)
    scorer = package / "score.py"
    if scorer.exists() and "shared" in scorer.read_text():
        (model / "shared").mkdir(parents=True, exist_ok=True)
        for file in _SHARED_FOR_SCORING:
            shutil.copy(legacy / "shared" / file, model / "shared" / file)
    data.mkdir(parents=True, exist_ok=True)
    shutil.copy(package / "fixtures" / "development_inputs.csv", data / "development_inputs.csv")
    reference = out / "reference_predictions.csv"
    shutil.copy(package / "fixtures" / "reference_predictions.csv", reference)
    _readable(model)
    _readable(data)
    return model, data, reference


def document_bundle(legacy: Path, name: str, out: Path) -> tuple[Path, Path]:
    """Keeps the layout `score.py` expects, with the package beside `shared/`."""
    model, data = out / "model", out / "data"
    _copy_package(legacy / name, model / name, ())
    _copy_package(legacy / "shared", model / "shared", ("__pycache__",))
    data.mkdir(parents=True, exist_ok=True)
    shutil.copy(legacy / name / "fixtures" / "development_inputs.csv", data / "development_inputs.csv")
    _readable(model)
    _readable(data)
    return model, data


# ---------------------------------------------------------------- reporting
def _request(prompt: str | None, kind: TaskKind, resolution: Resolution | None = None, **context: Any) -> TaskRequest:
    return TaskRequest(
        request_id=uuid4().hex,
        actor=Actor(id="live-run", role="developer"),
        kind=kind,
        prompt=prompt,
        context=TaskContext(policy_version="live-1", **context),
        resolution=resolution,
    )


class _Run:
    def __init__(self, manager: TaskManager, store: SqlTaskStore, log: Path) -> None:
        self.manager = manager
        self.store = store
        self.log = log

    async def task(self, label: str, request: TaskRequest, continue_from: str | None = None) -> tuple[str, TaskResult]:
        task_id = await self.manager.spawn(request, continue_from)
        events = self.manager.subscribe(task_id)
        tools: list[str] = []
        with self.log.open("a") as handle:
            async for event in events:
                self._record(handle, label, event, tools)
        result = await self.manager.wait(task_id)
        usage = result.usage
        print(
            f"[{label}] status={result.status} error={result.error_code} "
            f"cost=${usage.cost_usd:.5f} in={usage.input_tokens} out={usage.output_tokens} "
            f"reasoning={usage.reasoning_tokens} unknown_cost={usage.unknown_cost_calls}",
            flush=True,
        )
        print(f"[{label}] tools: {' '.join(tools) or '(none)'}", flush=True)
        if result.text:
            print(f"[{label}] text: {result.text[:600]}", flush=True)
        return task_id, result

    @staticmethod
    def _record(handle: Any, label: str, event: AgentEvent, tools: list[str]) -> None:
        if event.type == "token":
            return
        handle.write(json.dumps({"label": label, **event.model_dump()}) + "\n")
        handle.flush()
        if event.type == "tool_result":
            status = event.data["status"]
            tools.append(event.data["name"] + ("" if status == "ok" else f"[{status}]"))


async def scenario_chat(run: _Run) -> bool:
    good = True
    _, plain = await run.task("chat-plain", _request("In one sentence, what can you help me with here?", "chat"))
    good &= plain.status == "completed"

    first, read = await run.task(
        "chat-read", _request("What is the status of model m-17? Use the tool, then tell me briefly.", "chat")
    )
    good &= read.status == "completed"
    _, follow = await run.task(
        "chat-continue", _request("Which model ID did I just ask about? Answer with the ID only.", "chat"), first
    )
    good &= follow.status == "completed" and "m-17" in (follow.text or "")

    paused_id, paused = await run.task("chat-write", _request("Change the owner of the model to Bob.", "chat"))
    good &= paused.status == "awaiting_confirmation"
    if paused.status == "awaiting_confirmation":
        resolution = Resolution(pending_id=paused.pending_id or "", outcome="approved", detail="The owner is now Bob.")
        _, resumed = await run.task("chat-resume", _request(None, "chat", resolution), paused_id)
        good &= resumed.status == "completed"

    _, denied = await run.task("chat-denied", _request("Delete model m-17 now.", "chat"))
    good &= denied.status == "completed"

    many = await asyncio.gather(
        *(run.task(f"chat-parallel-{i}", _request(f"Reply with exactly this number and nothing else: {i}", "chat")) for i in range(4))
    )
    for i, (_, result) in enumerate(many):
        good &= result.status == "completed" and str(i) in (result.text or "")
    return good


# A later round cannot succeed after these, so the loop stops.
_FATAL_ERRORS = {"budget_exhausted", "sandbox_unavailable", "audit_write_failed", "internal_error", "context_too_large"}


def _next_round_prompt(round_number: int, report: dict[str, Any], status: str, error: str | None) -> str:
    if report.get("artifact_id") is None:
        return (
            f"Round {round_number}. The previous round ended without an exported candidate "
            f"(status {status}, error {error}). Finish the candidate in {REBUILD_ARTIFACT_DIR}/, "
            "check it, export it and summarize."
        )
    return (
        f"Round {round_number}. Official parity of candidate {report['artifact_id']} failed. "
        f"Report: {json.dumps(report)}. Find the cause, fix the candidate in "
        f"{REBUILD_ARTIFACT_DIR}/, check it again, export it and summarize."
    )


async def scenario_rebuild(run: _Run, scenario: _Scenario, name: str, rounds: int, out: Path) -> bool:
    """The rebuild loop: each round is one task that leaves a candidate, calling run_parity as
    it goes. Then the same parity check runs on the latest candidate. A pass ends the loop. A
    failure starts the next round as a continuation, in the same workspace, with the parity
    report as its prompt.
    """
    runner, config = AsyncioProcessRunner(), SandboxConfig()
    history: list[dict[str, Any]] = []
    prompt = "Rebuild the legacy model package in /inputs/model. Development data is at /inputs/data/development_inputs.csv."
    task_id: str | None = None
    passed = False
    for number in range(1, rounds + 1):
        scenario.last_export = None
        request = _request(prompt, "rebuild", model_package_id=name, development_data_id="dev")
        task_id, result = await run.task(f"round-{number}", request, task_id)
        if scenario.last_export is None:
            report: dict[str, Any] = {"passed": False, "error": "no candidate was exported in this round", "artifact_id": None}
        else:
            report = await official_parity(scenario, scenario.last_export, runner, config)
        passed = bool(report.get("passed"))
        history.append({
            "round": number, "task_id": task_id, "status": result.status, "error_code": result.error_code,
            "cost_usd": result.usage.cost_usd, "summary": (result.text or "")[:2000], "parity": report,
        })  # fmt: skip
        print(f"[round-{number}] parity {'PASSED' if passed else 'FAILED'}: {json.dumps(report)[:600]}", flush=True)
        (out / "rebuild-summary.json").write_text(json.dumps(
            {"package": name, "passed": passed, "rounds": history}, indent=2, default=str
        ))  # fmt: skip
        if passed or result.error_code in _FATAL_ERRORS:
            break
        prompt = _next_round_prompt(number + 1, report, result.status, result.error_code)
    return passed


async def scenario_document(run: _Run, scenario: _Scenario, name: str) -> bool:
    # No prompt: the production profile's default prompt, rules and template are what is tested.
    _, result = await run.task("document", _request(None, "document", model_package_id=name, development_data_id="dev"))
    if result.status != "completed" or not scenario.exported:
        return False
    document = next(iter(scenario.exported.values())).read_text()
    problems = document_problems(document)
    print(f"[document] {len(document)} characters, problems: {problems or 'none'}", flush=True)
    return not problems


# ---------------------------------------------------------------- main
async def _report(engine: Any, out: Path, key: str) -> None:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT status, finish_reason, provider, returned_model, reasoning_effort, count(*), "
                    "sum(prompt_tokens), sum(completion_tokens), sum(reasoning_tokens), "
                    "round(sum(cost_usd), 5), sum(cost_estimated), max(first_chunk_ms), max(total_ms) "
                    "FROM llm_calls GROUP BY 1, 2, 3, 4, 5"
                )
            )
        ).fetchall()
    print("\nstatus finish provider model effort calls in out reasoning cost estimated max_first_ms max_total_ms")
    for row in rows:
        print("  ", *row)
    leaked = [str(p) for p in out.rglob("*") if p.is_file() and key.encode() in p.read_bytes()]
    print(f"key found in output files: {len(leaked)}")


async def _main(args: argparse.Namespace) -> int:
    key = json.loads(args.key_file.read_text())["key"]
    args.out.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{args.out / f'{args.scenario}.db'}")
    enable_sqlite_pragmas(engine)
    async with engine.begin() as connection:
        await connection.run_sync(create_tables)
    store = SqlTaskStore(async_sessionmaker(engine, expire_on_commit=False))
    settings = HarnessSettings(global_budget_usd=args.budget)
    client = OpenRouterClient(key, settings)
    scenario = _Scenario(args.out / args.scenario)
    volumes: list[str] = []
    try:
        if args.scenario == "chat":
            gateways: Any = FakeGatewayFactory()
        else:
            bundle = args.out / "bundles" / args.scenario
            if args.scenario == "rebuild":
                model, data, reference = rebuild_bundle(args.legacy, args.package, bundle)
                scenario.reference = reference
            else:
                model, data = document_bundle(args.legacy, args.package, bundle)
            scenario.data_dir = data

            async def resolve(task: TaskInfo) -> SandboxMounts:
                volumes.append(f"mgr-workspace-{task.workspace_id}")
                return SandboxMounts(model_package_path=str(model), development_data_path=str(data))

            gateways = SandboxGatewayFactory(_LivePlatformFactory(scenario), resolve)
        manager = TaskManager(store, client, gateways, settings)
        run = _Run(manager, store, args.out / f"{args.scenario}-events.jsonl")
        try:
            if args.scenario == "chat":
                good = await scenario_chat(run)
            elif args.scenario == "rebuild":
                good = await scenario_rebuild(run, scenario, args.package, args.rounds, args.out)
            else:
                good = await scenario_document(run, scenario, args.package)
        finally:
            await manager.shutdown()
        print(f"\nplatform calls: {scenario.platform_calls}")
        print(f"total cost so far in this database: ${await store.global_cost():.5f}")
        await _report(engine, args.out, key)
        print(f"SCENARIO {args.scenario}: {'PASSED' if good else 'FAILED'}")
        return 0 if good else 1
    finally:
        await client.aclose()
        await engine.dispose()
        runner = AsyncioProcessRunner()
        for volume in set(volumes):
            await runner.run(["docker", "volume", "rm", "--force", volume], timeout_s=30, max_output=500)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key-file", type=Path, required=True, help='JSON file with a "key" field')
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scenario", choices=["chat", "rebuild", "document"], required=True)
    parser.add_argument("--legacy", type=Path, help="legacy_models folder, for rebuild and document")
    parser.add_argument("--package", help="model package name, for rebuild and document")
    parser.add_argument("--budget", type=float, default=1.0, help="stop new LLM calls at this total, in USD")
    parser.add_argument("--rounds", type=int, default=5, help="rebuild only: the most rebuild rounds before giving up")
    args = parser.parse_args()
    if args.scenario != "chat" and not (args.legacy and args.package):
        parser.error("--legacy and --package are required for rebuild and document")
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
