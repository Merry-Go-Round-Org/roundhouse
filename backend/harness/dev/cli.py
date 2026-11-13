"""Scripted command-line run: tasks spawn at once, continue, pause and write audit rows.

Uses a scripted LLM, fake tools and a temporary SQLite file. No network, no Docker, no API key.

    python -m harness.dev.cli
    python -m harness.dev.cli --tasks 20 --db /tmp/harness-demo.db
"""
from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from harness.config import HarnessSettings
from harness.db import create_tables, enable_sqlite_pragmas
from harness.dev.gateway import FakeGatewayFactory, ScriptedLlm, text_turn, tool_turn
from harness.manager import TaskManager
from harness.schemas import Actor, LlmRequest, Resolution, TaskContext, TaskKind, TaskRequest
from harness.store import SqlTaskStore


def _reply(request: LlmRequest):
    """Decide the next scripted turn from the conversation so far."""
    prompts = [m["content"] for m in request.messages if m["role"] == "user"]
    last = request.messages[-1]
    if last["role"] == "tool":
        return text_turn("The tool has run, and here is the outcome.")
    if "change the owner" in prompts[-1]:
        return tool_turn(("get_model_status", {"model_id": "m1"}), ("update_model_owner", {"owner": "bob"}))
    if "was approved" in prompts[-1]:
        return text_turn("Done: the owner is now bob.")
    if "rebuild" in prompts[-1].lower():
        return tool_turn(("sandbox_write_file", {"path": "model.py", "content": "print(1)"}))
    if len(prompts) > 1:
        return text_turn(f"You asked {len(prompts)} questions so far. Latest: {prompts[-1]}")
    return text_turn(f"Answer to: {prompts[-1]}")


def _request(prompt: str | None, kind: TaskKind = "chat", resolution: Resolution | None = None) -> TaskRequest:
    return TaskRequest(
        request_id=uuid4().hex,
        actor=Actor(id="demo-user", role="developer"),
        kind=kind,
        prompt=prompt,
        context=TaskContext(policy_version="demo-1"),
        resolution=resolution,
    )


def _say(message: str) -> None:
    print(message, flush=True)


async def _scenario(manager: TaskManager, store: SqlTaskStore, tasks: int) -> None:
    _say(f"\n1. Spawn {tasks} chat tasks at once")
    task_ids = await asyncio.gather(*(manager.spawn(_request(f"question {i}")) for i in range(tasks)))
    statuses = [(await manager.get(t)).status for t in task_ids]
    _say(f"   after spawn: {statuses.count('queued')} queued, {statuses.count('running')} running")
    results = await asyncio.gather(*(manager.wait(t) for t in task_ids))
    _say(f"   after wait:  {sum(r.status == 'completed' for r in results)} completed")
    _say(f"   first reply: {results[0].text!r}")

    _say("\n2. Continue a finished task, which restores its context")
    child = await manager.spawn(_request("and a follow-up?"), continue_from=task_ids[0])
    continued = await manager.wait(child)
    _say(f"   new task {child[:8]}, thread {task_ids[0][:8]}: {continued.text!r}")

    _say("\n3. A write pauses for confirmation, then resumes with a resolution")
    paused_id = await manager.spawn(_request("please change the owner to bob"))
    paused = await manager.wait(paused_id)
    _say(f"   status {paused.status}, pending {paused.pending_id}")
    resolution = Resolution(pending_id=paused.pending_id or "", outcome="approved")
    resumed_id = await manager.spawn(_request(None, resolution=resolution), continue_from=paused_id)
    resumed = await manager.wait(resumed_id)
    _say(f"   status {resumed.status}: {resumed.text!r}")

    _say("\n4. A rebuild task streams events, using medium effort and sandbox tools")
    rebuild_id = await manager.spawn(_request("Rebuild the legacy model", kind="rebuild"))
    events = [e.type async for e in manager.subscribe(rebuild_id)]
    _say(f"   events: {' '.join(events)}")

    _say("\n5. Audit rows for the thread in step 3")
    _say("   turn attempt status     finish      effort tokens(in/out) cost_usd")
    for row in await store.list_llm_calls(paused_id):
        _say(
            f"   {row['turn']:<4} {row['attempt']:<7} {row['status']:<10} "
            f"{str(row['finish_reason']):<11} {row['reasoning_effort']:<6} "
            f"{row['prompt_tokens']}/{row['completion_tokens']:<12} {row['cost_usd']:.6f}"
        )
    _say(f"\nTotal cost across all tasks: ${await store.global_cost():.6f}")


async def _main(db_path: Path, tasks: int) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    enable_sqlite_pragmas(engine)
    async with engine.begin() as connection:
        await connection.run_sync(create_tables)
    store = SqlTaskStore(async_sessionmaker(engine, expire_on_commit=False))
    manager = TaskManager(store, ScriptedLlm(_reply), FakeGatewayFactory(), HarnessSettings())
    try:
        await _scenario(manager, store, tasks)
    finally:
        await manager.shutdown()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Scripted harness run, with no network or Docker.")
    parser.add_argument("--tasks", type=int, default=8, help="chat tasks to spawn at once")
    parser.add_argument("--db", type=Path, help="SQLite file to keep. Defaults to a temporary one")
    args = parser.parse_args()
    if args.db:
        asyncio.run(_main(args.db, args.tasks))
        _say(f"Database kept at {args.db}")
    else:
        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(_main(Path(folder) / "harness.db", args.tasks))


if __name__ == "__main__":
    main()
