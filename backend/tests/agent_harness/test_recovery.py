"""Context recovery: continuing from each end state, restored order, unresolved tool calls."""
from __future__ import annotations

import asyncio

import pytest

from harness.dev.gateway import FakeGatewayFactory, ScriptedLlm, text_turn, tool_turn
from harness.schemas import LlmError, NewMessage, NotContinuable, Resolution, ToolCall
from harness.store import UNKNOWN_RESULT_TEXT
from harness_helpers import Harness, make_request


def play(database, scenario, llm=None, gateways=None):
    async def main():
        async with database as sessions:
            return await scenario(Harness(sessions, llm or ScriptedLlm([]), gateways=gateways))

    return asyncio.run(main())


def assert_valid_history(messages):
    """Every tool call has exactly one result, directly after its assistant message."""
    i = 0
    while i < len(messages):
        message = messages[i]
        i += 1
        if message["role"] == "tool":
            raise AssertionError(f"tool result without a call: {message}")
        calls = [c["id"] for c in message.get("tool_calls", [])]
        for call_id in calls:
            assert messages[i]["role"] == "tool", messages
            assert messages[i]["tool_call_id"] == call_id
            i += 1


async def _reach_completed(h):
    h.llm = ScriptedLlm([text_turn("first answer")])
    task_id, result = await h.start(make_request("first question"))
    return task_id, result


async def _reach_confirmation(h):
    h.llm = ScriptedLlm([tool_turn(("update_model_owner", {"owner": "bob"}))])
    task_id, result = await h.start(make_request("first question"))
    return task_id, result


async def _reach_review(h):
    h.llm = ScriptedLlm([tool_turn(("submit_review_decision", {"decision": "approve"}))])
    task_id, result = await h.start(make_request("first question"))
    return task_id, result


async def _reach_failed(h):
    h.llm = ScriptedLlm([[LlmError("request_rejected", http_status=400)]])
    task_id, result = await h.start(make_request("first question"))
    return task_id, result


async def _reach_cancelled(h):
    h.llm = ScriptedLlm([text_turn("never")])
    h.cancel.set()
    task_id, result = await h.start(make_request("first question"))
    h.cancel = asyncio.Event()
    return task_id, result


async def _reach_interrupted(h):
    task_id = await h.create(make_request("first question"))
    await h.store.claim_task(task_id, "dead-worker")
    await h.store.append_messages(
        task_id,
        task_id,
        [
            NewMessage(
                role="assistant",
                origin="llm",
                tool_calls=[ToolCall(id="c1", name="update_model_owner", arguments="{}")],
            )
        ],
    )
    await asyncio.sleep(0.01)
    assert await h.store.interrupt_orphans(0) == 1
    return task_id, await h.store.get_task(task_id)


@pytest.mark.parametrize(
    "reach, status",
    [
        (_reach_completed, "completed"),
        (_reach_confirmation, "awaiting_confirmation"),
        (_reach_review, "awaiting_review"),
        (_reach_failed, "failed"),
        (_reach_cancelled, "cancelled"),
        (_reach_interrupted, "interrupted"),
    ],
)
def test_continue_from_each_end_state(database, reach, status):
    async def scenario(h):
        parent, result = await reach(h)
        assert result.status == status
        h.llm = ScriptedLlm([text_turn("second answer")])
        child, second = await h.start(make_request("follow up"), continue_from=parent)
        return parent, child, second, h.llm.requests[0], await h.store.get_task(child)

    parent, child, second, request, snapshot = play(database, scenario)
    assert second.status == "completed" and second.task_id != parent
    assert snapshot.thread_id == parent and snapshot.parent_task_id == parent
    assert child != parent
    sent = request.messages
    assert sent[0]["role"] == "system"
    assert sent[1]["content"] == "first question"
    assert sent[-1]["content"] == "follow up"
    assert_valid_history(sent)


def test_a_task_never_returns_to_running(database):
    async def scenario(h):
        parent, _ = await _reach_completed(h)
        assert not await h.store.claim_task(parent, "other")
        return (await h.store.get_task(parent)).status

    assert play(database, scenario) == "completed"


