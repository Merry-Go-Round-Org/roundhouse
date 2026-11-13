"""Docker sandbox: the agent's working environment.

Offline tests cover the commands it builds, and `test_docker_live.py` runs it against real
Docker. The harness team owns this code and the image. The backend owns datasets and model packages,
chooses what an agent gets for each run, and registers exported artifacts.

The image is defined in `sandbox_image/` and frozen by digest below. The digest covers linux/amd64,
which is official, and linux/arm64, which is for development only. The package is private, so run
`docker login ghcr.io` with a token that has `read:packages` first.
"""
from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import re
import signal
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Protocol

from pydantic import BaseModel, ConfigDict

from harness.profiles import (
    DEFAULT_PROFILES,
    INPUT_DIRS,
    SANDBOX_TOOL_NAMES,
    WORKSPACE_DIR,
    Profile,
)
from harness.schemas import (
    GatewayFactory,
    SandboxUnavailable,
    TaskInfo,
    TaskKind,
    ToolGateway,
    ToolResult,
    ToolSpec,
)

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
# Passed to the docker client only. Nothing reaches the container without an explicit flag,
# and this code never adds `--env`, so the API key cannot get in either way.
_CLIENT_ENV = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR", "TMPDIR", "LANG")
_TIMEOUT_EXIT_CODES = (124, 137)
_HOST_TIMEOUT_MARGIN_S = 10.0
_READER_GRACE_S = 2.0
_EXIT_POLL_S = 0.02


class SandboxConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    image: str = "ghcr.io/merry-go-round-org/sandbox@sha256:8083453253c3267caaa0fe36b43a287b9a986d5784148ad40714cd9d3fc2d80e"
    docker: str = "docker"
    user: str = "1000:1000"
    memory: str = "4g"
    cpus: str = "2"
    pids_limit: int = 256
    tmp_size: str = "256m"
    setup_timeout_s: float = 120.0
    file_timeout_s: float = 30.0
    default_exec_timeout_s: float = 300.0
    max_exec_timeout_s: float = 900.0
    max_output_chars: int = 20_000
    max_read_chars: int = 16_000
    max_write_chars: int = 16_000
    max_list_depth: int = 4

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> SandboxConfig:
        """`HARNESS_SANDBOX_CPUS` and `HARNESS_SANDBOX_MEMORY` set the per-container limits."""
        environ = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for variable, field in (("HARNESS_SANDBOX_CPUS", "cpus"), ("HARNESS_SANDBOX_MEMORY", "memory")):
            raw = environ.get(variable, "").strip()
            if raw:
                values[field] = raw
        return cls(**{**values, **overrides})


class SandboxMounts(BaseModel):
    """Host paths chosen by the backend, mounted read-only. None of them may hold final-test data.

    A task may receive only the inputs its profile lists. Anything else fails the task, so a
    resolver mistake cannot hand a validator the rebuild's inputs.
    """

    model_package_path: str | None = None  # /inputs/model
    development_data_path: str | None = None  # /inputs/data
    archive_path: str | None = None  # /inputs/archive: survey and intake
    document_path: str | None = None  # /inputs/document: the model document
    metrics_path: str | None = None  # /inputs/metrics: the metrics bundle, aggregates only
    monitoring_path: str | None = None  # /inputs/monitoring: batch, PSI, triage report
    reports_path: str | None = None  # /inputs/reports: the latest validation report

    def by_input(self) -> dict[str, str]:
        """Host path by input name, for the inputs that are set."""
        paths = {
            "model": self.model_package_path,
            "data": self.development_data_path,
            "archive": self.archive_path,
            "document": self.document_path,
            "metrics": self.metrics_path,
            "monitoring": self.monitoring_path,
            "reports": self.reports_path,
        }
        assert set(paths) == set(INPUT_DIRS)
        return {name: path for name, path in paths.items() if path is not None}


MountResolver = Callable[[TaskInfo], Awaitable[SandboxMounts]]


# ---------------------------------------------------------------- how many at once
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)b?\s*$", re.IGNORECASE)
_SIZE_FACTORS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def parse_memory(value: str) -> int:
    """Bytes in a Docker memory value, such as `4g` or `512m`."""
    match = _SIZE.match(value)
    if match is None:
        raise ValueError(f"not a memory size: {value!r}")
    return int(float(match.group(1)) * _SIZE_FACTORS[match.group(2).lower()])


def _machine_memory() -> int | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return None


