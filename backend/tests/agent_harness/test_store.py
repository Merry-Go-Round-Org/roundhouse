"""Store: state transitions, one active task per thread, idempotency, messages, audit rows."""
from __future__ import annotations

import asyncio
import inspect

import pytest

from harness.profiles import CHAT_PROFILE, REBUILD_PROFILE
from harness.schemas import (
    END_STATUSES,
    Actor,
    InvalidTaskRequest,
    LlmCallAlreadyFinalized,
    LlmCallOutcome,
    LlmCallStart,
    NewMessage,
    NotContinuable,
    Resolution,
    TaskContext,
    TaskNotFound,
    TaskRequest,
    ThreadBusy,
    ToolCall,
)
from harness.store import UNKNOWN_RESULT_TEXT, SqlTaskStore


def request(request_id="r1", actor="alice", kind="chat", prompt="hi", resolution=None):
    return TaskRequest(
        request_id=request_id,
        actor=Actor(id=actor, role="developer"),
        kind=kind,
        prompt=prompt,
        context=TaskContext(policy_version="p1"),
        resolution=resolution,
    )


def in_store(database, scenario):
    async def main():
        async with database as sessions:
            return await scenario(SqlTaskStore(sessions))

    return asyncio.run(main())


def start(task_id, thread_id, attempt=1):
    return LlmCallStart(
        task_id=task_id,
        thread_id=thread_id,
        actor_id="alice",
        turn=1,
        attempt=attempt,
        requested_model="m",
        provider_routing={"data_collection": "deny"},
        reasoning_effort="low",
        max_tokens=100,
        messages_hash="a",
        tools_hash="b",
        input_seq=1,
    )


async def ended(store, request_id="r1", status="completed", **kwargs):
    task_id, _ = await store.create_task(request(request_id, **kwargs), CHAT_PROFILE)
    assert await store.claim_task(task_id, "owner")
    assert await store.finish_task(task_id, status)
    return task_id


def test_happy_path_transitions(database):
    async def scenario(store):
        task_id, created = await store.create_task(request(), CHAT_PROFILE)
        assert created
        snap = await store.get_task(task_id)
        assert snap.status == "queued"
        assert snap.thread_id == task_id
        assert snap.workspace_id is None
        assert snap.profile_name == CHAT_PROFILE.name
        assert snap.profile_hash == CHAT_PROFILE.hash
        assert await store.claim_task(task_id, "owner")
        assert (await store.get_task(task_id)).owner_id == "owner"
        assert await store.finish_task(task_id, "completed", result_text="done")
        snap = await store.get_task(task_id)
        assert (snap.status, snap.result_text) == ("completed", "done")
        assert snap.finished_at is not None

    in_store(database, scenario)


@pytest.mark.parametrize("status", END_STATUSES)
def test_every_end_state_is_reachable_and_final(database, status):
    async def scenario(store):
        task_id = await ended(store, status=status)
        assert not await store.claim_task(task_id, "owner")
        assert not await store.finish_task(task_id, "completed")
        assert not await store.touch(task_id)
        assert (await store.get_task(task_id)).status == status

    in_store(database, scenario)


def test_rejected_transitions(database):
    async def scenario(store):
        task_id, _ = await store.create_task(request(), CHAT_PROFILE)
        # A queued task cannot be finished directly, and only cancelled through cancel_queued.
        assert not await store.finish_task(task_id, "completed")
        with pytest.raises(ValueError):
            await store.finish_task(task_id, "running")
        assert await store.claim_task(task_id, "a")
        assert not await store.claim_task(task_id, "b")  # only one owner wins
        assert not await store.cancel_queued(task_id)
        assert (await store.get_task(task_id)).owner_id == "a"

    in_store(database, scenario)


def test_cancel_queued(database):
    async def scenario(store):
        task_id, _ = await store.create_task(request(), CHAT_PROFILE)
        assert await store.cancel_queued(task_id)
        assert (await store.get_task(task_id)).status == "cancelled"
        assert not await store.claim_task(task_id, "a")

    in_store(database, scenario)


def test_concurrent_claims_have_one_winner(database):
    async def scenario(store):
        task_id, _ = await store.create_task(request(), CHAT_PROFILE)
        wins = await asyncio.gather(*(store.claim_task(task_id, f"o{i}") for i in range(10)))
        assert sum(wins) == 1

    in_store(database, scenario)


