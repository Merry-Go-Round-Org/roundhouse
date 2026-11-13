"""Helpers for building requests and running tasks with scripted parts."""
from __future__ import annotations

import asyncio
from uuid import uuid4

from harness.config import HarnessSettings
from harness.dev.gateway import FakeGatewayFactory
from harness.loop import run_task
from harness.schemas import Actor, TaskContext, TaskRequest
from harness.store import SqlTaskStore


def make_request(prompt="hi", *, kind="chat", actor="alice", request_id=None, resolution=None, **context):
    return TaskRequest(
        request_id=request_id or uuid4().hex,
        actor=Actor(id=actor, role="developer"),
        kind=kind,
        prompt=prompt,
        context=TaskContext(policy_version="policy-1", **context),
        resolution=resolution,
    )


def fast_settings(**overrides) -> HarnessSettings:
    return HarnessSettings(**{"retry_base_delay_s": 0.0, **overrides})


def with_profile(settings: HarnessSettings, kind: str, **changes) -> HarnessSettings:
    profile = settings.profiles[kind].model_copy(update=changes)
    return settings.model_copy(update={"profiles": {**settings.profiles, kind: profile}})


class Harness:
    """A store, fake gateways and an event log, for one test."""

    def __init__(self, sessions, llm, settings=None, gateways=None):
        self.store = SqlTaskStore(sessions)
        self.llm = llm
        self.settings = settings or fast_settings()
        self.gateways = gateways or FakeGatewayFactory()
        self.events = []
        self.cancel = asyncio.Event()

    async def create(self, request, continue_from=None) -> str:
        profile = self.settings.profiles[request.kind]
        task_id, _ = await self.store.create_task(request, profile, continue_from)
        return task_id

    async def run(self, task_id):
        return await run_task(
            task_id,
            store=self.store,
            llm=self.llm,
            gateways=self.gateways,
            emit=self.events.append,
            cancel=self.cancel,
            settings=self.settings,
        )

    async def start(self, request=None, continue_from=None):
        task_id = await self.create(request or make_request(), continue_from)
        return task_id, await self.run(task_id)

    def event_types(self):
        return [e.type for e in self.events]

    def events_of(self, event_type):
        return [e for e in self.events if e.type == event_type]
