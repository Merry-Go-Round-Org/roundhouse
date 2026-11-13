"""Task manager: concurrency, queueing, events, orphan recovery and shutdown."""
from __future__ import annotations

import asyncio

import pytest

from harness.dev.gateway import FakeGatewayFactory, ScriptedLlm, text_turn, tool_turn
from harness.loop import run_task
from harness.manager import TaskManager, _Subscriber
from harness.schemas import AgentEvent, HarnessBusy, Resolution, TaskNotFound, ThreadBusy
from harness.store import SqlTaskStore
from harness_helpers import fast_settings, make_request


def play(database, scenario, **settings):
    async def main():
        async with database as sessions:
            return await scenario(SqlTaskStore(sessions), fast_settings(**settings))

    return asyncio.run(main())


class Gate:
    """A scripted LLM that holds every call until released, tracking how many are in flight."""

    def __init__(self):
        self.release = asyncio.Event()
        self.active = 0
        self.peak = 0
        self.entered = 0
        self.llm = ScriptedLlm(lambda _r: [self._hold, *text_turn("done")])

    async def _hold(self):
        self.active += 1
        self.entered += 1
        self.peak = max(self.peak, self.active)
        try:
            await self.release.wait()
        finally:
            self.active -= 1


async def until(condition, timeout=5):
    async def poll():
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


def test_spawn_returns_before_the_task_finishes(database):
    async def scenario(store, settings):
        gate = Gate()
        manager = TaskManager(store, gate.llm, FakeGatewayFactory(), settings)
        task_id = await manager.spawn(make_request())
        assert (await manager.get(task_id)).status in ("queued", "running")
        await until(lambda: gate.entered == 1)
        assert (await manager.get(task_id)).status == "running"
        gate.release.set()
        result = await manager.wait(task_id)
        assert result.status == "completed" and result.text == "done"

    play(database, scenario)


def test_50_concurrent_tasks_keep_their_contexts_separate(database):
    async def scenario(store, settings):
        def reply(request):
            prompts = [m["content"] for m in request.messages if m["role"] == "user"]
            assert len(prompts) == 1  # no message from another task
            return text_turn(f"echo {prompts[0]}")

        llm = ScriptedLlm(reply)
        manager = TaskManager(store, llm, FakeGatewayFactory(), settings)
        task_ids = await asyncio.gather(
            *(manager.spawn(make_request(f"prompt-{i}", actor=f"user-{i}")) for i in range(50))
        )
        results = await asyncio.gather(*(manager.wait(t) for t in task_ids))
        assert len(set(task_ids)) == 50
        for i, (task_id, result) in enumerate(zip(task_ids, results)):
            assert result.status == "completed" and result.text == f"echo prompt-{i}"
            snapshot = await manager.get(task_id)
            assert snapshot.actor_id == f"user-{i}" and snapshot.thread_id == task_id
            messages = await store.load_messages(task_id)
            assert [m.content for m in messages] == [f"prompt-{i}", f"echo prompt-{i}"]
        assert await store.global_cost() == pytest.approx(50 * 0.0001)

    play(database, scenario)


def test_chat_concurrency_limit_holds(database):
    async def scenario(store, settings):
        gate = Gate()
        manager = TaskManager(store, gate.llm, FakeGatewayFactory(), settings)
        task_ids = [await manager.spawn(make_request()) for _ in range(6)]
        await until(lambda: gate.entered == 2)
        await asyncio.sleep(0.05)
        assert gate.peak == 2
        statuses = [(await manager.get(t)).status for t in task_ids]
        assert statuses.count("running") == 2 and statuses.count("queued") == 4
        gate.release.set()
        results = await asyncio.gather(*(manager.wait(t) for t in task_ids))
        assert all(r.status == "completed" for r in results)
        assert gate.peak == 2

    play(database, scenario, chat_concurrency=2)


