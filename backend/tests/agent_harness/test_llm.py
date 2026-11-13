"""OpenRouter client, tested with httpx.MockTransport. Nothing reaches the network."""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from harness.config import HarnessSettings
from harness.llm import LimitedLlm, OpenRouterClient
from harness.schemas import LlmCompletion, LlmError, LlmRequest, TextDelta

KEY = "sk-or-test-secret-key-value"
REASONING = "SECRET_REASONING_TEXT"


def llm_request(**overrides):
    values = dict(
        model="qwen/qwen3.8-27b",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_tokens=100,
        reasoning_effort="low",
        provider_routing={"data_collection": "deny", "require_parameters": True},
        idle_timeout_s=5,
        turn_timeout_s=10,
    )
    return LlmRequest(**{**values, **overrides})


def sse(*chunks, done=True, comments=()):
    lines = [f": {c}\n\n" for c in comments]
    lines += [f"data: {json.dumps(c)}\n\n" for c in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def delta(content=None, finish=None, **extra):
    d = {k: v for k, v in {"content": content, **extra}.items() if v is not None}
    return {"id": "gen-1", "model": "qwen/x", "provider": "Prov", "choices": [{"delta": d, "finish_reason": finish}]}


USAGE = {
    "id": "gen-1",
    "choices": [],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 7,
        "cost": 0.0012,
        "prompt_tokens_details": {"cached_tokens": 4},
        "completion_tokens_details": {"reasoning_tokens": 3},
    },
}


def run_stream(handler, request=None, **client_kwargs):
    """Returns (items, error, request bodies)."""
    bodies = []

    def wrapped(http_request):
        bodies.append(json.loads(http_request.content))
        return handler(http_request)

    async def main():
        client = OpenRouterClient(
            KEY, HarnessSettings(), transport=httpx.MockTransport(wrapped), **client_kwargs
        )
        items, error = [], None
        try:
            async for item in client.stream(request or llm_request()):
                items.append(item)
        except LlmError as exc:
            error = exc
        finally:
            await client.aclose()
        return items, error

    items, error = asyncio.run(main())
    return items, error, bodies


def respond(content, status=200):
    return lambda _request: httpx.Response(status, content=content)


def completion(items) -> LlmCompletion:
    assert isinstance(items[-1], LlmCompletion)
    return items[-1]


def test_text_stream_with_usage_and_metadata():
    items, error, _ = run_stream(respond(sse(delta("Hel"), delta("lo", "stop"), USAGE)))
    assert error is None
    assert [i.text for i in items[:-1]] == ["Hel", "lo"]
    c = completion(items)
    assert (c.text, c.finish_reason, c.provider, c.model, c.generation_id) == (
        "Hello",
        "stop",
        "Prov",
        "qwen/x",
        "gen-1",
    )
    u = c.usage
    assert (u.prompt_tokens, u.completion_tokens, u.reasoning_tokens, u.cached_tokens) == (10, 7, 3, 4)
    assert u.cost_usd == 0.0012


def test_tool_fragments_are_assembled_by_index_including_parallel_calls():
    fragments = [
        delta(tool_calls=[{"index": 1, "id": "b", "function": {"name": "second", "arguments": "{\"y\""}}]),
        delta(tool_calls=[{"index": 0, "id": "a", "function": {"name": "first", "arguments": ""}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": "{\"x\":1}"}}]),
        delta(tool_calls=[{"index": 1, "function": {"arguments": ":2}"}}], finish="tool_calls"),
    ]
    items, error, _ = run_stream(respond(sse(*fragments)))
    assert error is None
    c = completion(items)
    assert [(t.id, t.name, t.arguments) for t in c.tool_calls] == [
        ("a", "first", '{"x":1}'),
        ("b", "second", '{"y":2}'),
    ]


def test_invalid_tool_arguments_stay_raw_for_the_loop_to_reject():
    fragment = delta(
        tool_calls=[{"index": 0, "id": "a", "function": {"name": "t", "arguments": "{bad"}}],
        finish="tool_calls",
    )
    items, _, _ = run_stream(respond(sse(fragment)))
    assert completion(items).tool_calls[0].arguments == "{bad"


def test_comment_lines_are_ignored_and_done_stops_the_stream():
    body = sse(delta("ok", "stop"), comments=["OPENROUTER PROCESSING"])
    body += b'data: {"choices":[{"delta":{"content":"after done"}}]}\n\n'
    items, error, _ = run_stream(respond(body))
    assert error is None
    assert completion(items).text == "ok"


