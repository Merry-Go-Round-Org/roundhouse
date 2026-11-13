"""Agent loop: finish reasons, tool handling, retries, limits, audit and cancellation."""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy.exc import OperationalError

from harness.dev.gateway import (
    FakeGatewayFactory,
    ScriptedLlm,
    text_turn,
    tool_turn,
)
from harness.loop import (
    INVALID_ARGUMENTS_TEXT,
    NOT_ALLOWED_TEXT,
    NOT_EXECUTED_TEXT,
)
from harness.schemas import LlmCompletion, LlmError, LlmUsage, TextDelta
from harness.store import UNKNOWN_RESULT_TEXT
from harness_helpers import Harness, fast_settings, make_request, with_profile


def play(database, llm, scenario=None, *, settings=None, gateways=None, request=None):
    async def main():
        async with database as sessions:
            h = Harness(sessions, llm, settings, gateways)
            if scenario is not None:
                return await scenario(h)
            task_id, result = await h.start(request)
            return h, task_id, result, await h.store.load_messages(task_id), await h.store.list_llm_calls(task_id)

    return asyncio.run(main())


def retryable(code="http_error", status=503):
    return LlmError(code, http_status=status, retryable=True, no_charge=status is not None)


# ---------------------------------------------------------------- finish reasons
def test_stop_with_text_completes(database):
    h, task_id, result, messages, calls = play(database, ScriptedLlm([text_turn("Hello there")]))
    assert result.status == "completed" and result.text == "Hello there"
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].origin == "llm"
    assert h.event_types() == ["task_started", "thinking", "token", "token", "llm_turn", "done"]
    assert h.events[-1].data == {"status": "completed"}
    assert [e.seq for e in h.events] == list(range(1, len(h.events) + 1))
    assert calls[0]["status"] == "completed" and calls[0]["finish_reason"] == "stop"


@pytest.mark.parametrize("text", ["", "   \n"])
def test_stop_without_text_fails(database, text):
    turn = [LlmCompletion(text=text, finish_reason="stop")]
    h, _, result, messages, _ = play(database, ScriptedLlm([turn]))
    assert (result.status, result.error_code) == ("failed", "empty_response")
    assert [m.role for m in messages] == ["user"]
    assert h.event_types()[-2:] == ["error", "done"]


def test_length_fails_and_saves_nothing(database):
    llm = ScriptedLlm([text_turn("cut off", finish_reason="length")])
    _, _, result, messages, calls = play(database, llm, request=make_request("go", kind="document"))
    assert (result.status, result.error_code) == ("failed", "output_truncated")
    assert [m.role for m in messages] == ["user"]
    assert len(llm.requests) == 1  # no automatic retry
    assert calls[0]["finish_reason"] == "length" and calls[0]["status"] == "completed"


def test_chat_keeps_a_truncated_text_reply_with_a_note(database):
    # The user already saw the streamed text; failing the turn threw it away (Flow C C7.5).
    llm = ScriptedLlm([text_turn("A long answer that", finish_reason="length")])
    _, _, result, messages, _ = play(database, llm)
    assert result.status == "completed"
    assert result.text.startswith("A long answer that") and "cut off at the length limit" in result.text
    assert [m.role for m in messages] == ["user", "assistant"]
    assert len(llm.requests) == 1


def test_truncated_tool_call_is_not_executed(database):
    turn = tool_turn(("get_model_status", {"model_id": "m"}))
    turn[0].finish_reason = "length"
    gateways = FakeGatewayFactory()
    _, _, result, messages, _ = play(database, ScriptedLlm([turn]), gateways=gateways)
    assert result.error_code == "output_truncated"
    assert gateways.calls == []
    assert [m.role for m in messages] == ["user"]


@pytest.mark.parametrize("reason", ["content_filter", "error", None, "weird"])
def test_other_finish_reasons_fail(database, reason):
    _, _, result, messages, _ = play(
        database, ScriptedLlm([[LlmCompletion(text="x", finish_reason=reason)]])
    )
    assert (result.status, result.error_code) == ("failed", "unexpected_finish_reason")
    assert [m.role for m in messages] == ["user"]