def test_llm_limit_holds_across_pools(database):
    async def scenario(store, settings):
        gate = Gate()
        manager = TaskManager(store, gate.llm, FakeGatewayFactory(), settings)
        task_ids = [await manager.spawn(make_request()) for _ in range(5)]
        task_ids += [await manager.spawn(make_request(kind="rebuild")) for _ in range(2)]
        await until(lambda: gate.entered == 3)
        await asyncio.sleep(0.05)
        assert gate.peak == 3  # tasks are running, but LLM calls wait
        statuses = [(await manager.get(t)).status for t in task_ids]
        assert statuses.count("running") == 7
        gate.release.set()
        await asyncio.gather(*(manager.wait(t) for t in task_ids))
        assert gate.peak == 3

    play(database, scenario, llm_concurrency=3)


def test_background_kinds_share_their_slots(database):
    async def scenario(store, settings):
        gate = Gate()
        manager = TaskManager(store, gate.llm, FakeGatewayFactory(), settings)
        kinds = ["rebuild", "document", "intake", "validation"]
        task_ids = [await manager.spawn(make_request(kind=k)) for k in kinds]
        await until(lambda: gate.entered == 2)
        await asyncio.sleep(0.05)
        assert gate.peak == 2
        gate.release.set()
        await asyncio.gather(*(manager.wait(t) for t in task_ids))

    play(database, scenario, background_concurrency=2)


def test_rebuild_backlog_does_not_block_chat(database):
    async def scenario(store, settings):
        gate = Gate()

        def reply(request):
            # Rebuild prompts stall, chat prompts answer at once.
            if request.reasoning_effort == "medium":
                return [gate._hold, *text_turn("rebuilt")]
            return text_turn("chat answer")

        manager = TaskManager(store, ScriptedLlm(reply), FakeGatewayFactory(), settings)
        rebuilds = [await manager.spawn(make_request(kind="rebuild")) for _ in range(5)]
        await until(lambda: gate.entered == 2)
        chats = [await manager.spawn(make_request()) for _ in range(5)]
        results = await asyncio.wait_for(asyncio.gather(*(manager.wait(t) for t in chats)), 5)
        assert all(r.text == "chat answer" for r in results)
        statuses = [(await manager.get(t)).status for t in rebuilds]
        assert statuses.count("queued") == 3 and statuses.count("running") == 2
        gate.release.set()
        await asyncio.gather(*(manager.wait(t) for t in rebuilds))

    play(database, scenario, background_concurrency=2)


def test_queue_overflow_raises_harness_busy_but_a_full_queue_does_not_block_a_free_pool(database):
    async def scenario(store, settings):
        gate = Gate()
        manager = TaskManager(store, gate.llm, FakeGatewayFactory(), settings)
        rebuilds = [await manager.spawn(make_request(kind="rebuild")) for _ in range(4)]
        # Two running, two queued. The queue limit is reached.
        with pytest.raises(HarnessBusy):
            await manager.spawn(make_request(kind="document"))
        chat = await manager.spawn(make_request())  # the chat pool still has a free slot
        gate.release.set()
        await asyncio.gather(*(manager.wait(t) for t in [*rebuilds, chat]))
        # There is room again once the backlog has cleared.
        last = await manager.spawn(make_request(kind="document"))
        assert (await manager.wait(last)).status == "completed"

    play(database, scenario, max_queued=2, background_concurrency=2)


def test_burst_within_capacity_is_not_refused(database):
    async def scenario(store, settings):
        llm = ScriptedLlm(lambda _r: text_turn("ok"))
        manager = TaskManager(store, llm, FakeGatewayFactory(), settings)
        # 16 run and 100 queue, with no chance to start between spawns.
        task_ids = await asyncio.gather(*(manager.spawn(make_request()) for _ in range(116)))
        await asyncio.gather(*(manager.wait(t) for t in task_ids))

    play(database, scenario)


def test_repeated_request_id_returns_the_same_task_and_runs_once(database):
    async def scenario(store, settings):
        llm = ScriptedLlm(lambda _r: text_turn("ok"))
        manager = TaskManager(store, llm, FakeGatewayFactory(), settings)
        request = make_request()
        first = await manager.spawn(request)
        again = await manager.spawn(request)
        assert first == again
        await manager.wait(first)
        assert await manager.spawn(request) == first
        assert len(llm.requests) == 1

    play(database, scenario)


