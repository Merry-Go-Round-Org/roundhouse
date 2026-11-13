"""Docker sandbox, tested with a fake process runner. Docker itself is never called."""
from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from harness.dev.gateway import FakeGatewayFactory, ScriptedLlm, text_turn, tool_turn
from harness.profiles import SANDBOX_TOOL_NAMES
from harness.sandbox import (
    AsyncioProcessRunner,
    DockerSandbox,
    ProcessResult,
    SandboxConfig,
    SandboxGatewayFactory,
    SandboxMounts,
    safe_relative_path,
    _BadPath,
)
from harness.schemas import SandboxUnavailable, TaskInfo
from harness_helpers import Harness, make_request

MOUNTS = SandboxMounts(model_package_path="/data/pkg-1", development_data_path="/data/dev-1")


def task_info(kind="rebuild", workspace="ws1"):
    return TaskInfo(
        task_id="task1",
        thread_id="task1",
        workspace_id=workspace,
        kind=kind,
        actor_id="alice",
        actor_role="developer",
        policy_version="p1",
        model_package_id="pkg-1",
        development_data_id="dev-1",
    )


class FakeRunner:
    def __init__(self, respond=None):
        self.commands = []
        self.stdin = []
        self.timeouts = []
        self.respond = respond

    async def run(self, argv, *, timeout_s, max_output, stdin=None):
        self.commands.append(argv)
        self.stdin.append(stdin)
        self.timeouts.append(timeout_s)
        if self.respond is not None:
            result = await self.respond(argv)
            if result is not None:
                return result
        if "realpath" in argv:
            return ProcessResult(returncode=0, stdout=argv[-1] + "\n")
        return ProcessResult(returncode=0, stdout="output")

    def inner(self):
        """Commands run inside the container, without the docker exec prefix."""
        result = []
        for command in self.commands:
            if command[:2] == ["docker", "exec"]:
                result.append(command[command.index("mgr-task-task1") + 1 :])
        return result


def sandbox(runner=None, config=None, mounts=MOUNTS):
    return DockerSandbox(task_info(), mounts, config or SandboxConfig(), runner or FakeRunner())


def call(box, name, **args):
    return asyncio.run(box.call(name, args))


# ---------------------------------------------------------------- the container command
def test_container_command_has_every_isolation_flag():
    command = sandbox().run_command()
    joined = " ".join(command)
    for flag in (
        "--network none",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit 256",
        "--memory 4g",
        "--cpus 2",
        "--read-only",
        "--user 1000:1000",
    ):
        assert flag in joined, flag
    assert "/tmp:rw,noexec,nosuid,nodev,size=256m" in command
    assert command[-3:] == [SandboxConfig().image, "sleep", "infinity"]
    assert command[:2] == ["docker", "run"]


def test_workspace_is_writable_and_inputs_are_read_only():
    mounts = [v for k, v in zip(sandbox().run_command(), sandbox().run_command()[1:]) if k == "--mount"]
    assert mounts == [
        "type=volume,src=mgr-workspace-ws1,dst=/workspace",
        "type=bind,src=/data/pkg-1,dst=/inputs/model,readonly",
        "type=bind,src=/data/dev-1,dst=/inputs/data,readonly",
    ]


def test_missing_inputs_are_not_mounted():
    command = sandbox(mounts=SandboxMounts()).run_command()
    assert command.count("--mount") == 1