def test_error_field_raises_even_with_http_200():
    items, error, _ = run_stream(
        respond(sse(delta("par"), {"error": {"code": 502, "message": "upstream died"}}))
    )
    assert [i.text for i in items] == ["par"]
    assert error.http_status == 200
    assert error.retryable is True and error.no_charge is False
    assert "upstream died" in str(error)


def test_error_field_with_client_code_is_not_retryable():
    _, error, _ = run_stream(respond(sse({"error": {"code": 400, "message": "bad"}})))
    assert error.retryable is False and error.code == "request_rejected"


def test_reasoning_fields_never_leave_the_client(caplog):
    caplog.set_level(logging.DEBUG)
    chunk = delta(
        "visible",
        "stop",
        reasoning=REASONING,
        reasoning_details=[{"type": "reasoning.text", "text": REASONING}],
    )
    chunk["choices"][0]["message"] = {"reasoning": REASONING}
    items, error, _ = run_stream(respond(sse(chunk, USAGE)))
    assert error is None
    assert REASONING not in repr(items)
    assert "reasoning_details" not in repr(items)
    assert REASONING not in caplog.text
    assert completion(items).usage.reasoning_tokens == 3


@pytest.mark.parametrize("effort", ["low", "medium"])
def test_request_always_has_routing_settings_and_exclude(effort):
    _, _, bodies = run_stream(
        respond(sse(delta("x", "stop"))), request=llm_request(reasoning_effort=effort)
    )
    body = bodies[0]
    assert body["reasoning"] == {"effort": effort, "exclude": True}
    assert body["provider"] == {"data_collection": "deny", "require_parameters": True}
    assert body["stream"] is True
    assert body["model"] == "qwen/qwen3.8-27b"
    assert body["max_tokens"] == 100
    assert "tools" not in body


def test_tools_are_sent_when_present():
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    _, _, bodies = run_stream(respond(sse(delta("x", "stop"))), request=llm_request(tools=tools))
    assert bodies[0]["tools"] == tools


@pytest.mark.parametrize(
    "status, retryable, code, no_charge",
    [
        (429, True, "http_error", True),
        (500, True, "http_error", True),
        (503, True, "http_error", True),
        (400, False, "request_rejected", True),
        (401, False, "request_rejected", True),
        (402, False, "request_rejected", True),
        (404, False, "request_rejected", True),
    ],
)
def test_http_errors_map_to_the_right_llm_error(status, retryable, code, no_charge):
    body = json.dumps({"error": {"message": "nope"}}).encode()
    _, error, _ = run_stream(respond(body, status=status))
    assert (error.http_status, error.retryable, error.code, error.no_charge) == (
        status,
        retryable,
        code,
        no_charge,
    )
    assert "nope" in str(error)


def test_error_body_that_is_not_json_is_tolerated():
    _, error, _ = run_stream(respond(b"<html>bad gateway</html>", status=502))
    assert error.http_status == 502 and error.retryable


def test_network_error_is_retryable():
    def handler(_request):
        raise httpx.ConnectError("boom")

    _, error, _ = run_stream(handler)
    assert error.code == "network_error" and error.retryable and not error.no_charge


def test_malformed_chunk_is_a_retryable_error():
    _, error, _ = run_stream(respond(b"data: {not json}\n\n"))
    assert error.code == "malformed_stream" and error.retryable


def test_stream_that_ends_without_a_finish_reason_is_an_error():
    items, error, _ = run_stream(respond(sse(delta("partial"), done=False)))
    assert [i.text for i in items] == ["partial"]
    assert error.code == "stream_ended_early" and error.retryable


def test_missing_usage_is_reported_as_none():
    items, _, _ = run_stream(respond(sse(delta("x", "stop"))))
    assert completion(items).usage is None