def test_one_active_task_per_thread_raises_thread_busy(database):
    async def scenario(store, settings):
        llm = ScriptedLlm(lambda _r: text_turn("ok"))
        manager = TaskManager(store, llm, FakeGatewayFactory(), settings)
        parent = await manager.spawn(make_request())
        with pytest.raises(ThreadBusy):
            await manager.spawn(make_request("again"), continue_from=parent)
        await manager.wait(parent)
        child = await manager.spawn(make_request("again"), continue_from=parent)
        with pytest.raises(ThreadBusy):
            await manager.spawn(make_request("third"), continue_from=parent)
        await manager.wait(child)

    play(database, scenario)


def test_continuation_through_the_manager_after_a_review(database):
    async def scenario(store, settings):
        llm = ScriptedLlm(
            [tool_turn(("submit_review_decision", {"decision": "approve"})), text_turn("Recorded.")]
        )
        manager = TaskManager(store, llm, FakeGatewayFactory(), settings)
        parent = await manager.spawn(make_request("approve it"))
        paused = await manager.wait(parent)
        assert paused.status == "awaiting_review"
        resolution = Resolution(pending_id=paused.pending_id, outcome="approved")
        child = await manager.spawn(make_request(None, resolution=resolution), continue_from=parent)
        assert (await manager.wait(child)).status == "completed"

    play(database, scenario)


# ---------------------------------------------------------------- cancellation
def test_cancel_running_task(database):
    async def scenario(store, settings):
        gate = Gate()
        manager = TaskManager(store, gate.llm, FakeGatewayFactory(), settings)
        task_id = await manager.spawn(make_request())
        await until(lambda: gate.entered == 1)
        await manager.cancel(task_id)
        result = await asyncio.wait_for(manager.wait(task_id), 5)
        assert result.status == "cancelled"
        assert (await manager.get(task_id)).cancel_requested

    play(database, scenario)


def test_cancel_queued_task_frees_its_place(database):
    async def scenario(store, settings):
        gate = Gate()
        manager = TaskManager(store, gate.llm, FakeGatewayFactory(), settings)
        running = await manager.spawn(make_request())
        await until(lambda: gate.entered == 1)
        queued = await manager.spawn(make_request())
        events = manager.subscribe(queued)
        await manager.cancel(queued)
        result = await asyncio.wait_for(manager.wait(queued), 5)
        assert result.status == "cancelled"
        assert [e.type async for e in events] == ["done"]
        gate.release.set()
        assert (await manager.wait(running)).status == "completed"
        # The slot was not leaked.
        again = await manager.spawn(make_request())
        assert (await asyncio.wait_for(manager.wait(again), 5)).status == "completed"

    play(database, scenario, chat_concurrency=1)


def test_cancel_unknown_task_raises(database):
    async def scenario(store, settings):
        manager = TaskManager(store, ScriptedLlm([]), FakeGatewayFactory(), settings)
        with pytest.raises(TaskNotFound):
            await manager.cancel("missing")
        with pytest.raises(TaskNotFound):
            await manager.get("missing")

    play(database, scenario)


def test_caller_that_stops_waiting_does_not_cancel_the_task(database):
    async def scenario(store, settings):
        gate = Gate()
        manager = TaskManager(store, gate.llm, FakeGatewayFactory(), settings)
        task_id = await manager.spawn(make_request())
        await until(lambda: gate.entered == 1)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(manager.wait(task_id), 0.02)
        assert (await manager.get(task_id)).status == "running"
        gate.release.set()
        assert (await manager.wait(task_id)).status == "completed"

    play(database, scenario)


# ---------------------------------------------------------------- events
def test_events_arrive_in_order_and_end_with_done(database):
    async def scenario(store, settings):
        llm = ScriptedLlm([tool_turn(("get_model_status", {})), text_turn("all done")])
        manager = TaskManager(store, llm, FakeGatewayFactory(), settings)
        task_id = await manager.spawn(make_request())
        events = [e async for e in manager.subscribe(task_id)]
        types = [e.type for e in events]
        assert types[0] == "task_started" and types[-1] == "done"
        assert types.index("tool_call") < types.index("tool_result") < types.index("token")
        assert [e.seq for e in events] == sorted(e.seq for e in events)
        assert events[-1].data == {"status": "completed"}

    play(database, scenario)


