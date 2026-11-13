"""The sandbox against real Docker. Skipped unless HARNESS_DOCKER_TESTS=1, and no LLM is used.

    docker pull ghcr.io/merry-go-round-org/sandbox@sha256:8083453253c3267caaa0fe36b43a287b9a986d5784148ad40714cd9d3fc2d80e
    HARNESS_DOCKER_TESTS=1 python -m pytest tests/agent_harness/test_docker_live.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from harness.sandbox import AsyncioProcessRunner, DockerSandbox, SandboxConfig, SandboxMounts
from harness.schemas import SandboxUnavailable, TaskInfo

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_DOCKER_TESTS") != "1", reason="set HARNESS_DOCKER_TESTS=1 to run"
)


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


class Box:
    """A real sandbox for one test, with its container and volume removed afterwards."""

    def __init__(self, inputs, workspace=None, config=None):
        self.workspace = workspace or uuid4().hex
        self.task_id = uuid4().hex
        info = TaskInfo(
            task_id=self.task_id,
            thread_id=self.task_id,
            workspace_id=self.workspace,
            kind="rebuild",
            actor_id="alice",
            actor_role="developer",
            policy_version="p1",
        )
        self.sandbox = DockerSandbox(
            info,
            SandboxMounts(
                model_package_path=str(inputs / "model"),
                development_data_path=str(inputs / "data"),
            ),
            config or SandboxConfig(),
            AsyncioProcessRunner(),
        )

    def call(self, name, **args):
        return asyncio.run(self.sandbox.call(name, args))

    def run(self, *argv, timeout_s=60):
        return self.call("sandbox_exec", argv=list(argv), timeout_s=timeout_s)

    def python(self, code, timeout_s=60):
        return self.run("python", "-c", code, timeout_s=timeout_s)

    def __enter__(self):
        asyncio.run(self.sandbox.start())
        return self

    def __exit__(self, *_exc):
        asyncio.run(self.sandbox.destroy())


@pytest.fixture
def inputs(tmp_path):
    (tmp_path / "model").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "model" / "score.py").write_text("print('legacy')\n")
    (tmp_path / "data" / "dev.csv").write_text("a,b\n1,2\n")
    for folder in (tmp_path, tmp_path / "model", tmp_path / "data"):
        folder.chmod(0o755)
    return tmp_path


@pytest.fixture
def box(inputs):
    created = Box(inputs)
    try:
        with created as running:
            yield running
    finally:
        _docker("volume", "rm", "--force", created.sandbox.volume)


# ---------------------------------------------------------------- isolation
def test_no_network_of_any_kind(box):
    code = (
        "import socket\n"
        "for table in ('/proc/net/route', '/proc/net/ipv6_route'):\n"
        "    rows = open(table).read().splitlines()\n"
        "    print(table, [r for r in rows if not r.startswith('Iface') and ' lo' not in r])\n"
        "for target in (('1.1.1.1', 53), ('host.docker.internal', 80), ('openrouter.ai', 443)):\n"
        "    try:\n"
        "        socket.create_connection(target, timeout=3)\n"
        "        print('CONNECTED', target)\n"
        "    except OSError as exc:\n"
        "        print('blocked', target, type(exc).__name__)\n"
    )
    result = box.python(code)
    assert result.status == "ok", result.content
    assert "CONNECTED" not in result.content
    assert result.content.count("blocked") == 3
    # Tunnel devices can appear in any namespace, but with no route they lead nowhere.
    assert "/proc/net/route []" in result.content
    assert "/proc/net/ipv6_route []" in result.content


def test_runs_as_a_non_root_user_with_no_capabilities(box):
    result = box.run("python", "-c", (
        "import os\n"
        "print('uid', os.getuid(), os.getgid())\n"
        "status = open('/proc/self/status').read()\n"
        "print([l for l in status.splitlines() if l.startswith(('CapEff', 'CapBnd', 'NoNewPrivs'))])\n"
    ))
    assert "uid 1000 1000" in result.content
    assert "CapEff:\\t0000000000000000" in result.content
    assert "CapBnd:\\t0000000000000000" in result.content
    assert "NoNewPrivs:\\t1" in result.content


def test_root_filesystem_is_read_only_and_only_workspace_and_tmp_are_writable(box):
    code = (
        "import os\n"
        "for path in ('/etc/x', '/usr/x', '/x', '/opt/sandbox/x', '/inputs/model/x', '/inputs/data/x',\n"
        "             '/workspace/x', '/tmp/x'):\n"
        "    try:\n"
        "        open(path, 'w').close()\n"
        "        print('writable', path)\n"
        "    except OSError as exc:\n"
        "        print('refused', path)\n"
    )
    out = box.python(code).content
    for path in ("/etc/x", "/usr/x", "/x", "/opt/sandbox/x", "/inputs/model/x", "/inputs/data/x"):
        assert f"refused {path}" in out
    assert "writable /workspace/x" in out and "writable /tmp/x" in out


def test_inputs_are_readable_and_tmp_cannot_execute(box):
    assert "legacy" in box.run("python", "/inputs/model/score.py").content
    assert "a,b" in box.run("head", "/inputs/data/dev.csv").content
    box.run("cp", "/usr/bin/env", "/tmp/copied")
    assert box.run("/tmp/copied").status == "error"


def test_no_credentials_or_docker_socket_inside(box, monkeypatch):
    code = (
        "import os, json\n"
        "print(json.dumps(sorted(os.environ)))\n"
        "print('socket', os.path.exists('/var/run/docker.sock'), os.path.exists('/run/docker.sock'))\n"
    )
    out = box.python(code).content
    assert "socket False False" in out
    assert "OPENROUTER" not in out and "API_KEY" not in out and "TOKEN" not in out


def test_resource_limits_are_applied(box):
    inspect = json.loads(_docker("inspect", box.sandbox.container).stdout)[0]["HostConfig"]
    assert inspect["NetworkMode"] == "none"
    assert inspect["ReadonlyRootfs"] is True
    assert inspect["CapDrop"] == ["ALL"]
    assert "no-new-privileges" in inspect["SecurityOpt"]
    assert inspect["PidsLimit"] == 256
    assert inspect["Memory"] == 4 * 1024**3
    assert inspect["NanoCpus"] == 2 * 10**9
    assert inspect["Privileged"] is False
    assert not inspect["Binds"]


def test_fork_bomb_is_stopped_by_the_pid_limit(box):
    code = (
        "import os, time\n"
        "started = 0\n"
        "try:\n"
        "    for _ in range(2000):\n"
        "        if os.fork() == 0:\n"
        "            time.sleep(20)\n"
        "            os._exit(0)\n"
        "        started += 1\n"
        "except OSError:\n"
        "    print('stopped after', started)\n"
    )
    result = box.python(code, timeout_s=15)
    assert "stopped after" in result.content, result.content[:500]


# ---------------------------------------------------------------- file tools
def test_write_append_read_and_list(box):
    assert box.call("sandbox_write_file", path="src/a.txt", content="one\n").status == "ok"
    assert box.call("sandbox_write_file", path="src/a.txt", content="two\n", append=True).status == "ok"
    assert box.call("sandbox_read_file", path="src/a.txt").content == "one\ntwo\n"
    listing = box.call("sandbox_list_files").content
    assert "d" in listing and "src/a.txt" in listing
    assert box.call("sandbox_write_file", path="src/a.txt", content="over\n").status == "ok"
    assert box.call("sandbox_read_file", path="src/a.txt").content == "over\n"


def test_unicode_content_survives(box):
    text = "模型文档 — naïve ✓\n"
    assert box.call("sandbox_write_file", path="u.md", content=text).status == "ok"
    assert box.call("sandbox_read_file", path="u.md").content == text


def test_real_symlink_escapes_are_rejected(box):
    box.run("ln", "-s", "/etc/passwd", "leak")
    box.run("ln", "-s", "/inputs/model", "model_link")
    box.run("ln", "-s", "..", "up")
    assert box.call("sandbox_read_file", path="leak").status == "denied"
    assert box.call("sandbox_write_file", path="model_link/x", content="x").status == "denied"
    assert box.call("sandbox_list_files", path="up").status == "denied"
    assert box.call("sandbox_read_file", path="../etc/passwd").status == "denied"


def test_missing_file_is_an_error_not_a_crash(box):
    result = box.call("sandbox_read_file", path="nope.txt")
    assert result.status == "error" and "nope.txt" in result.content


def test_file_names_that_look_like_flags_are_safe(box):
    assert box.call("sandbox_write_file", path="-rf", content="safe").status == "ok"
    assert box.call("sandbox_read_file", path="-rf").content == "safe"


# ---------------------------------------------------------------- commands
def test_exec_has_no_shell(box):
    result = box.run("echo", "$(whoami); ls | cat")
    assert result.status == "ok" and "$(whoami); ls | cat" in result.content


def test_exec_reports_exit_codes_and_missing_commands(box):
    assert "exit code 3" in box.python("import sys; sys.exit(3)").content
    assert box.run("no-such-command").status == "error"


def test_timeout_kills_the_process_inside_the_container(box):
    result = box.run("python", "-c", "import time; time.sleep(120)", timeout_s=1)
    assert result.status == "error" and "timed out" in result.content
    processes = box.run("python", "-c", (
        "import os\n"
        "for pid in filter(str.isdigit, os.listdir('/proc')):\n"
        "    print(open(f'/proc/{pid}/cmdline').read().replace(chr(0), ' '))\n"
    )).content
    assert "time.sleep(120)" not in processes


def test_cancel_kills_the_container(box):
    async def main():
        running = asyncio.create_task(
            box.sandbox.call("sandbox_exec", {"argv": ["python", "-c", "import time; time.sleep(120)"]})
        )
        await asyncio.sleep(1.5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(main())
    state = json.loads(_docker("inspect", box.sandbox.container).stdout)[0]["State"]
    assert state["Running"] is False


def test_huge_output_is_truncated(box):
    result = box.python("import sys; sys.stdout.write('x' * 5_000_000)", timeout_s=30)
    assert "[Truncated" in result.content and len(result.content) < 45_000


# ---------------------------------------------------------------- lifecycle
def test_workspace_survives_for_the_next_task_and_the_container_does_not(inputs):
    workspace = uuid4().hex
    first = Box(inputs, workspace)
    try:
        with first as running:
            running.call("sandbox_write_file", path="kept.txt", content="still here")
            container = running.sandbox.container
        assert _docker("inspect", container).returncode != 0
        with Box(inputs, workspace) as second:
            assert second.call("sandbox_read_file", path="kept.txt").content == "still here"
    finally:
        _docker("volume", "rm", "--force", first.sandbox.volume)


def test_missing_image_is_sandbox_unavailable(inputs):
    box = Box(inputs, config=SandboxConfig(image="merry-go-round/does-not-exist:0"))
    try:
        with pytest.raises(SandboxUnavailable):
            asyncio.run(box.sandbox.start())
    finally:
        _docker("volume", "rm", "--force", box.sandbox.volume)


# ---------------------------------------------------------------- dependencies
def test_dependencies_work_under_the_runtime_restrictions(box):
    # The image build checks this too, but with a writable root and no isolation flags.
    result = box.run("python", "/opt/sandbox/verify_image.py", timeout_s=120)
    assert result.status == "ok", result.content
    for name in ("readxl", "digest", "jsonlite", "renv"):
        assert f"R {name}" in result.content


def test_models_can_really_fit_under_the_runtime_restrictions(box):
    code = (
        "import numpy as np, pandas as pd, joblib, lightgbm, xgboost, statsmodels.api as sm\n"
        "from sklearn.linear_model import LogisticRegression\n"
        "from docx import Document\n"
        "rng = np.random.default_rng(0)\n"
        "X = rng.normal(size=(2000, 5)); y = (X[:, 0] + rng.normal(size=2000) > 0).astype(int)\n"
        "for model in (LogisticRegression(), xgboost.XGBClassifier(n_estimators=5),\n"
        "              lightgbm.LGBMClassifier(n_estimators=5, verbose=-1)):\n"
        "    model.fit(X, y); joblib.dump(model, 'm.joblib'); joblib.load('m.joblib').predict_proba(X)\n"
        "    print('fit', type(model).__name__)\n"
        "print('glm', sm.Logit(y, sm.add_constant(X)).fit(disp=0).converged)\n"
        "Document().save('d.docx'); print('docx ok')\n"
    )
    result = box.python(code, timeout_s=120)
    assert result.status == "ok", result.content
    assert result.content.count("fit ") == 3 and "glm True" in result.content
    assert "docx ok" in result.content


def test_r_can_fit_and_read_excel_under_the_runtime_restrictions(box):
    code = (
        "set.seed(1); x<-rnorm(500); y<-as.integer(x+rnorm(500)>0)\n"
        "fit<-glm.fit(cbind(1,x),y,family=binomial()); stopifnot(fit$converged)\n"
        "saveRDS(fit,'m.rds'); stopifnot(!is.null(readRDS('m.rds')$coefficients))\n"
        "cat(digest::digest(file='m.rds',algo='sha256') != '', jsonlite::toJSON(1), '\\n')\n"
        "stopifnot(is.function(readxl::read_excel)); cat('r ok\\n')\n"
    )
    assert box.call("sandbox_write_file", path="t.R", content=code).status == "ok"
    result = box.run("Rscript", "t.R", timeout_s=120)
    assert result.status == "ok" and "r ok" in result.content, result.content


# ---------------------------------------------------------------- the scoring service
def test_the_scoring_service_scores_in_a_fresh_container(tmp_path):
    from harness.scoring import ScoringJob, ScoringService

    artifact = tmp_path / "candidate"
    artifact.mkdir()
    (artifact / "score.py").write_text(
        "import argparse, csv, socket\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--input'); p.add_argument('--output')\n"
        "a = p.parse_args()\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=2); raise SystemExit('network')\n"
        "except OSError:\n"
        "    pass\n"
        "rows = list(csv.DictReader(open(a.input)))\n"
        "with open(a.output, 'w', newline='') as f:\n"
        "    w = csv.writer(f); w.writerow(['row_id', 'positive_probability'])\n"
        "    for r in rows: w.writerow([r['row_id'], repr(int(r['age']) / 100)])\n"
    )
    data = tmp_path / "dev.csv"
    data.write_text("row_id,age\nr1,30\nr2,40\n")
    service = ScoringService(work_dir=str(tmp_path))
    run = asyncio.run(
        service.score(ScoringJob(artifact_path=str(artifact), input_path=str(data), timeout_s=60))
    )
    assert run.status == "ok", run.error
    assert run.predictions.probabilities == [0.3, 0.4]
    # The artifact was read-only: a scorer that writes into it fails.
    (artifact / "score.py").write_text("open('/artifact/x', 'w')\n")
    broken = asyncio.run(
        service.score(ScoringJob(artifact_path=str(artifact), input_path=str(data), timeout_s=60))
    )
    assert broken.status == "not_executable"


LEGACY = Path(__file__).resolve().parents[3] / "legacy_models"


def _copy_package(destination, *names):
    """A bundle that keeps the archive's relative paths, as the backend will build it."""
    for name in names:
        shutil.copytree(LEGACY / name, destination / name)
    for folder in [destination, *destination.rglob("*")]:
        if folder.is_dir():
            folder.chmod(0o755)
    return destination