def test_repeated_request_id_returns_the_same_task(database):
    async def scenario(store):
        first, created = await store.create_task(request(), CHAT_PROFILE)
        second, created_again = await store.create_task(request(), CHAT_PROFILE)
        assert first == second and created and not created_again
        assert len(await store.load_messages(first)) == 1

    in_store(database, scenario)


def test_concurrent_repeats_of_a_request_id_create_one_task(database):
    async def scenario(store):
        results = await asyncio.gather(
            *(store.create_task(request(), CHAT_PROFILE) for _ in range(8))
        )
        assert len({task_id for task_id, _ in results}) == 1
        assert sum(created for _, created in results) == 1

    in_store(database, scenario)


def test_one_active_task_per_thread(database):
    async def scenario(store):
        parent = await ended(store)
        await store.create_task(request("r2"), CHAT_PROFILE, continue_from=parent)
        with pytest.raises(ThreadBusy):
            await store.create_task(request("r3"), CHAT_PROFILE, continue_from=parent)

    in_store(database, scenario)


def test_active_index_is_enforced_by_the_database(database):
    from sqlalchemy.exc import IntegrityError

    from harness.db import AgentTask

    async def scenario(store):
        task_id, _ = await store.create_task(request(), CHAT_PROFILE)
        snapshot = await store.get_task(task_id)
        with pytest.raises(IntegrityError):
            async with store._sessions.begin() as session:
                session.add(
                    AgentTask(
                        id="other",
                        thread_id=snapshot.thread_id,
                        request_id="other",
                        actor_id="alice",
                        actor_role="developer",
                        kind="chat",
                        policy_version="p1",
                        context_json="{}",
                        profile_name="x",
                        profile_hash="x",
                        status="running",
                        created_at=snapshot.created_at,
                    )
                )

    in_store(database, scenario)


def test_continuing_an_active_task_is_busy(database):
    async def scenario(store):
        task_id, _ = await store.create_task(request(), CHAT_PROFILE)
        with pytest.raises(ThreadBusy):
            await store.create_task(request("r2"), CHAT_PROFILE, continue_from=task_id)

    in_store(database, scenario)


def test_continuing_an_older_task_is_rejected(database):
    async def scenario(store):
        first = await ended(store)
        second, _ = await store.create_task(request("r2"), CHAT_PROFILE, continue_from=first)
        await store.claim_task(second, "o")
        await store.finish_task(second, "completed")
        with pytest.raises(NotContinuable, match="newest"):
            await store.create_task(request("r3"), CHAT_PROFILE, continue_from=first)

    in_store(database, scenario)


def test_continuing_another_users_thread_is_rejected(database):
    async def scenario(store):
        parent = await ended(store)
        with pytest.raises(NotContinuable, match="another actor"):
            await store.create_task(
                request("r2", actor="mallory"), CHAT_PROFILE, continue_from=parent
            )

    in_store(database, scenario)


def test_continuing_with_another_kind_is_rejected(database):
    async def scenario(store):
        parent = await ended(store)
        with pytest.raises(NotContinuable, match="same kind"):
            await store.create_task(
                request("r2", kind="rebuild"), REBUILD_PROFILE, continue_from=parent
            )

    in_store(database, scenario)


def test_continuing_an_unknown_task_is_rejected(database):
    async def scenario(store):
        with pytest.raises(TaskNotFound):
            await store.create_task(request(), CHAT_PROFILE, continue_from="missing")

    in_store(database, scenario)


def test_continuation_shares_thread_and_workspace(database):
    async def scenario(store):
        first, _ = await store.create_task(request(kind="rebuild"), REBUILD_PROFILE)
        await store.claim_task(first, "o")
        await store.finish_task(first, "failed", error_code="output_truncated")
        second, _ = await store.create_task(
            request("r2", kind="rebuild"), REBUILD_PROFILE, continue_from=first
        )
        a, b = await store.get_task(first), await store.get_task(second)
        assert a.workspace_id and a.workspace_id == b.workspace_id
        assert b.thread_id == first and b.parent_task_id == first

    in_store(database, scenario)