def test_two_subscribers_both_receive_events(database):
    async def scenario(store, settings):
        manager = TaskManager(
            store, ScriptedLlm([text_turn("one two")]), FakeGatewayFactory(), settings
        )
        task_id = await manager.spawn(make_request())
        a, b = manager.subscribe(task_id), manager.subscribe(task_id)
        first = [e.type async for e in a]
        second = [e.type async for e in b]
        assert first == second and first[-1] == "done"

    play(database, scenario)


def test_subscribing_after_the_task_ended_still_gets_done(database):
    async def scenario(store, settings):
        manager = TaskManager(store, ScriptedLlm([text_turn("ok")]), FakeGatewayFactory(), settings)
        task_id = await manager.spawn(make_request())
        await manager.wait(task_id)
        await asyncio.sleep(0)
        events = [e async for e in manager.subscribe(task_id)]
        assert [e.type for e in events] == ["done"]
        assert events[0].data == {"status": "completed"}

    play(database, scenario)


def test_following_a_task_from_another_process(database):
    async def scenario(store, settings):
        other = TaskManager(store, ScriptedLlm([]), FakeGatewayFactory(), settings)
        task_id, _ = await store.create_task(make_request(), settings.profiles["chat"])
        events = other.subscribe(task_id)
        waiting = asyncio.create_task(other.wait(task_id))
        reader = asyncio.create_task(events.__anext__())
        await run_task(
            task_id,
            store=store,
            llm=ScriptedLlm([text_turn("elsewhere")]),
            gateways=FakeGatewayFactory(),
            emit=lambda _e: None,
            cancel=asyncio.Event(),
            settings=settings,
        )
        assert (await asyncio.wait_for(waiting, 5)).text == "elsewhere"
        assert (await asyncio.wait_for(reader, 5)).data == {"status": "completed"}

    play(database, scenario, wait_poll_interval_s=0.01)


def test_slow_subscriber_does_not_block_the_loop_and_done_always_arrives(database):
    async def scenario(store, settings):
        words = " ".join(f"w{i}" for i in range(500))
        llm = ScriptedLlm([tool_turn(("get_model_status", {})), text_turn(words)])
        manager = TaskManager(store, llm, FakeGatewayFactory(), settings)
        task_id = await manager.spawn(make_request())
        events = manager.subscribe(task_id)  # nobody reads until the task has ended
        result = await asyncio.wait_for(manager.wait(task_id), 10)
        assert result.status == "completed"
        received = [e async for e in events]
        assert len(received) <= 10
        types = [e.type for e in received]
        assert types[-1] == "done"
        # Tokens were dropped first, so the tool events survive.
        assert "tool_call" in types and "tool_result" in types
        # A gap in seq is what tells a client to reload from the database.
        assert received[-1].seq > len(received)
        assert (await store.load_messages(task_id))[-1].content == words

    play(database, scenario, event_queue_size=10)


def _event(seq, event_type):
    return AgentEvent(task_id="t", seq=seq, type=event_type)


def test_subscriber_drops_a_new_token_rather_than_an_older_one():
    async def main():
        queue = _Subscriber(2)
        queue.push(_event(1, "token"))
        queue.push(_event(2, "token"))
        queue.push(_event(3, "token"))
        queue.push(_event(4, "done"))
        return [e.seq async for e in queue]

    assert asyncio.run(main()) == [2, 4]


def test_subscriber_drops_tokens_first_and_always_keeps_done():
    async def main():
        queue = _Subscriber(3)
        queue.push(_event(1, "token"))
        queue.push(_event(2, "tool_call"))
        queue.push(_event(3, "token"))
        queue.push(_event(4, "token"))  # full, and a token: dropped
        queue.push(_event(5, "tool_result"))  # evicts the oldest token
        queue.push(_event(6, "llm_turn"))  # evicts the other token
        queue.push(_event(7, "error"))  # nothing to evict: dropped
        queue.push(_event(8, "done"))  # always gets in
        return [e.seq async for e in queue]

    assert asyncio.run(main()) == [5, 6, 8]


