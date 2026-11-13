"""`harness/runner.py`: the contract the backend calls, with a scripted LLM and no network."""
from __future__ import annotations

import asyncio
import inspect
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from contracts.harness import (
    Actor,
    Harness,
    HarnessEvent,
    HarnessRejected,
    HarnessRequest,
    RequestContext,
    Resolution,
    ToolResult,
    ToolSpec,
)
from harness import runner as runner_module
from harness.dev.gateway import ScriptedLlm, text_turn, tool_turn
from harness.runner import HarnessRunner, open_harness
from harness.schemas import SandboxUnavailable, TaskInfo
from harness_helpers import fast_settings


def request(prompt="What is the status of model m1?", **changes) -> HarnessRequest:
    values = {
        "request_id": uuid4().hex,
        "actor": Actor(id="alice", role="developer"),
        "kind": "chat",
        "prompt": prompt,
        "context": RequestContext(policy_version="p1", conversation_id="secret-conversation"),
    }
    return HarnessRequest(**{**values, **changes})


class ReadOnlyTools:
    """The shape of what the backend will write: read tools, bound to one actor."""

    def __init__(self, result: ToolResult | None = None) -> None:
        self.calls: list[tuple[str, dict, str]] = []
        self.result = result or ToolResult(status="ok", content='{"status": "validated"}')

    async def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="get_model_status",
                description="Read a model's status.",
                parameters={"type": "object", "properties": {"model_id": {"type": "string"}}},
                risk="read",
            )
        ]

    async def call(self, name: str, args: dict, idempotency_key: str) -> ToolResult:
        self.calls.append((name, args, idempotency_key))
        return self.result


class Events(list):
    async def __call__(self, event: HarnessEvent) -> None:
        self.append(event)

    def of(self, kind: str) -> list[HarnessEvent]:
        return [event for event in self if event.type == kind]


async def stall() -> None:
    await asyncio.sleep(30)


def play(database, llm, scenario, **options):
    async def main():
        async with database as sessions:
            runner = HarnessRunner(
                sessions, llm, settings=options.pop("settings", fast_settings()), **options
            )
            try:
                return await scenario(runner)
            finally:
                await runner.shutdown()

    return asyncio.run(main())


def test_run_matches_the_contract_signature():
    def params(function):
        return list(inspect.signature(function).parameters)

    assert params(HarnessRunner.run) == params(Harness.run)
    assert inspect.iscoroutinefunction(HarnessRunner.run)


def test_chat_streams_tokens_and_returns_the_result(database):
    llm = ScriptedLlm([text_turn("The model is fine.")])
    events = Events()

    async def scenario(runner):
        return await runner.run(request(), ReadOnlyTools(), events, asyncio.Event())

    result = play(database, llm, scenario)
    assert result.status == "completed"
    assert result.text == "The model is fine."
    assert result.task_id and result.error_code is None and result.checkpoint_id is None
    assert result.usage.input_tokens > 0
    assert "".join(e.data["text"] for e in events.of("token")) == "The model is fine."
    assert events[-1].type == "done" and events[-1].data == {"status": "completed"}


def test_internal_cost_events_stay_inside(database):
    events = Events()

    async def scenario(runner):
        return await runner.run(request(), ReadOnlyTools(), events, asyncio.Event())

    play(database, ScriptedLlm([text_turn("Hello.")]), scenario)
    assert not any(e.type == "llm_turn" for e in events)
    assert "cost_usd" not in json.dumps([e.data for e in events])


def test_a_read_tool_runs_through_the_backend_gateway(database):
    tools = ReadOnlyTools()
    llm = ScriptedLlm(
        [tool_turn(("get_model_status", {"model_id": "m1"})), text_turn("It is validated.")]
    )
    events = Events()

    async def scenario(runner):
        return await runner.run(request(), tools, events, asyncio.Event())

    result = play(database, llm, scenario)
    assert result.text == "It is validated."
    name, args, key = tools.calls[0]
    assert (name, args) == ("get_model_status", {"model_id": "m1"})
    assert key == f"{result.task_id}:1:0"
    assert llm.requests[0].tools[0]["function"]["name"] == "get_model_status"
    assert llm.requests[1].messages[-1]["content"] == '{"status": "validated"}'
    assert events.of("tool_result")[0].data["status"] == "ok"