def test_tool_calls_finish_reason_without_calls_fails(database):
    _, _, result, _, _ = play(
        database, ScriptedLlm([[LlmCompletion(finish_reason="tool_calls")]])
    )
    assert result.error_code == "unexpected_finish_reason"


def test_stop_with_tool_calls_still_runs_them(database):
    turn = tool_turn(("get_model_status", {}))
    turn[0].finish_reason = "stop"
    gateways = FakeGatewayFactory()
    _, _, result, _, _ = play(
        database, ScriptedLlm([turn, text_turn("done")]), gateways=gateways
    )
    assert result.status == "completed"
    assert [c.name for c in gateways.calls] == ["get_model_status"]


# ---------------------------------------------------------------- tool statuses
def test_ok_and_error_results_are_saved_and_returned_to_the_llm(database):
    llm = ScriptedLlm(
        [tool_turn(("get_model_status", {"model_id": "m1"}), ("failing_tool", {})), text_turn("done")]
    )
    gateways = FakeGatewayFactory()
    h, task_id, result, messages, _ = play(database, llm, gateways=gateways)
    assert result.status == "completed"
    assert [m.role for m in messages] == ["user", "assistant", "tool", "tool", "assistant"]
    assert json.loads(messages[2].content)["args"] == {"model_id": "m1"}
    assert messages[3].content == "The tool failed."
    second = llm.requests[1].messages
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool", "tool"]
    assert second[3]["tool_call_id"] == messages[1].tool_calls[0].id
    assert [c.idempotency_key for c in gateways.calls] == [f"{task_id}:1:0", f"{task_id}:1:1"]
    assert [e.data["status"] for e in h.events_of("tool_result")] == ["ok", "error"]


def test_invalid_json_and_non_object_arguments_never_reach_the_gateway(database):
    llm = ScriptedLlm(
        [tool_turn(("get_model_status", "{bad"), ("get_model_status", "[1, 2]")), text_turn("sorry")]
    )
    gateways = FakeGatewayFactory()
    _, _, result, messages, _ = play(database, llm, gateways=gateways)
    assert gateways.calls == []
    assert result.status == "completed"
    assert [m.content for m in messages[2:4]] == [INVALID_ARGUMENTS_TEXT] * 2
    # The next request carries valid JSON, since some providers reject bad history.
    arguments = [c["function"]["arguments"] for c in llm.requests[1].messages[2]["tool_calls"]]
    assert arguments == ["{}", "{}"]


def test_disallowed_tool_never_reaches_the_gateway(database):
    # Outside the rebuild allowlist, even though the gateway offers it.
    llm = ScriptedLlm([tool_turn(("delete_model", {})), text_turn("sorry")])
    gateways = FakeGatewayFactory()
    _, _, result, messages, _ = play(
        database, llm, gateways=gateways, request=make_request("go", kind="rebuild")
    )
    assert gateways.calls == []
    assert messages[2].content == NOT_ALLOWED_TEXT
    assert result.status == "completed"


def test_unknown_tool_is_denied_for_chat_too(database):
    llm = ScriptedLlm([tool_turn(("made_up_tool", {})), text_turn("sorry")])
    gateways = FakeGatewayFactory()
    _, _, _, messages, _ = play(database, llm, gateways=gateways)
    assert gateways.calls == []
    assert messages[2].content == NOT_ALLOWED_TEXT


def test_gateway_denial_is_saved_and_not_retried(database):
    llm = ScriptedLlm(
        [
            tool_turn(("delete_model", {"model_id": "m"})),
            tool_turn(("delete_model", {"model_id": "m"})),
            text_turn("I am not allowed to do that."),
        ]
    )
    gateways = FakeGatewayFactory()
    _, _, result, messages, _ = play(database, llm, gateways=gateways)
    assert len(gateways.calls) == 1
    assert messages[2].content == "You may not delete models."
    assert "already denied" in messages[4].content
    assert result.status == "completed"