def test_continuation_restores_earlier_messages_in_order(database):
    async def scenario(h):
        h.llm = ScriptedLlm([tool_turn(("get_model_status", {"a": 1})), text_turn("answer one")])
        first, _ = await h.start(make_request("question one"))
        h.llm = ScriptedLlm([text_turn("answer two")])
        second, _ = await h.start(make_request("question two"), continue_from=first)
        h.llm = ScriptedLlm([text_turn("answer three")])
        await h.start(make_request("question three"), continue_from=second)
        return h.llm.requests[0], await h.store.load_messages(first)

    request, stored = play(database, scenario)
    assert [m.seq for m in stored] == list(range(1, 9))
    sent = request.messages
    assert sent[0]["role"] == "system"
    assert [m["role"] for m in sent[1:]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert [m["content"] for m in sent if m["role"] == "user"] == [
        "question one",
        "question two",
        "question three",
    ]
    assert_valid_history(sent)
    # The system prompt comes from the profile each time and is never stored.
    assert all(m.role != "system" for m in stored)


def test_unresolved_tool_call_becomes_result_unknown_and_is_not_repeated(database):
    gateways = FakeGatewayFactory()

    async def scenario(h):
        parent, _ = await _reach_interrupted(h)
        h.llm = ScriptedLlm([text_turn("I will check the state first.")])
        await h.start(make_request("carry on"), continue_from=parent)
        return h.llm.requests[0], await h.store.load_messages(parent)

    request, stored = play(database, scenario, gateways=gateways)
    assert gateways.calls == []  # the write is never run a second time
    recovery = [m for m in stored if m.origin == "recovery"]
    assert [(m.role, m.tool_call_id, m.content) for m in recovery] == [
        ("tool", "c1", UNKNOWN_RESULT_TEXT)
    ]
    assert_valid_history(request.messages)
    roles = [m["role"] for m in request.messages]
    assert roles == ["system", "user", "assistant", "tool", "user"]


def test_cancelled_tool_batch_is_repaired_on_continuation(database):
    started = asyncio.Event()

    async def on_call(_name):
        started.set()
        await asyncio.sleep(3600)

    gateways = FakeGatewayFactory(on_call=on_call)

    async def scenario(h):
        h.llm = ScriptedLlm([tool_turn(("get_model_status", {}), ("get_model_status", {"b": 2}))])
        parent = await h.create(make_request("go"))
        running = asyncio.create_task(h.run(parent))
        await asyncio.wait_for(started.wait(), 5)
        h.cancel.set()
        await running
        h.cancel = asyncio.Event()
        gateways.on_call = None
        calls_before = len(gateways.calls)
        h.llm = ScriptedLlm([text_turn("checked")])
        await h.start(make_request("again"), continue_from=parent)
        return h.llm.requests[0], calls_before

    request, calls_before = play(database, scenario, gateways=gateways)
    assert calls_before == len(gateways.calls) == 1
    assert_valid_history(request.messages)
    results = [m["content"] for m in request.messages if m["role"] == "tool"]
    assert results == [UNKNOWN_RESULT_TEXT, UNKNOWN_RESULT_TEXT]


@pytest.mark.parametrize("reach", [_reach_confirmation, _reach_review])
def test_resolution_reaches_the_llm_and_each_call_has_one_result(database, reach):
    async def scenario(h):
        parent, result = await reach(h)
        resolution = Resolution(
            pending_id=result.pending_id, outcome="approved", detail="Owner changed to bob."
        )
        h.llm = ScriptedLlm([text_turn("Done, the owner is bob.")])
        _, second = await h.start(
            make_request(None, resolution=resolution), continue_from=parent
        )
        return second, h.llm.requests[0], await h.store.load_messages(parent)

    second, request, stored = play(database, scenario)
    assert second.status == "completed"
    assert_valid_history(request.messages)
    last = request.messages[-1]
    assert last["role"] == "user" and "approved" in last["content"]
    assert "Owner changed to bob." in last["content"]
    assert [m.origin for m in stored if m.role == "user"] == ["user", "resolution"]
    assert sum(1 for m in stored if m.role == "tool") == 1


def test_pause_with_later_calls_keeps_one_result_per_call(database):
    async def scenario(h):
        h.llm = ScriptedLlm(
            [tool_turn(("update_model_owner", {"owner": "bob"}), ("get_model_status", {}))]
        )
        parent, result = await h.start(make_request("go"))
        resolution = Resolution(pending_id=result.pending_id, outcome="rejected")
        h.llm = ScriptedLlm([text_turn("OK, left unchanged.")])
        await h.start(make_request(None, resolution=resolution), continue_from=parent)
        return h.llm.requests[0]

    request = play(database, scenario)
    assert_valid_history(request.messages)


def test_continuing_an_older_task_or_another_users_thread_is_rejected(database):
    async def scenario(h):
        first, _ = await _reach_completed(h)
        h.llm = ScriptedLlm([text_turn("two")])
        second, _ = await h.start(make_request("next"), continue_from=first)
        with pytest.raises(NotContinuable, match="newest"):
            await h.create(make_request("branch off"), continue_from=first)
        with pytest.raises(NotContinuable, match="another actor"):
            await h.create(make_request("steal", actor="mallory"), continue_from=second)
        return len(await h.store.list_thread_tasks(first))

    assert play(database, scenario) == 2


def test_truncated_output_is_absent_from_the_continuation(database):
    async def scenario(h):
        h.llm = ScriptedLlm([text_turn("half an ans", finish_reason="length")])
        parent, result = await h.start(make_request("go", kind="document"))
        assert result.error_code == "output_truncated"
        h.llm = ScriptedLlm([text_turn("short answer")])
        await h.start(make_request("be brief", kind="document"), continue_from=parent)
        return h.llm.requests[0]

    request = play(database, scenario)
    assert "half an ans" not in str(request.messages)