def test_a_follow_up_restores_the_conversation(database):
    llm = ScriptedLlm([text_turn("First answer."), text_turn("Second answer.")])

    async def scenario(runner):
        first = await runner.run(request("first question"), ReadOnlyTools(), Events(), asyncio.Event())
        return await runner.run(
            request("second question", resume_from=first.task_id),
            ReadOnlyTools(),
            Events(),
            asyncio.Event(),
        )

    assert play(database, llm, scenario).text == "Second answer."
    contents = [m.get("content") for m in llm.requests[1].messages if m["role"] != "system"]
    assert contents == ["first question", "First answer.", "second question"]


def test_context_values_never_reach_the_llm(database):
    llm = ScriptedLlm([text_turn("Hello.")])

    async def scenario(runner):
        return await runner.run(request(), ReadOnlyTools(), Events(), asyncio.Event())

    play(database, llm, scenario)
    sent = json.dumps(llm.requests[0].messages)
    assert "secret-conversation" not in sent and "alice" not in sent


def test_a_write_pauses_and_resumes_after_the_human_decision(database):
    tools = ReadOnlyTools(ToolResult(status="needs_confirmation", content="Waiting.", pending_id="p-1"))
    llm = ScriptedLlm([tool_turn(("get_model_status", {})), text_turn("Done.")])
    events = Events()

    async def scenario(runner):
        paused = await runner.run(request(), tools, events, asyncio.Event())
        resumed = await runner.run(
            request(
                None,
                resume_from=paused.task_id,
                resolution=Resolution(pending_id=paused.checkpoint_id, outcome="approved"),
            ),
            tools,
            Events(),
            asyncio.Event(),
        )
        return paused, resumed

    paused, resumed = play(database, llm, scenario)
    assert paused.status == "awaiting_confirmation" and paused.checkpoint_id == "p-1"
    assert events.of("confirmation_required")[0].data["pending_id"] == "p-1"
    assert resumed.status == "completed" and resumed.text == "Done."


def test_a_cancel_signal_cancels_the_task(database):
    cancel = asyncio.Event()

    async def scenario(runner):
        running = asyncio.create_task(runner.run(request(), ReadOnlyTools(), Events(), cancel))
        await asyncio.sleep(0.05)
        cancel.set()
        return await asyncio.wait_for(running, 5)

    assert play(database, ScriptedLlm([[stall]]), scenario).status == "cancelled"


def test_a_caller_timeout_cancels_the_task_instead_of_leaving_it_running(database):
    async def scenario(runner):
        req = request()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(runner.run(req, ReadOnlyTools(), Events(), asyncio.Event()), 0.05)
        task_id = await runner._manager._store.find_by_request_id(req.request_id)
        return await asyncio.wait_for(runner._manager.wait(task_id), 5)

    assert play(database, ScriptedLlm([[stall]]), scenario).status == "cancelled"


def test_a_broken_emit_never_stops_the_task(database):
    async def closed_browser(event):
        raise ConnectionError("the browser went away")

    async def scenario(runner):
        return await runner.run(request(), ReadOnlyTools(), closed_browser, asyncio.Event())

    result = play(database, ScriptedLlm([text_turn("Still finished.")]), scenario)
    assert result.status == "completed" and result.text == "Still finished."


def test_the_same_request_id_never_starts_a_second_task(database):
    llm = ScriptedLlm([text_turn("Only once.")])

    async def scenario(runner):
        req = request()
        first = await runner.run(req, ReadOnlyTools(), Events(), asyncio.Event())
        second = await runner.run(req, ReadOnlyTools(), Events(), asyncio.Event())
        return first, second

    first, second = play(database, llm, scenario)
    assert first.task_id == second.task_id and second.text == "Only once."
    assert len(llm.requests) == 1


def test_a_full_queue_is_rejected_as_retryable(database):
    async def scenario(runner):
        running = asyncio.create_task(
            runner.run(request(), ReadOnlyTools(), Events(), asyncio.Event())
        )
        await asyncio.sleep(0.05)
        try:
            with pytest.raises(HarnessRejected) as rejected:
                await runner.run(request(), ReadOnlyTools(), Events(), asyncio.Event())
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        return rejected.value

    rejected = play(
        database,
        ScriptedLlm([[stall]]),
        scenario,
        settings=fast_settings(chat_concurrency=1, max_queued=0),
    )
    assert rejected.code == "busy" and rejected.retryable


