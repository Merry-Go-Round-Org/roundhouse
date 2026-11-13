"""Model, prices, limits and concurrency. Values are starting points from a few short tests,
to be reviewed after the first real runs. All limits are per process.
"""
from __future__ import annotations

import os
import socket
from typing import Any, Mapping, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.profiles import DEFAULT_PROFILES, Profile
from harness.schemas import TaskKind

_ALL_KINDS: tuple[TaskKind, ...] = get_args(TaskKind)
# Survey and intake runs of one archive share `archive_budget_usd`.
ARCHIVE_KINDS: tuple[TaskKind, ...] = ("survey", "intake")


class Prices(BaseModel):
    """USD per million tokens."""

    model_config = ConfigDict(frozen=True)

    input_per_mtok: float = 0.15
    output_per_mtok: float = 1.875
    cached_input_per_mtok: float = 0.0375

    def cost(self, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float:
        uncached = max(prompt_tokens - cached_tokens, 0)
        return (
            uncached * self.input_per_mtok
            + cached_tokens * self.cached_input_per_mtok
            + completion_tokens * self.output_per_mtok
        ) / 1_000_000


def _default_owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class HarnessSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str = "qwen/qwen3.8-27b"
    base_url: str = "https://openrouter.ai/api/v1"
    provider_routing: dict[str, Any] = Field(
        default_factory=lambda: {"data_collection": "deny", "require_parameters": True}
    )
    prices: Prices = Field(default_factory=Prices)
    profiles: dict[TaskKind, Profile] = Field(default_factory=lambda: dict(DEFAULT_PROFILES))

    llm_concurrency: int = Field(16, ge=1)  # max_concurrent_llm_requests in the proposal
    chat_concurrency: int = Field(16, ge=1)
    # Every kind except chat, shared. Each of these runs in a sandbox, so
    # `max_concurrent_sandboxes` is the real limit on the machine. Work over it waits.
    background_concurrency: int = Field(8, ge=1)
    # Agent and scoring containers at once. None means from the machine's CPUs and memory,
    # divided by the per-container limits in `SandboxConfig`.
    max_concurrent_sandboxes: int | None = Field(None, ge=1)
    max_queued: int = Field(100, ge=0)

    max_llm_retries: int = 2
    retry_base_delay_s: float = 1.0
    connect_timeout_s: float = 10.0

    max_tool_result_chars: int = 20_000
    global_budget_usd: float = 40.0  # the OpenRouter key itself has a $50 limit
    archive_budget_usd: float = 3.0  # all survey and intake runs of one archive

    heartbeat_interval_s: float = 15.0
    orphan_timeout_s: float = 60.0
    wait_poll_interval_s: float = 0.5
    event_queue_size: int = 1_000
    owner_id: str = Field(default_factory=_default_owner_id)

    @model_validator(mode="after")
    def _check_profiles(self) -> HarnessSettings:
        for kind in _ALL_KINDS:
            profile = self.profiles.get(kind)
            if profile is None:
                raise ValueError(f"no profile configured for {kind}")
            if profile.kind != kind:
                raise ValueError(f"profile {profile.name} is for {profile.kind}, not {kind}")
        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> HarnessSettings:
        """Settings with limits taken from `HARNESS_*` environment variables, so an operator can
        size them for the machine without a code change. Unset variables keep the defaults.
        """
        environ = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for variable, field in ENV_SETTINGS.items():
            raw = environ.get(variable)
            if raw is not None and raw.strip():
                values[field] = raw.strip()
        return cls(**{**values, **overrides})


ENV_SETTINGS: dict[str, str] = {
    "HARNESS_MODEL": "model",
    "HARNESS_MAX_CONCURRENT_LLM_REQUESTS": "llm_concurrency",
    "HARNESS_CHAT_CONCURRENCY": "chat_concurrency",
    "HARNESS_BACKGROUND_CONCURRENCY": "background_concurrency",
    "HARNESS_MAX_CONCURRENT_SANDBOXES": "max_concurrent_sandboxes",
    "HARNESS_MAX_QUEUED": "max_queued",
    "HARNESS_GLOBAL_BUDGET_USD": "global_budget_usd",
    "HARNESS_ARCHIVE_BUDGET_USD": "archive_budget_usd",
}