@pytest.mark.parametrize(
    "tool, status, event",
    [
        ("update_model_owner", "awaiting_confirmation", "confirmation_required"),
        ("submit_review_decision", "awaiting_review", "review_required"),
    ],
)
def test_pause_saves_placeholder_and_marks_later_calls_not_executed(database, tool, status, event):
    args = {"owner": "bob", "decision": "approve"}
    llm = ScriptedLlm(
        [tool_turn(("get_model_status", {}), (tool, args), ("get_model_status", {"x": 1}))]
    )
    gateways = FakeGatewayFactory()
    h, _, result, messages, _ = play(database, llm, gateways=gateways)
    assert result.status == status
    assert result.pending_id
    assert [c.name for c in gateways.calls] == ["get_model_status", tool]  # in order, then stop
    assert [m.role for m in messages] == ["user", "assistant", "tool", "tool", "tool"]
    assert "Paused" in messages[3].content
    assert messages[4].content == NOT_EXECUTED_TEXT
    # The UI card gets the real tool and arguments, not LLM text.
    card = h.events_of(event)[0].data
    assert card["name"] == tool and card["arguments"] == args
    assert card["pending_id"] == result.pending_id
    assert len(llm.requests) == 1


def test_gateway_exception_becomes_result_unknown(database):
    llm = ScriptedLlm([tool_turn(("exploding_tool", {})), text_turn("checking")])
    _, _, result, messages, _ = play(database, llm)
    assert messages[2].content == UNKNOWN_RESULT_TEXT
    assert "internal gateway detail" not in repr(messages)
    assert result.status == "completed"


def test_long_tool_result_is_truncated_with_a_notice(database):
    llm = ScriptedLlm([tool_turn(("huge_tool", {})), text_turn("done")])
    _, _, _, messages, _ = play(database, llm)
    content = messages[2].content
    assert content.startswith("x" * 20_000)
    assert len(content) < 20_100
    assert "Truncated: 30000 characters omitted" in content


# ---------------------------------------------------------------- retries
def test_retry_before_the_first_token_with_one_audit_row_per_attempt(database):
    llm = ScriptedLlm([[retryable()], [retryable("network_error", None)], text_turn("recovered")])
    h, _, result, _, calls = play(database, llm)
    assert result.status == "completed" and result.text == "recovered"
    assert [(c["attempt"], c["status"]) for c in calls] == [
        (1, "failed"),
        (2, "failed"),
        (3, "completed"),
    ]
    assert calls[0]["http_status"] == 503 and calls[0]["error_code"] == "http_error"
    assert {c["turn"] for c in calls} == {1}
    assert len(h.events_of("thinking")) == 3


def test_retries_are_limited(database):
    llm = ScriptedLlm([[retryable()], [retryable()], [retryable()], text_turn("never")])
    _, _, result, _, calls = play(database, llm)
    assert (result.status, result.error_code) == ("failed", "llm_stream_error")
    assert len(calls) == 3
    assert len(llm.requests) == 3


def test_no_retry_after_a_token(database):
    llm = ScriptedLlm([[TextDelta(text="par"), retryable("provider_error", None)], text_turn("never")])
    h, _, result, messages, calls = play(database, llm)
    assert (result.status, result.error_code) == ("failed", "llm_stream_error")
    assert len(llm.requests) == 1 and len(calls) == 1
    assert [m.role for m in messages] == ["user"]
    assert calls[0]["first_chunk_ms"] is not None


def test_background_task_retries_after_a_token(database):
    # A document run's tokens reach no user, so a stream that stalls mid-answer is retried.
    llm = ScriptedLlm([[TextDelta(text="par"), retryable("idle_timeout", None)], text_turn("recovered")])
    _, _, result, _, calls = play(database, llm, request=make_request("go", kind="document"))
    assert result.status == "completed" and result.text == "recovered"
    assert [(c["attempt"], c["status"]) for c in calls] == [(1, "failed"), (2, "completed")]