def auto_sandbox_limit(
    config: SandboxConfig, *, cpus: int | None = None, memory: int | None = None
) -> int:
    """Containers that fit on this machine at the per-container limits, at least one.

    An estimate from the host. Docker Desktop runs in a VM with less, so set
    `max_concurrent_sandboxes` there.
    """
    cpus = cpus if cpus is not None else os.cpu_count() or 1
    memory = memory if memory is not None else _machine_memory()
    by_cpu = int(cpus // max(float(config.cpus), 0.01))
    limit = by_cpu
    if memory is not None:
        limit = min(limit, memory // max(parse_memory(config.memory), 1))
    return max(int(limit), 1)


class SandboxSlots:
    """One limit on the containers a process runs at once, shared by agent tasks and the
    scoring service. Work over the limit waits for a slot.
    """

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("the sandbox limit must be at least 1")
        self.limit = limit
        self._semaphore = asyncio.Semaphore(limit)
        self.in_use = 0

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        async with self._semaphore:
            self.in_use += 1
            try:
                yield
            finally:
                self.in_use -= 1


def bind_mount_args(host_path: str, target: str) -> list[str]:
    if "," in host_path or "\n" in host_path or "=" in host_path:
        raise SandboxUnavailable("unsafe mount path")
    return ["--mount", f"type=bind,src={host_path},dst={target},readonly"]


# ---------------------------------------------------------------- running processes
class ProcessResult(BaseModel):
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    truncated: bool = False


class ProcessRunner(Protocol):
    async def run(
        self, argv: list[str], *, timeout_s: float, max_output: int, stdin: bytes | None = None
    ) -> ProcessResult: ...


class AsyncioProcessRunner:
    """Runs an argument list with no shell. A timeout or a cancel kills the whole process group."""

    async def run(
        self, argv: list[str], *, timeout_s: float, max_output: int, stdin: bytes | None = None
    ) -> ProcessResult:
        env = {name: os.environ[name] for name in _CLIENT_ENV if name in os.environ}
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            raise SandboxUnavailable(f"cannot run {argv[0]}: {type(exc).__name__}") from None

        out, err = _CappedReader(max_output), _CappedReader(max_output)
        readers = [
            asyncio.create_task(out.drain(process.stdout)),
            asyncio.create_task(err.drain(process.stderr)),
        ]
        if stdin is not None:
            readers.append(asyncio.create_task(_feed(process.stdin, stdin)))
        timed_out = False
        try:
            try:
                await asyncio.wait_for(_exited(process), timeout_s)
            except asyncio.TimeoutError:
                timed_out = True
                _kill_group(process)
                await _exited(process)
            await asyncio.wait(readers, timeout=_READER_GRACE_S)
            await _clean_up(process)
        except asyncio.CancelledError:
            await _clean_up(process)
            raise
        finally:
            for reader in readers:
                reader.cancel()
        return ProcessResult(
            returncode=process.returncode,
            stdout=out.text(),
            stderr=err.text(),
            timed_out=timed_out,
            truncated=out.truncated or err.truncated,
        )


class _CappedReader:
    """Keeps the first bytes and discards the rest, so huge output cannot use up host memory."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._parts: list[bytes] = []
        self._size = 0
        self.truncated = False

    async def drain(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while chunk := await stream.read(65_536):
            room = self._limit - self._size
            if room > 0:
                self._parts.append(chunk[:room])
                self._size += min(room, len(chunk))
            if len(chunk) > room:
                self.truncated = True

    def text(self) -> str:
        return b"".join(self._parts).decode("utf-8", errors="replace")


async def _feed(stream: asyncio.StreamWriter | None, data: bytes) -> None:
    if stream is None:
        return
    try:
        stream.write(data)
        await stream.drain()
    finally:
        stream.close()


async def _exited(process: asyncio.subprocess.Process) -> None:
    # Not `process.wait()`, which also waits for the pipes to close, so a background child
    # holding them would make a finished command look as if it had timed out.
    while process.returncode is None:
        await asyncio.sleep(_EXIT_POLL_S)


async def _clean_up(process: asyncio.subprocess.Process) -> None:
    """End anything left in the group, which may be holding the pipes open, and let the
    transport close. Bounded, in case a child escaped the group.
    """
    _kill_group(process)
    await _exited(process)
    # asyncio has no public way to drop pipes that a surviving child still holds open.
    process._transport.close()  # type: ignore[attr-defined]
    closed = asyncio.ensure_future(process.wait())
    await asyncio.wait({closed}, timeout=_READER_GRACE_S)
    closed.cancel()


def _kill_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # the group has already gone


# ---------------------------------------------------------------- paths
class _BadPath(Exception):
    pass


def safe_relative_path(path: Any, *, allow_root: bool = False) -> str:
    """Accept only a path relative to the workspace, with no `..` and no absolute form."""
    if not isinstance(path, str) or "\x00" in path or "\\" in path:
        raise _BadPath("Invalid path.")
    if path.startswith("/") or path.startswith("~"):
        raise _BadPath("Paths must be relative to the workspace.")
    if ".." in path.split("/"):
        raise _BadPath("Paths must not contain '..'.")
    normalized = posixpath.normpath(path) if path else "."
    if normalized == "." and not allow_root:
        raise _BadPath("A file path is required.")
    return normalized


# ---------------------------------------------------------------- the sandbox
def _object_arguments(**properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "additionalProperties": False}


def sandbox_tool_specs(config: SandboxConfig) -> list[ToolSpec]:
    path = {"type": "string", "description": "Path relative to the workspace."}
    specs = [
        ToolSpec(
            name="sandbox_read_file",
            description=f"Read a file from the workspace, up to {config.max_read_chars} characters.",
            parameters={**_object_arguments(path=path), "required": ["path"]},
            risk="read",
        ),
        ToolSpec(
            name="sandbox_write_file",
            description=(
                f"Write up to {config.max_write_chars} characters to a workspace file. "
                "Set append to true to add to a file in several calls."
            ),
            parameters={
                **_object_arguments(
                    path=path, content={"type": "string"}, append={"type": "boolean"}
                ),
                "required": ["path", "content"],
            },
            risk="write",
        ),
        ToolSpec(
            name="sandbox_list_files",
            description="List files under a workspace directory.",
            parameters=_object_arguments(path=path),
            risk="read",
        ),
        ToolSpec(
            name="sandbox_exec",
            description=(
                "Run a command in the workspace, as an argument list with no shell. "
                "There is no network."
            ),
            parameters={
                **_object_arguments(
                    argv={"type": "array", "items": {"type": "string"}, "minItems": 1},
                    timeout_s={"type": "number"},
                ),
                "required": ["argv"],
            },
            risk="write",
        ),
    ]
    assert tuple(s.name for s in specs) == SANDBOX_TOOL_NAMES
    return specs


class DockerSandbox:
    """One container per task, and one volume per workspace, kept after the task ends."""

    def __init__(
        self,
        task: TaskInfo,
        mounts: SandboxMounts,
        config: SandboxConfig,
        runner: ProcessRunner,
        *,
        allowed_inputs: tuple[str, ...] | None = None,
    ) -> None:
        if not task.workspace_id:
            raise SandboxUnavailable("a chat task has no sandbox")
        for value in (task.task_id, task.workspace_id):
            if not _SAFE_NAME.match(value):
                raise SandboxUnavailable("unsafe sandbox identifier")
        if allowed_inputs is None:
            allowed_inputs = DEFAULT_PROFILES[task.kind].inputs
        refused = sorted(set(mounts.by_input()) - set(allowed_inputs))
        if refused:
            # The names are ours, not paths, so they are safe to put in the error.
            raise SandboxUnavailable(f"a {task.kind} task may not mount: {', '.join(refused)}")
        self.config = config
        self.mounts = mounts
        self.runner = runner
        self.container = f"mgr-task-{task.task_id}"
        self.volume = f"mgr-workspace-{task.workspace_id}"

    # ------------------------------------------------------------ commands
    def _docker(self, *args: str) -> list[str]:
        return [self.config.docker, *args]

    def run_command(self) -> list[str]:
        c = self.config
        command = self._docker(
            "run",
            "--detach",
            "--name", self.container,
            "--network", "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(c.pids_limit),
            "--memory", c.memory,
            "--cpus", c.cpus,
            "--read-only",
            "--user", c.user,
            "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={c.tmp_size}",
            "--workdir", WORKSPACE_DIR,
            "--mount", f"type=volume,src={self.volume},dst={WORKSPACE_DIR}",
        )  # fmt: skip
        for name, host_path in self.mounts.by_input().items():
            command += bind_mount_args(host_path, INPUT_DIRS[name])
        return [*command, c.image, "sleep", "infinity"]

    def exec_command(self, argv: list[str], *, interactive: bool = False) -> list[str]:
        flags = ["--interactive"] if interactive else []
        return self._docker("exec", *flags, "--workdir", WORKSPACE_DIR, self.container, *argv)

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        for command in (
            self._docker("version", "--format", "{{.Server.Version}}"),
            self._docker("volume", "create", self.volume),
            self.run_command(),
        ):
            result = await self._setup(command)
            if result.returncode != 0 or result.timed_out:
                # Stderr can quote paths and is for the operator, so it stays out of the error.
                logger.error("sandbox setup failed: %s exited with %s", command[1], result.returncode)
                raise SandboxUnavailable(f"docker {command[1]} failed")

    async def _setup(self, command: list[str]) -> ProcessResult:
        return await self.runner.run(
            command, timeout_s=self.config.setup_timeout_s, max_output=self.config.max_output_chars
        )

    async def destroy(self) -> None:
        """Remove the container and keep the volume, so a continuation finds the workspace."""
        await self._setup(self._docker("rm", "--force", self.container))

    # ------------------------------------------------------------ tools
    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        try:
            if name == "sandbox_read_file":
                return await self._read(args)
            if name == "sandbox_write_file":
                return await self._write(args)
            if name == "sandbox_list_files":
                return await self._list(args)
            if name == "sandbox_exec":
                return await self._exec(args)
        except _BadPath as exc:
            return ToolResult(status="denied", content=str(exc))
        return ToolResult(status="error", content=f"Unknown sandbox tool: {name}")

    async def _file_command(
        self, argv: list[str], *, stdin: bytes | None = None, max_output: int | None = None
    ) -> ProcessResult:
        return await self.runner.run(
            self.exec_command(argv, interactive=stdin is not None),
            timeout_s=self.config.file_timeout_s,
            max_output=max_output or self.config.max_output_chars,
            stdin=stdin,
        )

    async def _resolve(self, relative: str) -> str:
        """Resolve symlinks inside the container and reject anything leaving the workspace."""
        target = posixpath.normpath(posixpath.join(WORKSPACE_DIR, relative))
        result = await self._file_command(["realpath", "-m", "--", target])
        resolved = result.stdout.strip()
        if result.returncode != 0 or not (
            resolved == WORKSPACE_DIR or resolved.startswith(f"{WORKSPACE_DIR}/")
        ):
            raise _BadPath("The path leaves the workspace.")
        return resolved

    async def _read(self, args: dict[str, Any]) -> ToolResult:
        path = await self._resolve(safe_relative_path(args.get("path")))
        limit = self.config.max_read_chars
        # One byte more than the limit, to tell a complete file from a cut one.
        result = await self._file_command(["head", "-c", str(limit + 1), "--", path], max_output=limit + 1)
        if result.returncode != 0:
            return ToolResult(status="error", content=self._failure("Could not read the file", result))
        content = result.stdout
        if len(content) > limit:
            content = f"{content[:limit]}\n[Truncated: the file is longer than {limit} characters.]"
        return ToolResult(status="ok", content=content)

    async def _write(self, args: dict[str, Any]) -> ToolResult:
        relative = safe_relative_path(args.get("path"))
        content = args.get("content")
        if not isinstance(content, str):
            return ToolResult(status="error", content="content must be a string.")
        if len(content) > self.config.max_write_chars:
            return ToolResult(
                status="error",
                content=(
                    f"Too long: at most {self.config.max_write_chars} characters per call. "
                    "Split the content and set append to true."
                ),
            )
        path = await self._resolve(relative)
        made = await self._file_command(["mkdir", "-p", "--", posixpath.dirname(path)])
        if made.returncode != 0:
            return ToolResult(status="error", content=self._failure("Could not create the folder", made))
        tee = ["tee", "-a", "--", path] if args.get("append") else ["tee", "--", path]
        result = await self._file_command(tee, stdin=content.encode())
        if result.returncode != 0:
            return ToolResult(status="error", content=self._failure("Could not write the file", result))
        return ToolResult(status="ok", content=f"Wrote {len(content)} characters to {relative}.")

    async def _list(self, args: dict[str, Any]) -> ToolResult:
        relative = safe_relative_path(args.get("path", "."), allow_root=True)
        path = await self._resolve(relative)
        result = await self._file_command(
            ["find", "-P", path, "-maxdepth", str(self.config.max_list_depth), "-printf", "%y %s %P\n"]
        )
        if result.returncode != 0:
            return ToolResult(status="error", content=self._failure("Could not list the files", result))
        return ToolResult(status="ok", content=self._with_notice(result.stdout, result.truncated))

    async def _exec(self, args: dict[str, Any]) -> ToolResult:
        argv = args.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            return ToolResult(status="error", content="argv must be a non-empty list of strings.")
        requested = args.get("timeout_s", self.config.default_exec_timeout_s)
        if not isinstance(requested, (int, float)) or isinstance(requested, bool) or requested <= 0:
            return ToolResult(status="error", content="timeout_s must be a positive number.")
        timeout = min(float(requested), self.config.max_exec_timeout_s)
        # Killing the docker client would leave the process running in the container, so
        # `timeout` enforces the limit in there. The host limit is only a backstop.
        command = self.exec_command(["timeout", "--kill-after=5s", f"{timeout:g}s", *argv])
        try:
            result = await self.runner.run(
                command,
                timeout_s=timeout + _HOST_TIMEOUT_MARGIN_S,
                max_output=self.config.max_output_chars,
            )
        except asyncio.CancelledError:
            await self._kill_container()
            raise
        if result.timed_out:
            await self._kill_container()
        if result.timed_out or result.returncode in _TIMEOUT_EXIT_CODES:
            return ToolResult(
                status="error",
                content=f"The command timed out after {timeout:g}s.\n{self._output(result)}",
            )
        status = "ok" if result.returncode == 0 else "error"
        return ToolResult(status=status, content=f"exit code {result.returncode}\n{self._output(result)}")

    async def _kill_container(self) -> None:
        # Shielded, so the process really dies even while the caller is being cancelled.
        await asyncio.shield(self._setup(self._docker("kill", self.container)))

    def _output(self, result: ProcessResult) -> str:
        text = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        return self._with_notice(text, result.truncated)

    @staticmethod
    def _with_notice(text: str, truncated: bool) -> str:
        return f"{text}\n[Truncated: the output was too long.]" if truncated else text

    @staticmethod
    def _failure(what: str, result: ProcessResult) -> str:
        return f"{what}: {result.stderr.strip() or f'exit code {result.returncode}'}"


class _SandboxedGateway:
    """Adds sandbox tools to a task's platform tools. The sandbox's tools take precedence."""

    def __init__(self, platform: ToolGateway, sandbox: DockerSandbox) -> None:
        self._platform = platform
        self._sandbox = sandbox

    async def list_tools(self) -> list[ToolSpec]:
        platform = [t for t in await self._platform.list_tools() if t.name not in SANDBOX_TOOL_NAMES]
        return [*platform, *sandbox_tool_specs(self._sandbox.config)]

    async def call(self, name: str, args: dict[str, Any], idempotency_key: str) -> ToolResult:
        if name in SANDBOX_TOOL_NAMES:
            return await self._sandbox.call(name, args)
        return await self._platform.call(name, args, idempotency_key)


class SandboxGatewayFactory:
    """A task with a workspace gets a container, destroyed when the gateway closes.
    A chat task has no workspace, so it gets the platform gateway alone.
    """

    def __init__(
        self,
        platform: GatewayFactory,
        resolve_mounts: MountResolver,
        *,
        config: SandboxConfig | None = None,
        runner: ProcessRunner | None = None,
        profiles: Mapping[TaskKind, Profile] | None = None,
        slots: SandboxSlots | None = None,
    ) -> None:
        self._platform = platform
        self._resolve_mounts = resolve_mounts
        self._config = config or SandboxConfig()
        self._runner = runner or AsyncioProcessRunner()
        self._profiles = profiles or DEFAULT_PROFILES
        self._slots = slots

    @asynccontextmanager
    async def open(self, task: TaskInfo) -> AsyncIterator[ToolGateway]:
        async with self._platform.open(task) as platform:
            if not task.workspace_id:
                yield platform
                return
            sandbox = DockerSandbox(
                task,
                await self._resolve_mounts(task),
                self._config,
                self._runner,
                allowed_inputs=self._profiles[task.kind].inputs,
            )
            async with self._slot():
                try:
                    await sandbox.start()
                    yield _SandboxedGateway(platform, sandbox)
                finally:
                    try:
                        await sandbox.destroy()
                    except Exception as exc:
                        logger.error(
                            "could not remove %s: %s", sandbox.container, type(exc).__name__
                        )

    def _slot(self) -> Any:
        if self._slots is None:
            return _no_slot()
        return self._slots.hold()


@asynccontextmanager
async def _no_slot() -> AsyncIterator[None]:
    yield