def test_usage_without_cost_leaves_cost_unset():
    usage = {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
    items, _, _ = run_stream(respond(sse(delta("x", "stop"), usage)))
    u = completion(items).usage
    assert u.prompt_tokens == 1 and u.cost_usd is None


@pytest.mark.parametrize(
    "parts, expected",
    [
        (["\n\n", "\n  \n", "Hello"], "Hello"),
        (["  ", "\n", "  indented"], "  indented"),
        (["\r\n", "Hi\n\nthere"], "Hi\n\nthere"),
        (["Hi", "\n\n", "there"], "Hi\n\nthere"),
    ],
)
def test_leading_blank_lines_are_removed(parts, expected):
    chunks = [delta(p) for p in parts[:-1]] + [delta(parts[-1], "stop")]
    items, _, _ = run_stream(respond(sse(*chunks)))
    c = completion(items)
    assert c.text == expected
    assert "".join(i.text for i in items[:-1]) == expected


def test_only_blank_text_gives_empty_text_and_no_token():
    items, _, _ = run_stream(respond(sse(delta("\n\n"), delta(" ", "stop"))))
    assert len(items) == 1 and completion(items).text == ""


class SlowStream(httpx.AsyncByteStream):
    def __init__(self, parts):
        self._parts = parts

    async def __aiter__(self):
        for part in self._parts:
            if isinstance(part, float):
                await asyncio.sleep(part)
            else:
                yield part


def slow(*parts):
    return lambda _request: httpx.Response(200, stream=SlowStream(parts))


def test_idle_timeout():
    items, error, _ = run_stream(
        slow(sse(delta("a"), done=False), 5.0),
        request=llm_request(idle_timeout_s=0.05, turn_timeout_s=5),
    )
    assert [i.text for i in items] == ["a"]
    assert error.code == "idle_timeout" and error.retryable


def test_idle_timeout_before_the_first_chunk_ignores_keepalive_comments():
    comments = []
    for _ in range(20):
        comments += [b": OPENROUTER PROCESSING\n\n", 0.02]
    _, error, _ = run_stream(
        slow(*comments), request=llm_request(idle_timeout_s=0.1, turn_timeout_s=5)
    )
    assert error.code == "idle_timeout"


def test_turn_timeout_while_chunks_keep_arriving():
    parts = []
    for _ in range(50):
        parts += [sse(delta("a"), done=False), 0.02]
    _, error, _ = run_stream(
        slow(*parts), request=llm_request(idle_timeout_s=1, turn_timeout_s=0.1)
    )
    assert error.code == "turn_timeout" and error.retryable


def test_api_key_never_appears_in_repr_error_or_log(caplog):
    caplog.set_level(logging.DEBUG)
    leaked = json.dumps({"error": {"message": f"bad key {KEY}"}}).encode()

    async def main():
        client = OpenRouterClient(
            KEY, HarnessSettings(), transport=httpx.MockTransport(respond(leaked, status=401))
        )
        try:
            assert KEY not in repr(client)
            assert KEY not in str(vars(client).get("_api_key"))
            with pytest.raises(LlmError) as info:
                async for _ in client.stream(llm_request()):
                    pass
            return info.value
        finally:
            await client.aclose()

    error = asyncio.run(main())
    assert KEY not in str(error) and KEY not in repr(error)
    assert "[redacted]" in str(error)
    assert KEY not in caplog.text


def test_key_is_sent_as_a_bearer_header():
    seen = []

    def handler(http_request):
        seen.append(http_request.headers["authorization"])
        return httpx.Response(200, content=sse(delta("x", "stop")))

    run_stream(handler)
    assert seen == [f"Bearer {KEY}"]


def test_pool_limit_is_above_the_llm_limit():
    async def main():
        settings = HarnessSettings()
        client = OpenRouterClient(KEY, settings, transport=httpx.MockTransport(respond(b"")))
        try:
            assert client.pool_limits.max_connections > settings.llm_concurrency
        finally:
            await client.aclose()

    asyncio.run(main())


def test_limited_llm_holds_the_limit_and_releases_on_early_close():
    active = 0
    peak = 0

    class Inner:
        async def stream(self, _request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                yield TextDelta(text="a")
                yield LlmCompletion(text="a", finish_reason="stop")
            finally:
                active -= 1

    async def consume(llm):
        async for _ in llm.stream(llm_request()):
            pass

    async def main():
        llm = LimitedLlm(Inner(), 3)
        await asyncio.gather(*(consume(llm) for _ in range(12)))
        # Abandoning a stream must free its slot.
        abandoned = llm.stream(llm_request())
        await abandoned.__anext__()
        await abandoned.aclose()
        await asyncio.wait_for(consume(LimitedLlm(Inner(), 1)), 1)
        for _ in range(3):
            await asyncio.wait_for(consume(llm), 1)

    asyncio.run(main())
    assert peak == 3
    assert active == 0
