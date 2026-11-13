"""Tool policy, trusted context, and chat and sandbox separation."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from harness.dev.gateway import (
    PLATFORM_TOOLS,
    SANDBOX_TOOLS,
    FakeGatewayFactory,
    ScriptedLlm,
    text_turn,
)
from harness.profiles import REBUILD_PROFILE, SANDBOX_TOOL_NAMES
from harness_helpers import Harness, make_request


def play(database, scenario, llm, gateways=None):
    async def main():
        async with database as sessions:
            return await scenario(Harness(sessions, llm, gateways=gateways))

    return asyncio.run(main())


def tool_names(llm_request):
    return [t["function"]["name"] for t in llm_request.tools]


def test_chat_receives_all_gateway_tools_and_rebuild_only_its_allowlist(database):
    llm = ScriptedLlm(lambda _r: text_turn("ok"))

    async def scenario(h):
        await h.start(make_request(kind="chat"))
        await h.start(make_request(kind="rebuild"))
        await h.start(make_request(kind="document"))

    play(database, scenario, llm)
    chat, rebuild, document = map(tool_names, llm.requests)
    assert chat == [t.name for t in PLATFORM_TOOLS]
    assert set(rebuild) == set(REBUILD_PROFILE.tool_policy)
    assert set(document) == {*SANDBOX_TOOL_NAMES, "export_artifact"}
    assert "delete_model" not in rebuild


def test_rebuild_allowlist_is_intersected_with_what_the_user_can_do(database):
    class Restricted(FakeGatewayFactory):
        @asynccontextmanager
        async def open(self, task):
            async with super().open(task) as gateway:
                real = gateway.list_tools

                async def narrower():
                    return [t for t in await real() if t.name != "run_parity"]

                gateway.list_tools = narrower
                yield gateway

    llm = ScriptedLlm(lambda _r: text_turn("ok"))

    async def scenario(h):
        await h.start(make_request(kind="rebuild"))

    play(database, scenario, llm, Restricted())
    names = tool_names(llm.requests[0])
    assert "run_parity" not in names and "export_artifact" in names


def test_chat_task_has_no_sandbox_tools_or_workspace(database):
    llm = ScriptedLlm(lambda _r: text_turn("ok"))
    gateways = FakeGatewayFactory()

    async def scenario(h):
        task_id, _ = await h.start(make_request(kind="chat"))
        return await h.store.get_task(task_id)

    snapshot = play(database, scenario, llm, gateways)
    assert snapshot.workspace_id is None
    assert gateways.opened[0].workspace_id is None
    assert not [n for n in tool_names(llm.requests[0]) if n.startswith("sandbox_")]


def test_chat_drops_sandbox_tools_even_if_a_gateway_offers_them(database):
    class Careless(FakeGatewayFactory):
        @asynccontextmanager
        async def open(self, task):
            async with super().open(task) as gateway:
                async def everything():
                    return [*PLATFORM_TOOLS, *SANDBOX_TOOLS]

                gateway.list_tools = everything
                yield gateway

    llm = ScriptedLlm(lambda _r: text_turn("ok"))

    async def scenario(h):
        await h.start(make_request(kind="chat"))

    play(database, scenario, llm, Careless())
    assert not [n for n in tool_names(llm.requests[0]) if n.startswith("sandbox_")]


def test_trusted_context_reaches_the_gateway_and_never_the_llm(database):
    llm = ScriptedLlm(lambda _r: text_turn("ok"))
    gateways = FakeGatewayFactory()
    request = make_request(
        "go",
        kind="rebuild",
        actor="actor-TRUSTED",
        model_package_id="package-TRUSTED",
        development_data_id="data-TRUSTED",
        run_id="run-TRUSTED",
        report_id="report-TRUSTED",
        conversation_id="conversation-TRUSTED",
        model_id="model-TRUSTED",
    )

    async def scenario(h):
        task_id, _ = await h.start(request)
        return task_id

    task_id = play(database, scenario, llm, gateways)
    info = gateways.opened[0]
    assert info.task_id == task_id
    assert info.actor_id == "actor-TRUSTED"
    assert info.actor_role == "developer"
    assert info.policy_version == "policy-1"
    assert info.model_package_id == "package-TRUSTED"
    assert info.development_data_id == "data-TRUSTED"
    sent = json.dumps([r.model_dump() for r in llm.requests])
    assert "TRUSTED" not in sent
    assert "policy-1" not in sent
    assert task_id not in sent


def test_rebuild_and_document_share_a_workspace_only_within_a_thread(database):
    llm = ScriptedLlm(lambda _r: text_turn("ok"))
    gateways = FakeGatewayFactory()

    async def scenario(h):
        first, _ = await h.start(make_request(kind="rebuild"))
        await h.start(make_request("more", kind="rebuild"), continue_from=first)
        await h.start(make_request(kind="rebuild"))

    play(database, scenario, llm, gateways)
    workspaces = [info.workspace_id for info in gateways.opened]
    assert workspaces[0] == workspaces[1]
    assert workspaces[2] != workspaces[0]
    assert all(workspaces)