def test_other_4xx_is_rejected_without_retry(database):
    llm = ScriptedLlm([[LlmError("request_rejected", http_status=400, no_charge=True)], text_turn("never")])
    _, _, result, _, calls = play(database, llm)
    assert (result.status, result.error_code) == ("failed", "llm_request_rejected")
    assert len(llm.requests) == 1
    assert calls[0]["cost_usd"] == 0 and not calls[0]["cost_estimated"]


def test_timeout_is_handled_like_a_network_error(database):
    llm = ScriptedLlm([[LlmError("idle_timeout", retryable=True)], text_turn("back")])
    _, _, result, _, calls = play(database, llm)
    assert result.status == "completed"
    assert calls[0]["error_code"] == "idle_timeout"
    assert calls[0]["cost_estimated"] and calls[0]["cost_usd"] > 0  # may have been billed


def test_backoff_grows_exponentially(database, monkeypatch):
    delays = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args):
        # Only the backoff is skipped. The heartbeat must really wait, or it would spin.
        if delay in (0.5, 1.0):
            delays.append(delay)
            delay = 0
        await real_sleep(delay, *args)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    llm = ScriptedLlm([[retryable()], [retryable()], text_turn("ok")])
    _, _, result, _, _ = play(database, llm, settings=fast_settings(retry_base_delay_s=0.5))
    assert result.status == "completed"
    assert delays == [0.5, 1.0]


# ---------------------------------------------------------------- limits
def test_turn_limit(database):
    llm = ScriptedLlm(lambda _r: tool_turn(("get_model_status", {})))
    settings = with_profile(fast_settings(), "chat", max_turns=3)
    _, _, result, _, calls = play(database, llm, settings=settings)
    assert (result.status, result.error_code) == ("failed", "turn_limit_exceeded")
    assert len(llm.requests) == 3
    assert [c["turn"] for c in calls] == [1, 2, 3]


def _costly(cost):
    return LlmUsage(prompt_tokens=10, completion_tokens=5, cost_usd=cost)


def test_task_budget_stops_after_passing_by_at_most_one_turn(database):
    llm = ScriptedLlm(lambda _r: tool_turn(("get_model_status", {}), usage=_costly(0.03)))
    _, _, result, _, _ = play(database, llm)  # chat budget is $0.05
    assert (result.status, result.error_code) == ("failed", "budget_exhausted")
    assert len(llm.requests) == 2
    assert result.usage.cost_usd == pytest.approx(0.06)


def test_thread_budget_spans_continuations(database):
    settings = with_profile(fast_settings(), "chat", thread_budget_usd=0.04)
    llm = ScriptedLlm(lambda _r: text_turn("ok", usage=_costly(0.04)))

    async def scenario(h):
        first, result = await h.start()
        assert result.status == "completed"
        _, second = await h.start(make_request("again"), continue_from=first)
        return second

    second = play(database, llm, scenario, settings=settings)
    assert (second.status, second.error_code) == ("failed", "budget_exhausted")
    assert len(llm.requests) == 1


def test_global_budget_fails_new_calls_in_other_threads(database):
    llm = ScriptedLlm(lambda _r: text_turn("ok", usage=_costly(0.04)))

    async def scenario(h):
        _, first = await h.start()
        _, second = await h.start()
        return first, second

    first, second = play(
        database, llm, scenario, settings=fast_settings(global_budget_usd=0.04)
    )
    assert first.status == "completed"
    assert (second.status, second.error_code) == ("failed", "budget_exhausted")


def test_context_limit_uses_the_previous_prompt_tokens(database):
    big = LlmUsage(prompt_tokens=100_001, completion_tokens=1, cost_usd=0.0)
    llm = ScriptedLlm([tool_turn(("get_model_status", {}), usage=big), text_turn("never")])
    _, _, result, _, _ = play(database, llm)
    assert (result.status, result.error_code) == ("failed", "context_too_large")
    assert len(llm.requests) == 1