def _score(service, **job):
    from harness.scoring import ScoringJob

    return ScoringJob(timeout_s=job.pop("timeout_s", 120), **job)


@pytest.mark.skipif(not LEGACY.is_dir(), reason="legacy_models is not checked out")
def test_reproduction_check_reproduces_the_real_r_model(tmp_path):
    from harness.scoring import ScoringService

    bundle = _copy_package(tmp_path / "bundle", "credit_logistic_r")
    package = bundle / "credit_logistic_r"
    service = ScoringService(work_dir=str(tmp_path))
    check = asyncio.run(
        service.reproduction_check(
            _score(
                service,
                artifact_path=str(bundle),
                entry_point="credit_logistic_r/score.R",
                input_path=str(package / "fixtures" / "development_inputs.csv"),
            ),
            archive_reference_path=str(package / "fixtures" / "reference_predictions.csv"),
        )
    )
    assert check.status == "PASS", (check.reason, check.run.error)
    assert check.comparison.rows == 100 and check.comparison.max_abs_diff <= 1e-8
    assert len(check.reference.row_ids) == 100


@pytest.mark.skipif(not LEGACY.is_dir(), reason="legacy_models is not checked out")
def test_reproduction_and_parity_with_the_real_python_model(tmp_path):
    from harness.scoring import ScoringService

    bundle = _copy_package(tmp_path / "bundle", "credit_logistic_python", "shared")
    fixtures = bundle / "credit_logistic_python" / "fixtures"
    service = ScoringService(work_dir=str(tmp_path))
    job = _score(
        service,
        artifact_path=str(bundle),
        entry_point="credit_logistic_python/score.py",
        input_path=str(fixtures / "development_inputs.csv"),
    )
    check = asyncio.run(
        service.reproduction_check(
            job, archive_reference_path=str(fixtures / "reference_predictions.csv")
        )
    )
    assert check.status == "PASS", (check.reason, check.run.error)

    # A rebuild that reproduces the reference passes parity, and a slightly wrong one fails.
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    shutil.copy(fixtures / "reference_predictions.csv", candidate / "reference.csv")
    (candidate / "score.py").write_text(
        "import argparse, csv\n"
        "p = argparse.ArgumentParser(); p.add_argument('--input'); p.add_argument('--output')\n"
        "a = p.parse_args()\n"
        "shift = float(open('shift.txt').read()) if __import__('os').path.exists('shift.txt') else 0.0\n"
        "ref = {r['row_id']: float(r['positive_probability']) for r in csv.DictReader(open('reference.csv'))}\n"
        "with open(a.output, 'w', newline='') as f:\n"
        "    w = csv.writer(f); w.writerow(['row_id', 'positive_probability'])\n"
        "    for r in csv.DictReader(open(a.input)):\n"
        "        w.writerow([r['row_id'], repr(min(ref[r['row_id']] + shift, 1.0))])\n"
    )
    candidate.chmod(0o755)
    candidate_job = _score(
        service, artifact_path=str(candidate), input_path=str(fixtures / "development_inputs.csv")
    )
    parity = asyncio.run(service.compare_with_reference(candidate_job, check.reference))
    assert parity.status == "PASS" and parity.max_abs_diff <= 1e-8, parity
    (candidate / "shift.txt").write_text("1e-6")
    parity = asyncio.run(service.compare_with_reference(candidate_job, check.reference))
    assert parity.status == "FAIL" and parity.mismatched_rows > 0


