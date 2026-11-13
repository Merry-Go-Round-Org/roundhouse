"""OpenRouter streaming client.

Reasoning content is never read from the stream: only named fields are copied out of each
chunk, so `reasoning` and `reasoning_details` are dropped here and cannot reach anything else.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import aclosing
from typing import Any, AsyncIterator

import httpx
from pydantic import SecretStr

from harness.config import HarnessSettings
from harness.schemas import LlmClient, LlmCompletion, LlmError, LlmRequest, LlmUsage, TextDelta, ToolCall

_LEADING_BLANK_LINES = re.compile(r"^(?:[ \t]*\r?\n)+")
_MAX_ERROR_BODY_BYTES = 2_048
_MAX_ERROR_MESSAGE_CHARS = 300


def _is_retryable_status(status: int) -> bool:
    return status == 429 or status >= 500


class _ToolCallAssembler:
    """Joins tool call fragments by `index`. Arguments stay raw text until the stream ends."""

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}

    def add(self, fragment: dict[str, Any]) -> None:
        index = fragment.get("index", 0)
        call = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if fragment.get("id"):
            call["id"] = fragment["id"]
        function = fragment.get("function") or {}
        if function.get("name"):
            call["name"] = function["name"]
        if function.get("arguments"):
            call["arguments"] += function["arguments"]

    def finish(self) -> list[ToolCall]:
        return [
            ToolCall(
                id=call["id"] or f"call_{index}",
                name=call["name"],
                arguments=call["arguments"],
            )
            for index, call in sorted(self._calls.items())
        ]


class _LeadingBlankStripper:
    """Holds text back until it has a non-blank character, then removes leading blank lines."""

    def __init__(self) -> None:
        self._held = ""
        self._started = False

    def feed(self, text: str) -> str:
        if self._started:
            return text
        self._held += text
        if not self._held.strip():
            return ""
        self._started = True
        return _LEADING_BLANK_LINES.sub("", self._held)


class LimitedLlm:
    """Wraps a client with the per-process limit on LLM calls in flight."""

    def __init__(self, inner: LlmClient, limit: int) -> None:
        self._inner = inner
        self._slots = asyncio.Semaphore(limit)

    def share_slots(self, slots: asyncio.Semaphore) -> None:
        self._slots = slots

    async def stream(self, request: LlmRequest) -> AsyncIterator[TextDelta | LlmCompletion]:
        async with self._slots:
            stream = self._inner.stream(request)
            try:
                async for item in stream:
                    yield item
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()


class OpenRouterClient:
    """One instance per process, because it holds the shared connection pool."""

    def __init__(
        self,
        api_key: str,
        settings: HarnessSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = SecretStr(api_key)
        self._url = f"{settings.base_url.rstrip('/')}/chat/completions"
        # Above the LLM limit, so a call in flight never waits on the pool.
        connections = settings.llm_concurrency + 4
        self.pool_limits = httpx.Limits(
            max_connections=connections, max_keepalive_connections=connections
        )
        # Reads are bounded by the idle and turn timeouts below, not by httpx.
        self._client = httpx.AsyncClient(
            transport=transport,
            limits=self.pool_limits,
            timeout=httpx.Timeout(None, connect=settings.connect_timeout_s),
        )

    def __repr__(self) -> str:
        return f"OpenRouterClient(url={self._url!r})"

    async def aclose(self) -> None:
        await self._client.aclose()

    def build_body(self, request: LlmRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "stream": True,
            "max_tokens": request.max_tokens,
            "reasoning": {"effort": request.reasoning_effort, "exclude": True},
            "provider": request.provider_routing,
            "messages": request.messages,
        }
        if request.tools:
            body["tools"] = request.tools
        return body

    def _scrub(self, text: str) -> str:
        return text.replace(self._api_key.get_secret_value(), "[redacted]")[
            :_MAX_ERROR_MESSAGE_CHARS
        ]

    async def stream(self, request: LlmRequest) -> AsyncIterator[TextDelta | LlmCompletion]:
        turn_deadline = time.monotonic() + request.turn_timeout_s
        # Comment lines are keep-alives and show no progress, so they do not extend this.
        idle_deadline = time.monotonic() + request.idle_timeout_s

        def remaining() -> tuple[float, str]:
            if turn_deadline <= idle_deadline:
                return turn_deadline - time.monotonic(), "turn_timeout"
            return idle_deadline - time.monotonic(), "idle_timeout"

        async def bounded(awaitable: Any) -> Any:
            timeout, code = remaining()
            try:
                return await asyncio.wait_for(awaitable, max(timeout, 0))
            except asyncio.TimeoutError:
                raise LlmError(code, retryable=True) from None
            except httpx.TransportError as exc:
                raise LlmError("network_error", type(exc).__name__, retryable=True) from None

        http_request = self._client.build_request(
            "POST",
            self._url,
            json=self.build_body(request),
            headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
        )
        response: httpx.Response = await bounded(self._client.send(http_request, stream=True))
        try:
            if response.status_code != 200:
                raise await self._http_error(response)

            tools = _ToolCallAssembler()
            stripper = _LeadingBlankStripper()
            parts: list[str] = []
            finish_reason = provider = model = generation_id = None
            usage: LlmUsage | None = None

            async with aclosing(response.aiter_lines()) as lines:
                while True:
                    try:
                        line = await bounded(anext(lines))
                    except StopAsyncIteration:
                        break
                    if not line.startswith("data:"):
                        continue  # comment lines, blank separators and other SSE fields
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        raise LlmError("malformed_stream", retryable=True) from None
                    if not isinstance(chunk, dict):
                        raise LlmError("malformed_stream", retryable=True)
                    if chunk.get("error"):
                        raise self._stream_error(chunk["error"])

                    idle_deadline = time.monotonic() + request.idle_timeout_s
                    provider = chunk.get("provider") or provider
                    model = chunk.get("model") or model
                    generation_id = chunk.get("id") or generation_id
                    if chunk.get("usage"):
                        usage = self._usage(chunk["usage"])
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if content:
                            text = stripper.feed(content)
                            if text:
                                parts.append(text)
                                yield TextDelta(text=text)
                        for fragment in delta.get("tool_calls") or []:
                            tools.add(fragment)
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]

            if finish_reason is None:
                # The connection closed before the model finished.
                raise LlmError("stream_ended_early", retryable=True)
            yield LlmCompletion(
                text="".join(parts),
                tool_calls=tools.finish(),
                finish_reason=finish_reason,
                provider=provider,
                model=model,
                generation_id=generation_id,
                usage=usage,
                http_status=response.status_code,
            )
        finally:
            await response.aclose()

    async def _http_error(self, response: httpx.Response) -> LlmError:
        status = response.status_code
        message = ""
        try:
            body = await asyncio.wait_for(response.aread(), 5)
            parsed = json.loads(body[:_MAX_ERROR_BODY_BYTES])
            message = str((parsed.get("error") or {}).get("message", ""))
        except (asyncio.TimeoutError, ValueError, AttributeError, httpx.HTTPError):
            pass
        retryable = _is_retryable_status(status)
        return LlmError(
            "http_error" if retryable else "request_rejected",
            self._scrub(message),
            http_status=status,
            retryable=retryable,
            no_charge=True,
        )

    def _stream_error(self, error: Any) -> LlmError:
        """An `error` field means failure even with HTTP 200."""
        code = error.get("code") if isinstance(error, dict) else None
        message = str(error.get("message", "")) if isinstance(error, dict) else str(error)
        status = code if isinstance(code, int) else None
        retryable = status is None or _is_retryable_status(status)
        return LlmError(
            "provider_error" if retryable else "request_rejected",
            self._scrub(message),
            http_status=200,
            retryable=retryable,
        )

    @staticmethod
    def _usage(raw: dict[str, Any]) -> LlmUsage:
        completion = raw.get("completion_tokens_details") or {}
        prompt = raw.get("prompt_tokens_details") or {}
        cost = raw.get("cost")
        return LlmUsage(
            prompt_tokens=raw.get("prompt_tokens") or 0,
            completion_tokens=raw.get("completion_tokens") or 0,
            reasoning_tokens=completion.get("reasoning_tokens") or 0,
            cached_tokens=prompt.get("cached_tokens") or 0,
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        )