def test_effort_and_limits_come_from_the_profile(database):
    llm = ScriptedLlm(lambda _r: text_turn("ok"))

    async def scenario(h):
        await h.start(make_request(kind="chat"))
        await h.start(make_request(kind="document"))
        await h.start(make_request(kind="rebuild"))

    play(database, llm, scenario)
    assert [(r.reasoning_effort, r.max_tokens, r.idle_timeout_s, r.turn_timeout_s) for r in llm.requests] == [
        ("low", 4_000, 45, 120),
        ("low", 6_500, 120, 300),
        ("medium", 8_000, 120, 300),
    ]
    assert llm.requests[0].model == "qwen/qwen3.8-27b"


# ---------------------------------------------------------------- cost
def test_missing_usage_is_estimated_from_max_tokens_and_flagged(database):
    llm = ScriptedLlm([text_turn("ok", usage=None)])
    _, _, result, _, calls = play(database, llm)
    assert result.status == "completed"
    assert calls[0]["cost_estimated"] is True
    # Output at max_tokens (4,000 for chat) dominates the estimate.
    assert calls[0]["cost_usd"] >= 4_000 * 1.875 / 1_000_000
    assert result.usage.unknown_cost_calls == 1
    assert result.usage.cost_usd == pytest.approx(calls[0]["cost_usd"])


def test_usage_without_cost_is_priced_from_config_and_flagged(database):
    usage = LlmUsage(prompt_tokens=1_000_000, completion_tokens=0, cached_tokens=1_000_000)
    _, _, result, _, calls = play(database, ScriptedLlm([text_turn("ok", usage=usage)]))
    assert calls[0]["cost_usd"] == pytest.approx(0.0375)
    assert calls[0]["cost_estimated"] is True
    assert result.usage.unknown_cost_calls == 1


def test_reported_cost_is_used_as_is(database):
    usage = LlmUsage(prompt_tokens=10, completion_tokens=5, reasoning_tokens=2, cost_usd=0.002)
    _, _, result, _, calls = play(database, ScriptedLlm([text_turn("ok", usage=usage)]))
    assert calls[0]["cost_usd"] == 0.002 and calls[0]["cost_estimated"] is False
    assert result.usage.unknown_cost_calls == 0
    assert result.usage.reasoning_tokens == 2


def test_audit_row_records_the_request(database):
    _, task_id, _, _, calls = play(database, ScriptedLlm([text_turn("ok")]))
    call = calls[0]
    assert call["task_id"] == task_id and call["actor_id"] == "alice"
    assert call["requested_model"] == "qwen/qwen3.8-27b"
    assert json.loads(call["provider_routing_json"])["data_collection"] == "deny"
    assert call["reasoning_effort"] == "low" and call["max_tokens"] == 4_000
    assert len(call["messages_hash"]) == 64 and len(call["tools_hash"]) == 64
    assert call["input_seq"] == 1
    assert call["returned_model"] == "scripted/model" and call["provider"] == "scripted"
    assert call["generation_id"].startswith("gen-")
    assert call["http_status"] == 200 and call["total_ms"] is not None


# ---------------------------------------------------------------- audit failures
class _BrokenStore:
    def __init__(self, store, fail_on):
        self._store = store
        self._fail_on = fail_on

    def __getattr__(self, name):
        attribute = getattr(self._store, name)
        if name != self._fail_on:
            return attribute

        async def broken(*_args, **_kwargs):
            raise OperationalError("INSERT", {}, Exception("database is down"))

        return broken


@pytest.mark.parametrize("fail_on", ["start_llm_call", "finish_llm_call"])
def test_failed_audit_writes_stop_the_task(database, fail_on):
    llm = ScriptedLlm(lambda _r: tool_turn(("get_model_status", {})))
    gateways = FakeGatewayFactory()

    async def scenario(h):
        h.store = _BrokenStore(h.store, fail_on)
        return await h.start()

    _, result = play(database, llm, scenario, gateways=gateways)
    assert (result.status, result.error_code) == ("failed", "audit_write_failed")
    assert len(llm.requests) == (0 if fail_on == "start_llm_call" else 1)
    assert gateways.calls == []  # an unaudited response is never acted on