@pytest.mark.skipif(not LEGACY.is_dir(), reason="legacy_models is not checked out")
def test_an_agent_written_wrapper_gets_the_bundle_as_its_model_dir(tmp_path):
    from harness.scoring import ScoringService

    bundle = _copy_package(tmp_path / "bundle", "credit_logistic_r")
    (bundle / "scoring_wrapper").mkdir(mode=0o755)
    (bundle / "scoring_wrapper" / "score.py").write_text(
        "import argparse, subprocess\n"
        "p = argparse.ArgumentParser()\n"
        "for flag in ('--input', '--output', '--model-dir'): p.add_argument(flag)\n"
        "a = p.parse_args()\n"
        "pkg = a.model_dir + '/credit_logistic_r'\n"
        "subprocess.run(['Rscript', pkg + '/score.R', '--package', pkg,\n"
        "                '--input', a.input, '--output', a.output], check=True)\n"
    )
    fixtures = bundle / "credit_logistic_r" / "fixtures"
    service = ScoringService(work_dir=str(tmp_path))
    check = asyncio.run(
        service.reproduction_check(
            _score(
                service,
                artifact_path=str(bundle),
                entry_point="scoring_wrapper/score.py",
                input_path=str(fixtures / "development_inputs.csv"),
                pass_model_dir=True,
            ),
            archive_reference_path=str(fixtures / "reference_predictions.csv"),
        )
    )
    assert check.status == "PASS", (check.reason, check.run.error)