def test_an_unknown_parent_is_rejected(database):
    async def scenario(runner):
        with pytest.raises(HarnessRejected) as rejected:
            await runner.run(
                request(resume_from="missing"), ReadOnlyTools(), Events(), asyncio.Event()
            )
        return rejected.value

    rejected = play(database, ScriptedLlm([]), scenario)
    assert rejected.code == "not_continuable" and not rejected.retryable


def test_chat_without_a_prompt_is_rejected(database):
    async def scenario(runner):
        with pytest.raises(HarnessRejected) as rejected:
            await runner.run(request(None), ReadOnlyTools(), Events(), asyncio.Event())
        return rejected.value

    assert play(database, ScriptedLlm([]), scenario).code == "invalid_request"


def test_a_malformed_tool_result_is_not_trusted(database):
    # A pause with no pending ID. The call may have run, so the LLM is told to check.
    # The contract type now refuses this shape too, so build it unvalidated, as a buggy
    # gateway could.
    tools = ReadOnlyTools(ToolResult.model_construct(status="needs_review", content="Waiting."))
    llm = ScriptedLlm([tool_turn(("get_model_status", {})), text_turn("I will check.")])

    async def scenario(runner):
        return await runner.run(request(), tools, Events(), asyncio.Event())

    result = play(database, llm, scenario)
    assert result.status == "completed"
    assert "unknown" in llm.requests[1].messages[-1]["content"].lower()


def test_chat_never_asks_for_a_sandbox(database):
    async def resolve(task):
        raise AssertionError("chat must not resolve mounts")

    async def scenario(runner):
        return await runner.run(request(), ReadOnlyTools(), Events(), asyncio.Event())

    result = play(database, ScriptedLlm([text_turn("Hello.")]), scenario, resolve_mounts=resolve)
    assert result.status == "completed"


def test_a_rebuild_without_mounts_fails_as_sandbox_unavailable(database):
    async def scenario(runner):
        return await runner.run(
            request("rebuild it", kind="rebuild"), ReadOnlyTools(), Events(), asyncio.Event()
        )

    result = play(database, ScriptedLlm([]), scenario)
    assert result.status == "failed" and result.error_code == "sandbox_unavailable"


def test_the_backend_chooses_a_rebuilds_data_through_the_resolver(database):
    seen: list[TaskInfo] = []

    async def resolve(task):
        seen.append(task)
        raise SandboxUnavailable("stop before Docker")

    async def scenario(runner):
        return await runner.run(
            request(
                "rebuild it",
                kind="rebuild",
                context=RequestContext(
                    policy_version="p1", model_package_id="pkg-1", development_data_id="data-1"
                ),
            ),
            ReadOnlyTools(),
            Events(),
            asyncio.Event(),
        )

    play(database, ScriptedLlm([]), scenario, resolve_mounts=resolve)
    assert (seen[0].model_package_id, seen[0].development_data_id) == ("pkg-1", "data-1")


def test_open_harness_recovers_orphans_and_closes_the_llm_client(database, monkeypatch):
    closed = []

    class FakeClient:
        def __init__(self, api_key, settings):
            self.api_key = api_key

        async def aclose(self):
            closed.append(self.api_key)

    monkeypatch.setattr(runner_module, "OpenRouterClient", FakeClient)

    async def main():
        async with database as sessions:
            async with open_harness(sessions, "key-1") as runner:
                assert isinstance(runner, HarnessRunner)

    asyncio.run(main())
    assert closed == ["key-1"]


def test_the_backend_creates_the_harness_tables_with_wal():
    from app.db.session import engine
    from app.main import app

    async def check():
        async with engine.connect() as connection:
            tables = await connection.execute(text("select name from sqlite_master where type='table'"))
            mode = await connection.execute(text("pragma journal_mode"))
            return {row[0] for row in tables}, mode.scalar()

    with TestClient(app):
        pass
    tables, mode = asyncio.run(check())
    assert {"agent_tasks", "agent_messages", "llm_calls"} <= tables
    assert mode == "wal"