# ---------------------------------------------------------------- cancellation
def _stall(started: asyncio.Event | None = None):
    async def stall():
        if started is not None:
            started.set()
        await asyncio.sleep(3600)

    return stall


def test_cancel_during_an_llm_stream(database):
    async def scenario(h):
        started = asyncio.Event()
        h.llm = ScriptedLlm([[TextDelta(text="partial"), _stall(started)]])
        task_id = await h.create(make_request())
        running = asyncio.create_task(h.run(task_id))
        await asyncio.wait_for(started.wait(), 5)
        h.cancel.set()
        result = await asyncio.wait_for(running, 5)
        return h, result, await h.store.load_messages(task_id), await h.store.list_llm_calls(task_id)

    h, result, messages, calls = play(database, None, scenario)
    assert result.status == "cancelled"
    assert [m.role for m in messages] == ["user"]  # cancelled output is discarded
    assert calls[0]["status"] == "cancelled"
    assert calls[0]["cost_estimated"] is True and calls[0]["cost_usd"] > 0
    assert h.events[-1].data == {"status": "cancelled"}
    assert result.usage.unknown_cost_calls == 1


def test_cancel_during_a_tool_call(database):
    started = asyncio.Event()

    async def on_call(_name):
        started.set()
        await asyncio.sleep(3600)

    gateways = FakeGatewayFactory(on_call=on_call)

    async def scenario(h):
        h.llm = ScriptedLlm([tool_turn(("get_model_status", {}), ("get_model_status", {"a": 1}))])
        task_id = await h.create(make_request())
        running = asyncio.create_task(h.run(task_id))
        await asyncio.wait_for(started.wait(), 5)
        h.cancel.set()
        result = await asyncio.wait_for(running, 5)
        return result, await h.store.load_messages(task_id)

    result, messages = play(database, None, scenario, gateways=gateways)
    assert result.status == "cancelled"
    assert len(gateways.calls) == 1  # the second call never started
    assert gateways.closed == [gateways.opened[0].task_id]  # the gateway is always closed
    assert [m.role for m in messages] == ["user", "assistant"]


def test_cancel_during_backoff(database):
    async def scenario(h):
        h.llm = ScriptedLlm([[retryable()], text_turn("never")])
        h.settings = fast_settings(retry_base_delay_s=3600)
        task_id = await h.create(make_request())
        running = asyncio.create_task(h.run(task_id))
        while not await h.store.list_llm_calls(task_id):
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        h.cancel.set()
        return await asyncio.wait_for(running, 5), h

    result, h = play(database, None, scenario)
    assert result.status == "cancelled"
    assert len(h.llm.requests) == 1


def test_cancel_requested_in_the_database_stops_at_the_next_boundary(database):
    async def scenario(h):
        task_id = await h.create(make_request())

        async def on_call(_name):
            await h.store.request_cancel(task_id)  # as if from another process

        h.gateways = FakeGatewayFactory(on_call=on_call)
        h.llm = ScriptedLlm(
            [tool_turn(("get_model_status", {}), ("get_model_status", {"a": 1})), text_turn("never")]
        )
        return await h.run(task_id), h

    result, h = play(database, None, scenario)
    assert result.status == "cancelled"
    assert len(h.gateways.calls) == 1
    assert len(h.llm.requests) == 1


def test_task_marked_interrupted_elsewhere_stops_and_stays_interrupted(database):
    async def scenario(h):
        task_id = await h.create(make_request())

        async def on_call(_name):
            await asyncio.sleep(0.02)
            await h.store.interrupt_orphans(0)

        h.gateways = FakeGatewayFactory(on_call=on_call)
        h.llm = ScriptedLlm([tool_turn(("get_model_status", {})), text_turn("never")])
        return await h.run(task_id), h

    result, h = play(database, None, scenario)
    assert result.status == "interrupted"
    assert len(h.llm.requests) == 1
    assert h.events[-1].data == {"status": "interrupted"}