def test_input_rules(database):
    async def scenario(store):
        with pytest.raises(InvalidTaskRequest):  # chat has no default prompt
            await store.create_task(request(prompt=None), CHAT_PROFILE)
        with pytest.raises(InvalidTaskRequest):
            await store.create_task(request(prompt="  "), CHAT_PROFILE)
        with pytest.raises(InvalidTaskRequest):  # wrong profile
            await store.create_task(request(), REBUILD_PROFILE)
        with pytest.raises(InvalidTaskRequest):  # a resolution needs a task to continue
            await store.create_task(
                request(resolution=Resolution(pending_id="p", outcome="approved")), CHAT_PROFILE
            )
        # A rebuild from a UI button has no prompt and uses the profile's.
        task_id, _ = await store.create_task(
            request("button", kind="rebuild", prompt=None), REBUILD_PROFILE
        )
        messages = await store.load_messages(task_id)
        assert messages[0].content == REBUILD_PROFILE.default_prompt
        # A continuation must bring new input.
        parent = await ended(store, "r5")
        with pytest.raises(InvalidTaskRequest):
            await store.create_task(request("r6", prompt=None), CHAT_PROFILE, continue_from=parent)

    in_store(database, scenario)


def test_resolution_must_match_the_pending_action(database):
    async def scenario(store):
        task_id, _ = await store.create_task(request(), CHAT_PROFILE)
        await store.claim_task(task_id, "o")
        await store.finish_task(task_id, "awaiting_confirmation", pending_id="p1")
        wrong = Resolution(pending_id="p2", outcome="approved")
        with pytest.raises(NotContinuable, match="pending action"):
            await store.create_task(
                request("r2", prompt=None, resolution=wrong), CHAT_PROFILE, continue_from=task_id
            )
        right = Resolution(pending_id="p1", outcome="rejected", detail="Too risky.")
        child, _ = await store.create_task(
            request("r3", prompt=None, resolution=right), CHAT_PROFILE, continue_from=task_id
        )
        last = (await store.load_messages(task_id))[-1]
        assert last.origin == "resolution" and last.task_id == child
        assert "rejected" in last.content and "Too risky." in last.content

    in_store(database, scenario)


def test_message_seq_is_ordered_and_unique(database):
    async def scenario(store):
        task_id, _ = await store.create_task(request(), CHAT_PROFILE)
        await asyncio.gather(
            *(
                store.append_messages(
                    task_id,
                    task_id,
                    [NewMessage(role="user", origin="user", content=f"m{i}")],
                )
                for i in range(25)
            )
        )
        messages = await store.load_messages(task_id)
        assert [m.seq for m in messages] == list(range(1, 27))
        # The store exposes no way to change or remove a message.
        names = {n for n, _ in inspect.getmembers(SqlTaskStore, inspect.isfunction)}
        assert not {n for n in names if "message" in n and n.split("_")[0] in {"update", "delete"}}

    in_store(database, scenario)


def test_repair_answers_only_unresolved_calls_in_order(database):
    async def scenario(store):
        task_id, _ = await store.create_task(request(), CHAT_PROFILE)
        calls = [ToolCall(id=f"c{i}", name="t", arguments="{}") for i in range(3)]
        await store.append_messages(
            task_id,
            task_id,
            [
                NewMessage(role="assistant", origin="llm", tool_calls=calls),
                NewMessage(role="tool", origin="tool", content="ok", tool_call_id="c0"),
            ],
        )
        assert await store.repair_unresolved(task_id, task_id) == 2
        assert await store.repair_unresolved(task_id, task_id) == 0
        tail = (await store.load_messages(task_id))[-2:]
        assert [m.tool_call_id for m in tail] == ["c1", "c2"]
        assert all(m.content == UNKNOWN_RESULT_TEXT and m.origin == "recovery" for m in tail)

    in_store(database, scenario)


def test_continuation_repairs_before_appending_the_new_prompt(database):
    async def scenario(store):
        parent, _ = await store.create_task(request(), CHAT_PROFILE)
        await store.claim_task(parent, "o")
        await store.append_messages(
            parent,
            parent,
            [
                NewMessage(
                    role="assistant",
                    origin="llm",
                    tool_calls=[ToolCall(id="c0", name="t", arguments="{}")],
                )
            ],
        )
        await store.interrupt_orphans(-1)
        await store.create_task(request("r2", prompt="next"), CHAT_PROFILE, continue_from=parent)
        roles = [m.role for m in await store.load_messages(parent)]
        assert roles == ["user", "assistant", "tool", "user"]

    in_store(database, scenario)