def test_no_credential_or_docker_socket_is_passed_in(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    command = sandbox().run_command()
    joined = " ".join(command)
    assert "--env" not in command and "-e" not in command and "--env-file" not in command
    assert "sk-or-secret" not in joined
    assert "docker.sock" not in joined
    assert "--privileged" not in command


def test_real_runner_gives_the_client_no_credentials(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    code = "import os; print('OPENROUTER_API_KEY' in os.environ)"

    result = asyncio.run(
        AsyncioProcessRunner().run([sys.executable, "-I", "-c", code], timeout_s=30, max_output=100)
    )
    assert result.returncode == 0 and result.stdout.strip() == "False"


@pytest.mark.parametrize("bad", ["../evil", "a b", "a;b", "", "-rf"])
def test_unsafe_identifiers_are_rejected(bad):
    info = task_info().model_copy(update={"task_id": bad})
    with pytest.raises(SandboxUnavailable):
        DockerSandbox(info, MOUNTS, SandboxConfig(), FakeRunner())


def test_mount_path_with_a_comma_is_rejected():
    box = sandbox(mounts=SandboxMounts(model_package_path="/a,dst=/etc"))
    with pytest.raises(SandboxUnavailable):
        box.run_command()


def test_chat_task_cannot_get_a_sandbox():
    with pytest.raises(SandboxUnavailable):
        DockerSandbox(task_info("chat", None), MOUNTS, SandboxConfig(), FakeRunner())


# ---------------------------------------------------------------- lifecycle
def test_start_checks_docker_creates_the_volume_and_destroy_keeps_it():
    runner = FakeRunner()
    box = sandbox(runner)

    async def main():
        await box.start()
        await box.destroy()

    asyncio.run(main())
    assert [c[1] for c in runner.commands] == ["version", "volume", "run", "rm"]
    assert runner.commands[1] == ["docker", "volume", "create", "mgr-workspace-ws1"]
    assert runner.commands[-1] == ["docker", "rm", "--force", "mgr-task-task1"]
    assert not [c for c in runner.commands if "volume" in c and "rm" in c]


def test_docker_daemon_down_is_unavailable():
    async def respond(argv):
        return ProcessResult(returncode=1, stderr="Cannot connect to the Docker daemon")

    with pytest.raises(SandboxUnavailable):
        asyncio.run(sandbox(FakeRunner(respond)).start())


def test_missing_docker_binary_is_unavailable():
    config = SandboxConfig(docker="/nonexistent/docker-binary")
    box = DockerSandbox(task_info(), MOUNTS, config, AsyncioProcessRunner())
    with pytest.raises(SandboxUnavailable):
        asyncio.run(box.start())


# ---------------------------------------------------------------- paths
@pytest.mark.parametrize(
    "path",
    ["../x", "a/../../x", "/etc/passwd", "~/x", "a/..", "..", "a\x00b", "a\\..\\b", 7, None],
)
def test_path_escapes_are_rejected(path):
    with pytest.raises(_BadPath):
        safe_relative_path(path)


@pytest.mark.parametrize("path, expected", [("a/b.py", "a/b.py"), ("./a//b", "a/b")])
def test_safe_paths_are_normalized(path, expected):
    assert safe_relative_path(path) == expected


@pytest.mark.parametrize(
    "tool, args",
    [
        ("sandbox_read_file", {"path": "../secret"}),
        ("sandbox_write_file", {"path": "/etc/passwd", "content": "x"}),
        ("sandbox_list_files", {"path": "../.."}),
    ],
)
def test_escaping_tool_calls_are_denied_without_running_anything(tool, args):
    runner = FakeRunner()
    result = call(sandbox(runner), tool, **args)
    assert result.status == "denied"
    assert runner.commands == []


@pytest.mark.parametrize(
    "tool, args",
    [
        ("sandbox_read_file", {"path": "link"}),
        ("sandbox_write_file", {"path": "link", "content": "x"}),
        ("sandbox_list_files", {"path": "link"}),
    ],
)
def test_escaping_symlinks_are_rejected(tool, args):
    async def respond(argv):
        if "realpath" in argv:
            return ProcessResult(returncode=0, stdout="/inputs/model/secret\n")
        return None

    runner = FakeRunner(respond)
    result = call(sandbox(runner), tool, **args)
    assert result.status == "denied" and "leaves the workspace" in result.content
    assert [c[0] for c in runner.inner()] == ["realpath"]


def test_prefix_lookalike_is_not_inside_the_workspace():
    async def respond(argv):
        if "realpath" in argv:
            return ProcessResult(returncode=0, stdout="/workspace-evil/x\n")
        return None

    assert call(sandbox(FakeRunner(respond)), "sandbox_read_file", path="link").status == "denied"


# ---------------------------------------------------------------- file tools
def test_read_file_uses_no_shell_and_marks_truncation():
    async def respond(argv):
        if "head" in argv:
            return ProcessResult(returncode=0, stdout="x" * 17)
        return None

    runner = FakeRunner(respond)
    result = call(sandbox(runner, SandboxConfig(max_read_chars=16)), "sandbox_read_file", path="a.txt")
    assert runner.inner()[-1] == ["head", "-c", "17", "--", "/workspace/a.txt"]
    assert result.status == "ok"
    assert result.content.startswith("x" * 16 + "\n[Truncated")


def test_write_file_creates_folders_and_sends_content_on_stdin():
    runner = FakeRunner()
    result = call(sandbox(runner), "sandbox_write_file", path="src/a.py", content="print(1)")
    assert result.status == "ok"
    assert runner.inner()[-2:] == [
        ["mkdir", "-p", "--", "/workspace/src"],
        ["tee", "--", "/workspace/src/a.py"],
    ]
    assert runner.stdin[-1] == b"print(1)"
    assert "print(1)" not in " ".join(runner.commands[-1])
    assert "--interactive" in runner.commands[-1]


def test_write_file_supports_appending():
    runner = FakeRunner()
    call(sandbox(runner), "sandbox_write_file", path="a", content="x", append=True)
    assert runner.inner()[-1] == ["tee", "-a", "--", "/workspace/a"]


def test_write_file_has_a_limit_of_16000_characters():
    runner = FakeRunner()
    assert SandboxConfig().max_write_chars == 16_000
    assert call(sandbox(runner), "sandbox_write_file", path="a", content="x" * 16_000).status == "ok"
    runner = FakeRunner()
    result = call(sandbox(runner), "sandbox_write_file", path="a", content="x" * 16_001)
    assert result.status == "error" and "append" in result.content
    assert runner.commands == []


def test_list_files_defaults_to_the_workspace():
    runner = FakeRunner()
    assert call(sandbox(runner), "sandbox_list_files").status == "ok"
    assert runner.inner()[-1][:3] == ["find", "-P", "/workspace"]


# ---------------------------------------------------------------- commands
def test_exec_uses_an_argument_list_without_a_shell():
    runner = FakeRunner()
    result = call(sandbox(runner), "sandbox_exec", argv=["python", "-c", "print('a; rm -rf /')"], timeout_s=5)
    assert result.status == "ok" and "exit code 0" in result.content
    assert runner.commands[-1] == [
        "docker", "exec", "--workdir", "/workspace", "mgr-task-task1",
        "timeout", "--kill-after=5s", "5s",
        "python", "-c", "print('a; rm -rf /')",
    ]  # fmt: skip
    assert "sh" not in runner.commands[-1] and "bash" not in runner.commands[-1]


@pytest.mark.parametrize("argv", ["ls -la", [], [1, 2], None])
def test_exec_rejects_anything_but_an_argument_list(argv):
    runner = FakeRunner()
    assert call(sandbox(runner), "sandbox_exec", argv=argv).status == "error"
    assert runner.commands == []


@pytest.mark.parametrize("timeout", [0, -1, "5", True])
def test_exec_rejects_bad_timeouts(timeout):
    runner = FakeRunner()
    assert call(sandbox(runner), "sandbox_exec", argv=["ls"], timeout_s=timeout).status == "error"
    assert runner.commands == []


def test_llm_cannot_extend_the_timeout_past_the_limit():
    runner = FakeRunner()
    call(sandbox(runner), "sandbox_exec", argv=["ls"], timeout_s=999_999)
    assert "900s" in runner.commands[-1]
    assert runner.timeouts[-1] == 910


def test_nonzero_exit_is_an_error_with_output():
    async def respond(_argv):
        return ProcessResult(returncode=3, stdout="out", stderr="err")

    result = call(sandbox(FakeRunner(respond)), "sandbox_exec", argv=["false"])
    assert result.status == "error"
    assert "exit code 3" in result.content and "out" in result.content and "err" in result.content


@pytest.mark.parametrize("code", [124, 137])
def test_timeout_inside_the_container_is_reported(code):
    async def respond(_argv):
        return ProcessResult(returncode=code, stdout="partial")

    runner = FakeRunner(respond)
    result = call(sandbox(runner), "sandbox_exec", argv=["sleep", "99"], timeout_s=1)
    assert result.status == "error" and "timed out" in result.content and "partial" in result.content


def test_host_timeout_kills_the_container():
    async def respond(argv):
        if argv[1] == "exec":
            return ProcessResult(returncode=-9, timed_out=True)
        return None

    runner = FakeRunner(respond)
    result = call(sandbox(runner), "sandbox_exec", argv=["sleep", "99"], timeout_s=1)
    assert "timed out" in result.content
    assert runner.commands[-1] == ["docker", "kill", "mgr-task-task1"]


def test_cancel_kills_the_process():
    started = asyncio.Event()

    async def respond(argv):
        if argv[1] == "exec":
            started.set()
            await asyncio.sleep(3600)
        return None

    runner = FakeRunner(respond)

    async def main():
        task = asyncio.create_task(sandbox(runner).call("sandbox_exec", {"argv": ["sleep", "99"]}))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert runner.commands[-1] == ["docker", "kill", "mgr-task-task1"]


def test_output_is_truncated_with_a_notice():
    async def respond(_argv):
        return ProcessResult(returncode=0, stdout="x" * 10, truncated=True)

    result = call(sandbox(FakeRunner(respond)), "sandbox_exec", argv=["yes"])
    assert "[Truncated" in result.content


# ---------------------------------------------------------------- the real process runner
def _script(code):
    return [sys.executable, "-I", "-c", code]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_real_runner_captures_output_and_exit_code():
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(4)"
    result = asyncio.run(AsyncioProcessRunner().run(_script(code), timeout_s=30, max_output=100))
    assert (result.returncode, result.stdout.strip(), result.stderr.strip()) == (4, "out", "err")
    assert not result.timed_out and not result.truncated


def test_real_runner_passes_stdin():
    code = "import sys; print(sys.stdin.read().upper())"
    result = asyncio.run(
        AsyncioProcessRunner().run(_script(code), timeout_s=30, max_output=100, stdin=b"hello")
    )
    assert result.stdout.strip() == "HELLO"


def test_real_runner_caps_output():
    code = "import sys; sys.stdout.write('x' * 500000)"
    result = asyncio.run(AsyncioProcessRunner().run(_script(code), timeout_s=30, max_output=1000))
    assert len(result.stdout) == 1000 and result.truncated


def test_real_runner_does_not_hang_on_a_background_child_holding_the_pipes():
    code = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print('parent done', flush=True)\n"
    )
    started = time.monotonic()
    result = asyncio.run(AsyncioProcessRunner().run(_script(code), timeout_s=25, max_output=100))
    assert result.returncode == 0 and "parent done" in result.stdout
    assert time.monotonic() - started < 15


def test_real_runner_timeout_kills_the_process_group(tmp_path):
    # The child starts a grandchild, which must die too.
    pid_file = tmp_path / "pid"
    code = (
        "import subprocess, sys, pathlib, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    result = asyncio.run(AsyncioProcessRunner().run(_script(code), timeout_s=1.5, max_output=100))
    assert result.timed_out and time.monotonic() - started < 30
    grandchild = int(pid_file.read_text())
    for _ in range(100):
        if not _alive(grandchild):
            break
        time.sleep(0.02)
    assert not _alive(grandchild)


def test_real_runner_cancel_kills_the_process(tmp_path):
    pid_file = tmp_path / "pid"
    code = (
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )

    async def main():
        task = asyncio.create_task(
            AsyncioProcessRunner().run(_script(code), timeout_s=60, max_output=100)
        )
        for _ in range(500):
            if pid_file.exists() and pid_file.read_text():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    pid = int(pid_file.read_text())
    assert not _alive(pid)


# ---------------------------------------------------------------- with the agent loop
async def _resolve_mounts(_task):
    return MOUNTS


def _play(database, llm, request, runner):
    platform = FakeGatewayFactory()
    gateways = SandboxGatewayFactory(platform, _resolve_mounts, runner=runner)

    async def main():
        async with database as sessions:
            h = Harness(sessions, llm, gateways=gateways)
            _, result = await h.start(request)
            return result, platform

    return asyncio.run(main())


def test_rebuild_gets_sandbox_tools_and_its_container_is_destroyed(database):
    runner = FakeRunner()
    llm = ScriptedLlm(
        [
            tool_turn(("sandbox_write_file", {"path": "a.py", "content": "x"}), ("export_artifact", {"path": "a.py"})),
            text_turn("exported"),
        ]
    )
    result, platform = _play(database, llm, make_request("go", kind="rebuild"), runner)
    assert result.status == "completed"
    names = [t["function"]["name"] for t in llm.requests[0].tools]
    assert set(SANDBOX_TOOL_NAMES) <= set(names)
    assert [c.name for c in platform.calls] == ["export_artifact"]  # sandbox tools stay local
    assert runner.commands[-1][:3] == ["docker", "rm", "--force"]
    assert [c[1] for c in runner.commands[:3]] == ["version", "volume", "run"]


def test_chat_never_starts_a_container(database):
    runner = FakeRunner()
    llm = ScriptedLlm([text_turn("hello")])
    result, _ = _play(database, llm, make_request("hi"), runner)
    assert result.status == "completed"
    assert runner.commands == []
    assert not [t for t in llm.requests[0].tools if t["function"]["name"].startswith("sandbox_")]


def test_docker_unavailable_fails_rebuild_with_sandbox_unavailable(database):
    async def respond(_argv):
        raise SandboxUnavailable("no docker")

    # destroy() also hits the failing runner, which must not hide the original error.
    llm = ScriptedLlm([text_turn("never")])
    result, _ = _play(database, llm, make_request("go", kind="document"), FakeRunner(respond))
    assert (result.status, result.error_code) == ("failed", "sandbox_unavailable")
    assert llm.requests == []