def test_outer_task_cancellation_records_the_end_state(database):
    async def scenario(h):
        started = asyncio.Event()
        h.llm = ScriptedLlm([[_stall(started)]])
        task_id = await h.create(make_request())
        running = asyncio.create_task(h.run(task_id))
        await asyncio.wait_for(started.wait(), 5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        return await h.store.get_task(task_id), await h.store.list_llm_calls(task_id)

    snapshot, calls = play(database, None, scenario)
    assert snapshot.status == "cancelled"  # a disconnect never marks a task as completed
    assert calls[0]["status"] == "cancelled"


def test_cancelled_before_start_makes_no_llm_call(database):
    llm = ScriptedLlm([text_turn("never")])

    async def scenario(h):
        h.cancel.set()
        return await h.start()

    _, result = play(database, llm, scenario)
    assert result.status == "cancelled"
    assert llm.requests == []


def test_task_that_cannot_be_claimed_does_not_run(database):
    llm = ScriptedLlm([text_turn("never")])

    async def scenario(h):
        task_id = await h.create(make_request())
        await h.store.cancel_queued(task_id)
        return await h.run(task_id), h

    result, h = play(database, llm, scenario)
    assert result.status == "cancelled"
    assert llm.requests == []
    assert h.event_types() == ["done"]


# ---------------------------------------------------------------- other behaviour
def test_heartbeat_keeps_a_long_task_from_being_marked_as_an_orphan(database):
    async def scenario(h):
        h.settings = fast_settings(heartbeat_interval_s=0.02)

        async def long_call():
            for _ in range(8):
                await asyncio.sleep(0.05)
                # A recovery pass from another process, with a short orphan timeout.
                assert await h.store.interrupt_orphans(0.15) == 0

        h.llm = ScriptedLlm([[long_call, *text_turn("finished")]])
        return await h.start()

    _, result = play(database, None, scenario)
    assert result.status == "completed"


def test_nothing_keeps_running_after_run_task_returns(database):
    # A stray heartbeat writing during engine disposal can hang the event loop on close.
    async def scenario(h):
        h.settings = fast_settings(heartbeat_interval_s=0.001)

        async def pause():
            await asyncio.sleep(0.02)

        h.llm = ScriptedLlm([[pause, *text_turn("ok")]])
        await h.start()
        return asyncio.all_tasks() - {asyncio.current_task()}

    assert play(database, None, scenario) == set()


def test_broken_event_consumer_does_not_stop_the_task(database):
    async def scenario(h):
        task_id = await h.create(make_request())
        from harness.loop import run_task

        def broken(_event):
            raise RuntimeError("consumer died")

        return await run_task(
            task_id,
            store=h.store,
            llm=h.llm,
            gateways=h.gateways,
            emit=broken,
            cancel=h.cancel,
            settings=h.settings,
        )

    result = play(database, ScriptedLlm([text_turn("ok")]), scenario)
    assert result.status == "completed"


def test_sandbox_unavailable_fails_the_task(database):
    llm = ScriptedLlm([text_turn("never")])
    gateways = FakeGatewayFactory(sandbox_available=False)
    _, _, result, _, _ = play(
        database, llm, gateways=gateways, request=make_request("go", kind="rebuild")
    )
    assert (result.status, result.error_code) == ("failed", "sandbox_unavailable")
    assert llm.requests == []


def test_stream_with_no_completion_is_retried_like_a_network_error(database):
    llm = ScriptedLlm([[], text_turn("back")])
    _, _, result, _, calls = play(database, llm)
    assert result.status == "completed"
    assert calls[0]["error_code"] == "stream_ended_early"


def test_unexpected_exception_is_an_internal_error(database):
    llm = ScriptedLlm([[RuntimeError("secret detail")]])
    _, _, result, _, _ = play(database, llm)
    assert (result.status, result.error_code) == ("failed", "internal_error")
    assert "secret detail" not in repr(result)