def test_a_scorer_that_runs_too_long_is_killed_and_its_container_removed(tmp_path):
    from harness.scoring import ScoringService

    artifact = tmp_path / "slow"
    artifact.mkdir()
    (artifact / "score.py").write_text("import time\ntime.sleep(120)\n")
    data = tmp_path / "in.csv"
    data.write_text("row_id\nr1\n")
    service = ScoringService(work_dir=str(tmp_path))
    started = time.monotonic()
    run = asyncio.run(
        service.score(_score(service, artifact_path=str(artifact), input_path=str(data), timeout_s=3))
    )
    assert run.status == "not_executable" and "timed out" in run.error
    assert time.monotonic() - started < 60
    left = _docker("ps", "-a", "--filter", "name=mgr-score-", "--format", "{{.Names}}").stdout
    assert left.strip() == ""


def test_protected_data_never_leaves_through_the_scorers_output(tmp_path):
    from harness.scoring import PROTECTED_ERROR_TEXT, ScoringService

    artifact = tmp_path / "leaky"
    artifact.mkdir()
    (artifact / "score.py").write_text(
        "import sys\nprint(open(sys.argv[2]).read(), file=sys.stderr)\nsys.exit(3)\n"
    )
    data = tmp_path / "final_test.csv"
    data.write_text("row_id,secret\nr1,TOP-SECRET-ROW\n")
    service = ScoringService(work_dir=str(tmp_path))
    open_run = asyncio.run(
        service.score(_score(service, artifact_path=str(artifact), input_path=str(data)))
    )
    assert "TOP-SECRET-ROW" in open_run.error  # proves the scorer really printed it
    protected = asyncio.run(
        service.score(
            _score(service, artifact_path=str(artifact), input_path=str(data), protected=True)
        )
    )
    assert protected.error == PROTECTED_ERROR_TEXT


