"""With reasoning in the stream, the text appears in no event, result, table or log."""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
from sqlalchemy import text as sql

from harness.config import HarnessSettings
from harness.dev.gateway import FakeGatewayFactory
from harness.llm import OpenRouterClient
from harness.loop import run_task
from harness.store import SqlTaskStore
from harness_helpers import make_request

REASONING = "SECRET_REASONING_TEXT_12345"


def _chunk(delta, finish=None, usage=None):
    delta = {
        **delta,
        "reasoning": REASONING,
        "reasoning_details": [{"type": "reasoning.text", "text": REASONING}],
    }
    chunk = {"id": "gen-1", "model": "m", "provider": "p", "choices": [{"delta": delta, "finish_reason": finish}]}
    if usage:
        chunk["usage"] = usage
    return f"data: {json.dumps(chunk)}\n\n"


USAGE = {
    "prompt_tokens": 10,
    "completion_tokens": 5,
    "cost": 0.001,
    "completion_tokens_details": {"reasoning_tokens": 7},
}


def _handler(http_request):
    body = json.loads(http_request.content)
    assert REASONING not in http_request.content.decode()  # never sent back either
    has_result = any(m["role"] == "tool" for m in body["messages"])
    if not has_result:
        tool_call = {"index": 0, "id": "c1", "function": {"name": "get_model_status", "arguments": "{}"}}
        lines = _chunk({"tool_calls": [tool_call]}, "tool_calls", USAGE)
    else:
        lines = _chunk({"content": "visible answer"}, "stop", USAGE)
    return httpx.Response(200, content=(lines + "data: [DONE]\n\n").encode())


def test_reasoning_appears_nowhere(database, caplog):
    caplog.set_level(logging.DEBUG)
    events = []

    async def main():
        async with database as sessions:
            store = SqlTaskStore(sessions)
            settings = HarnessSettings()
            client = OpenRouterClient("key", settings, transport=httpx.MockTransport(_handler))
            try:
                request = make_request("question")
                task_id, _ = await store.create_task(request, settings.profiles["chat"])
                result = await run_task(
                    task_id,
                    store=store,
                    llm=client,
                    gateways=FakeGatewayFactory(),
                    emit=events.append,
                    cancel=asyncio.Event(),
                    settings=settings,
                )
            finally:
                await client.aclose()
            dumps = []
            async with database.engine.connect() as connection:
                for table in ("agent_tasks", "agent_messages", "llm_calls"):
                    rows = await connection.execute(sql(f"SELECT * FROM {table}"))
                    dumps.append(repr(rows.fetchall()))
            return result, dumps

    result, dumps = asyncio.run(main())
    assert result.status == "completed" and result.text == "visible answer"
    assert REASONING not in result.model_dump_json()
    assert REASONING not in repr(events)
    for dump in dumps:
        assert REASONING not in dump
    assert REASONING not in caplog.text
    # Only the counts are kept, and two calls of 7 reasoning tokens were made.
    assert result.usage.reasoning_tokens == 14
    assert any(e.type == "llm_turn" and e.data["reasoning_tokens"] == 7 for e in events)
