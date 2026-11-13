"""Import rules, schema rules, profile rules and table creation."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text

import harness
from harness.config import HarnessSettings, Prices
from harness.profiles import (
    CHAT_PROFILE,
    DEFAULT_PROFILES,
    REBUILD_PROFILE,
    Profile,
    apply_tool_policy,
)
from harness.schemas import Actor, TaskContext, TaskRequest, ToolResult, ToolSpec

PACKAGE_ROOT = Path(harness.__file__).parent


def imported_modules(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def production_files() -> list[Path]:
    return [p for p in PACKAGE_ROOT.rglob("*.py") if "dev" not in p.relative_to(PACKAGE_ROOT).parts]


def test_harness_imports_neither_app_nor_contracts():
    """`runner.py` is the bridge, so it alone may import the contract."""
    for path in PACKAGE_ROOT.rglob("*.py"):
        banned = {"app"} if path == PACKAGE_ROOT / "runner.py" else {"app", "contracts"}
        for module in imported_modules(path):
            assert module.split(".")[0] not in banned, f"{path}: {module}"


def test_nothing_outside_dev_imports_dev():
    for path in production_files():
        for module in imported_modules(path):
            assert not module.startswith("harness.dev"), f"{path}: {module}"


def test_loop_does_not_import_sandbox():
    loop = PACKAGE_ROOT / "loop.py"
    if loop.exists():
        assert not any(m.startswith("harness.sandbox") for m in imported_modules(loop))


def test_package_import_does_not_pull_in_httpx():
    # httpx stays a dev dependency until the backend integration moves it to runtime.
    assert imported_modules(PACKAGE_ROOT / "__init__.py") == set()


@pytest.mark.parametrize("kind", ["chat", "document"])
def test_medium_effort_is_rejected_outside_rebuild(kind):
    with pytest.raises(ValidationError, match="only allowed for rebuild"):
        DEFAULT_PROFILES[kind].model_validate(
            {**DEFAULT_PROFILES[kind].model_dump(), "effort": "medium"}
        )


def test_medium_effort_is_allowed_for_rebuild():
    assert REBUILD_PROFILE.effort == "medium"
    assert CHAT_PROFILE.effort == "low"


def test_effort_outside_low_and_medium_is_rejected():
    with pytest.raises(ValidationError):
        Profile.model_validate({**REBUILD_PROFILE.model_dump(), "effort": "high"})


def test_profile_values_match_the_plan():
    expected = {
        "chat": (4_000, 300, 45, 120, 8, 0.05, 0.50, 100_000),
        "document": (6_500, 300, 120, 300, 20, 0.50, 1.00, 200_000),
        "rebuild": (8_000, 2_100, 120, 300, 40, 2.00, 10.00, 400_000),
    }
    for kind, values in expected.items():
        p = DEFAULT_PROFILES[kind]
        assert (
            p.max_tokens,
            p.reasoning_reserve,
            p.idle_timeout_s,
            p.turn_timeout_s,
            p.max_turns,
            p.task_budget_usd,
            p.thread_budget_usd,
            p.max_prompt_tokens,
        ) == values
    assert DEFAULT_PROFILES["chat"].sandbox is False
    assert DEFAULT_PROFILES["rebuild"].sandbox is True
    assert DEFAULT_PROFILES["document"].sandbox is True


def test_settings_reject_a_profile_under_the_wrong_kind():
    with pytest.raises(ValidationError, match="is for chat"):
        HarnessSettings(profiles={**DEFAULT_PROFILES, "rebuild": CHAT_PROFILE})


def test_default_settings_match_the_plan():
    s = HarnessSettings()
    assert s.model == "qwen/qwen3.8-27b"
    assert s.provider_routing == {"data_collection": "deny", "require_parameters": True}
    assert (s.llm_concurrency, s.chat_concurrency, s.background_concurrency) == (16, 16, 8)
    assert s.max_concurrent_sandboxes is None  # from the machine
    assert s.max_queued == 100
    assert s.global_budget_usd == 40.0
    assert s.archive_budget_usd == 3.0


def test_prices_treat_cached_tokens_separately():
    prices = Prices()
    assert prices.cost(1_000_000, 0) == pytest.approx(0.15)
    assert prices.cost(0, 1_000_000) == pytest.approx(1.875)
    assert prices.cost(1_000_000, 0, cached_tokens=1_000_000) == pytest.approx(0.0375)


def _tool(name: str) -> ToolSpec:
    return ToolSpec(name=name, description="", parameters={}, risk="read")


def test_tool_policy():
    tools = [_tool("get_status"), _tool("run_parity"), _tool("export_artifact"), _tool("sandbox_exec")]
    assert [t.name for t in apply_tool_policy(CHAT_PROFILE, tools)] == [
        "get_status",
        "run_parity",
        "export_artifact",
    ]
    # The rebuild agent calls official parity itself, the same check a person runs.
    assert [t.name for t in apply_tool_policy(REBUILD_PROFILE, tools)] == [
        "run_parity",
        "export_artifact",
        "sandbox_exec",
    ]


@pytest.mark.parametrize("name", ["", "has space", "a" * 65, "semi;colon"])
def test_tool_names_are_validated(name):
    with pytest.raises(ValidationError):
        _tool(name)


def test_pending_status_requires_a_pending_id():
    with pytest.raises(ValidationError):
        ToolResult(status="needs_review", content="x")
    assert ToolResult(status="needs_review", content="x", pending_id="p").pending_id == "p"


def test_task_request_rejects_fields_the_llm_or_caller_must_not_set():
    with pytest.raises(ValidationError):
        TaskRequest(
            request_id="r",
            actor=Actor(id="u", role="dev"),
            kind="chat",
            context=TaskContext(policy_version="p1"),
            effort="medium",
        )


def test_tables_and_partial_index_are_created_with_wal(database):
    async def scenario():
        async with database:
            async with database.engine.connect() as connection:
                names = await connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")
                )
                journal = await connection.execute(text("PRAGMA journal_mode"))
                busy = await connection.execute(text("PRAGMA busy_timeout"))
                return {r[0] for r in names}, journal.scalar(), busy.scalar()

    names, journal, busy = asyncio.run(scenario())
    assert {"agent_tasks", "agent_messages", "llm_calls"} <= names
    assert "uq_agent_tasks_one_active_per_thread" in names
    assert journal == "wal"
    assert busy == 5000


def _scores(path, rows):
    path.write_text("row_id,positive_probability\n" + "".join(f"{i},{p}\n" for i, p in rows))
    return path


def test_parity_comparison_uses_the_frozen_tolerance(tmp_path):
    from harness.dev.live import compare_scores

    reference = _scores(tmp_path / "ref.csv", [("a", 0.5), ("b", 0.25)])
    close = _scores(tmp_path / "close.csv", [("a", 0.5 + 9e-9), ("b", 0.25)])
    far = _scores(tmp_path / "far.csv", [("a", 0.5 + 2e-8), ("b", 0.25)])
    swapped = _scores(tmp_path / "swapped.csv", [("b", 0.25), ("a", 0.5)])
    short = _scores(tmp_path / "short.csv", [("a", 0.5)])
    assert compare_scores(close, reference)["passed"] is True
    assert compare_scores(far, reference)["passed"] is False
    assert compare_scores(swapped, reference)["passed"] is False
    assert compare_scores(short, reference)["passed"] is False
    assert compare_scores(tmp_path / "missing.csv", reference)["passed"] is False
    wrong = tmp_path / "wrong.csv"
    wrong.write_text("row_id,score\na,0.5\nb,0.25\n")
    assert compare_scores(wrong, reference)["passed"] is False
    empty = tmp_path / "empty.csv"
    empty.write_text("row_id,positive_probability\n")
    assert compare_scores(empty, _scores(tmp_path / "none.csv", []))["passed"] is False