# ---------------------------------------------------------------- inputs of the new kinds
def _sandbox_for(kind, mounts):
    task_id = uuid4().hex
    info = TaskInfo(
        task_id=task_id,
        thread_id=task_id,
        workspace_id=uuid4().hex,
        kind=kind,
        actor_id="alice",
        actor_role="validator",
        policy_version="p1",
    )
    return DockerSandbox(info, mounts, SandboxConfig(), AsyncioProcessRunner())


def _folder(path, files):
    path.mkdir()
    for name, text in files.items():
        (path / name).write_text(text)
    path.chmod(0o755)
    return str(path)


def test_validation_sees_its_four_inputs_read_only(tmp_path):
    mounts = SandboxMounts(
        model_package_path=_folder(tmp_path / "bundle", {"model.rds": "x"}),
        document_path=_folder(tmp_path / "doc", {"model_document.md": "# Doc"}),
        metrics_path=_folder(tmp_path / "metrics", {"metrics_bundle.json": "{}"}),
        development_data_path=_folder(tmp_path / "dev", {"dev.csv": "a\n1\n"}),
    )
    sandbox = _sandbox_for("validation", mounts)

    async def main():
        await sandbox.start()
        try:
            listing = await sandbox.call("sandbox_exec", {"argv": ["ls", "/inputs"]})
            metrics = await sandbox.call(
                "sandbox_exec", {"argv": ["cat", "/inputs/metrics/metrics_bundle.json"]}
            )
            write = await sandbox.call(
                "sandbox_exec", {"argv": ["touch", "/inputs/metrics/forged.json"]}
            )
            return listing, metrics, write
        finally:
            await sandbox.destroy()
            _docker("volume", "rm", "--force", sandbox.volume)

    listing, metrics, write = asyncio.run(main())
    assert listing.content.split("\n")[2:6] == ["data", "document", "metrics", "model"]
    assert "{}" in metrics.content
    assert write.status == "error" and "Read-only" in write.content


def test_survey_sees_only_the_archive(tmp_path):
    mounts = SandboxMounts(archive_path=_folder(tmp_path / "archive", {"model_final.R": "1"}))
    sandbox = _sandbox_for("survey", mounts)

    async def main():
        await sandbox.start()
        try:
            return await sandbox.call("sandbox_exec", {"argv": ["find", "/inputs", "-type", "f"]})
        finally:
            await sandbox.destroy()
            _docker("volume", "rm", "--force", sandbox.volume)

    result = asyncio.run(main())
    assert result.content.split("\n")[2] == "/inputs/archive/model_final.R"
    assert "/inputs/data" not in result.content