# ---------------------------------------------------------------- recovery and shutdown
def test_orphan_recovery(database):
    async def scenario(store, settings):
        manager = TaskManager(store, ScriptedLlm([]), FakeGatewayFactory(), settings)
        profile = settings.profiles["chat"]
        queued, _ = await store.create_task(make_request(), profile)
        running, _ = await store.create_task(make_request(), profile)
        await store.claim_task(running, "dead")
        fresh_manager = TaskManager(
            store, ScriptedLlm([]), FakeGatewayFactory(), settings.model_copy(update={"orphan_timeout_s": 60})
        )
        assert await fresh_manager.recover_orphans() == 0
        await asyncio.sleep(0.05)
        assert await manager.recover_orphans() == 2
        assert (await manager.get(queued)).status == "interrupted"
        assert (await manager.get(running)).status == "interrupted"

    play(database, scenario, orphan_timeout_s=0.01)


def test_long_queue_wait_is_not_mistaken_for_an_orphan(database):
    async def scenario(store, settings):
        gate = Gate()
        manager = TaskManager(store, gate.llm, FakeGatewayFactory(), settings)
        running = await manager.spawn(make_request())
        queued = await manager.spawn(make_request())
        await until(lambda: gate.entered == 1)
        for _ in range(6):
            await asyncio.sleep(0.05)
            assert await store.interrupt_orphans(0.15) == 0
        assert (await manager.get(queued)).status == "queued"
        gate.release.set()
        assert (await manager.wait(running)).status == "completed"
        assert (await manager.wait(queued)).status == "completed"

    play(database, scenario, chat_concurrency=1, heartbeat_interval_s=0.02)


def test_long_run_task_without_the_manager_is_not_an_orphan(database):
    async def scenario(store, settings):
        task_id, _ = await store.create_task(make_request(), settings.profiles["chat"])
        manager = TaskManager(store, ScriptedLlm([]), FakeGatewayFactory(), settings)

        async def long_call():
            for _ in range(6):
                await asyncio.sleep(0.05)
                assert await manager.recover_orphans() == 0

        result = await run_task(
            task_id,
            store=store,
            llm=ScriptedLlm([[long_call, *text_turn("finished")]]),
            gateways=FakeGatewayFactory(),
            emit=lambda _e: None,
            cancel=asyncio.Event(),
            settings=settings,
        )
        assert result.status == "completed"

    play(database, scenario, heartbeat_interval_s=0.02, orphan_timeout_s=0.15)


def test_shutdown_cancels_running_tasks_and_waits_for_their_final_state(database):
    async def scenario(store, settings):
        gate = Gate()
        gateways = FakeGatewayFactory()
        manager = TaskManager(store, gate.llm, gateways, settings)
        running = await manager.spawn(make_request())
        queued = await manager.spawn(make_request())
        await until(lambda: gate.entered == 1)
        await asyncio.wait_for(manager.shutdown(), 5)
        assert (await store.get_task(running)).status == "cancelled"
        assert (await store.get_task(queued)).status == "cancelled"
        assert gateways.closed == [gateways.opened[0].task_id]
        calls = await store.list_llm_calls(running)
        assert [c["status"] for c in calls] == ["cancelled"]
        with pytest.raises(HarnessBusy):
            await manager.spawn(make_request())

    play(database, scenario, chat_concurrency=1)


def test_no_strong_reference_is_kept_after_a_task_ends(database):
    async def scenario(store, settings):
        manager = TaskManager(store, ScriptedLlm([text_turn("ok")]), FakeGatewayFactory(), settings)
        task_id = await manager.spawn(make_request())
        await manager.wait(task_id)
        await asyncio.sleep(0)
        assert manager._tasks == {}

    play(database, scenario)