def test_llm_call_can_be_finalized_only_once(database):
    async def scenario(store):
        task_id, _ = await store.create_task(request(), CHAT_PROFILE)
        call_id = await store.start_llm_call(start(task_id, task_id))
        rows = await store.list_llm_calls(task_id)
        assert rows[0]["status"] == "started"
        await store.finish_llm_call(
            call_id,
            LlmCallOutcome(status="completed", prompt_tokens=10, cost_usd=0.5),
            NewMessage(role="assistant", origin="llm", content="hello"),
        )
        with pytest.raises(LlmCallAlreadyFinalized):
            await store.finish_llm_call(call_id, LlmCallOutcome(status="failed"))
        rows = await store.list_llm_calls(task_id)
        assert rows[0]["status"] == "completed" and rows[0]["prompt_tokens"] == 10
        # A rejected finalize adds nothing to the totals.
        assert (await store.get_task(task_id)).usage.cost_usd == 0.5

    in_store(database, scenario)


def test_finalize_updates_totals_and_saves_the_message_together(database):
    async def scenario(store):
        task_id, _ = await store.create_task(request(), CHAT_PROFILE)
        for attempt, estimated in ((1, False), (2, True)):
            call_id = await store.start_llm_call(start(task_id, task_id, attempt))
            await store.finish_llm_call(
                call_id,
                LlmCallOutcome(
                    status="completed",
                    prompt_tokens=100,
                    completion_tokens=20,
                    reasoning_tokens=5,
                    cost_usd=0.25,
                    cost_estimated=estimated,
                ),
                NewMessage(role="assistant", origin="llm", content=f"a{attempt}"),
            )
        usage = (await store.get_task(task_id)).usage
        assert (usage.input_tokens, usage.output_tokens, usage.reasoning_tokens) == (200, 40, 10)
        assert usage.cost_usd == 0.5 and usage.unknown_cost_calls == 1
        assert await store.task_cost(task_id) == 0.5
        assert await store.thread_cost(task_id) == 0.5
        assert await store.global_cost() == 0.5
        assert await store.last_prompt_tokens(task_id) == 100
        assert [m.content for m in await store.load_messages(task_id)][-2:] == ["a1", "a2"]

    in_store(database, scenario)


def test_orphans_use_the_heartbeat(database):
    async def scenario(store):
        queued, _ = await store.create_task(request("q"), CHAT_PROFILE)
        running, _ = await store.create_task(request("r"), CHAT_PROFILE)
        await store.claim_task(running, "o")
        finished = await ended(store, "d")
        assert await store.interrupt_orphans(60) == 0
        await asyncio.sleep(0.02)
        assert await store.interrupt_orphans(0) == 2
        assert (await store.get_task(queued)).status == "interrupted"
        assert (await store.get_task(running)).status == "interrupted"
        assert (await store.get_task(finished)).status == "completed"
        # A task that kept running after being interrupted cannot overwrite the outcome.
        assert not await store.finish_task(running, "completed")

    in_store(database, scenario)


def test_request_cancel_is_visible_and_ignored_once_ended(database):
    async def scenario(store):
        task_id, _ = await store.create_task(request(), CHAT_PROFILE)
        await store.request_cancel(task_id)
        assert await store.control(task_id) == ("queued", True)
        finished = await ended(store, "r2")
        await store.request_cancel(finished)
        assert await store.control(finished) == ("completed", False)
        with pytest.raises(TaskNotFound):
            await store.request_cancel("missing")

    in_store(database, scenario)


def test_concurrent_writers_succeed_under_wal(database):
    async def scenario(store):
        async def worker(i):
            task_id, _ = await store.create_task(request(f"w{i}"), CHAT_PROFILE)
            await store.claim_task(task_id, "o")
            for j in range(5):
                call_id = await store.start_llm_call(start(task_id, task_id, j))
                await store.finish_llm_call(
                    call_id,
                    LlmCallOutcome(status="completed", cost_usd=0.01),
                    NewMessage(role="assistant", origin="llm", content=f"{i}-{j}"),
                )
                await store.touch(task_id)
            await store.finish_task(task_id, "completed")
            return task_id

        task_ids = await asyncio.gather(*(worker(i) for i in range(30)))
        assert len(set(task_ids)) == 30
        for task_id in task_ids:
            assert len(await store.load_messages(task_id)) == 6
        assert await store.global_cost() == pytest.approx(30 * 5 * 0.01)

    in_store(database, scenario)
