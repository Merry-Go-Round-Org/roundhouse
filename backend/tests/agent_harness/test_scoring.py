"""The scoring service, with a fake process runner standing in for Docker."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harness.sandbox import ProcessResult, SandboxSlots
from harness.scoring import (
    PROTECTED_ERROR_TEXT,
    Check,
    MetricsBundle,
    Predictions,
    ScoringJob,
    ScoringService,
    compare_predictions,
    read_predictions,
    sha256_path,
)
from harness.validation_report import unsupported_numbers

ROWS = ["r1", "r2", "r3"]


class FakeDocker:
    """Answers `docker run` by writing `predictions` into the mounted output folder."""

    def __init__(self, predictions=None, *, returncode=0, stderr="", timed_out=False, hold=None):
        self.predictions = predictions
        self.returncode = returncode
        self.stderr = stderr
        self.timed_out = timed_out
        self.hold = hold
        self.commands = []
        self.running = 0
        self.peak = 0

    async def run(self, argv, *, timeout_s, max_output, stdin=None):
        self.commands.append(argv)
        if argv[1] != "run":
            return ProcessResult(returncode=0)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if self.hold is not None:
                await self.hold.wait()
            output = next(
                a.split("src=")[1].split(",")[0]
                for a in argv
                if a.startswith("type=bind") and a.endswith("dst=/output")
            )
            if self.predictions is not None:
                Path(output, "predictions.csv").write_text(self.predictions)
            return ProcessResult(
                returncode=None if self.timed_out else self.returncode,
                stderr=self.stderr,
                timed_out=self.timed_out,
            )
        finally:
            self.running -= 1


def csv_text(rows=ROWS, probs=(0.1, 0.5, 0.9)):
    return "row_id,positive_probability\n" + "".join(f"{r},{p!r}\n" for r, p in zip(rows, probs))


@pytest.fixture
def files(tmp_path):
    artifact = tmp_path / "candidate"
    artifact.mkdir()
    (artifact / "score.py").write_text("print('scoring')\n")
    (artifact / "params.json").write_text("{}")
    data = tmp_path / "dev.csv"
    data.write_text("row_id,age,default\nr1,30,0\nr2,40,1\nr3,50,0\n")
    return artifact, data


def job(files, **changes):
    artifact, data = files
    return ScoringJob(artifact_path=str(artifact), input_path=str(data), **changes)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- the container
def test_the_scoring_container_is_isolated_and_mounts_inputs_read_only(files):
    service = ScoringService(runner=FakeDocker(csv_text()))
    command = service.run_command("mgr-score-x", job(files, pass_model_dir=True), "/tmp/out")
    joined = " ".join(command)
    for flag in ("--network none", "--cap-drop ALL", "--read-only", "no-new-privileges", "--rm"):
        assert flag in joined
    artifact, data = files
    assert f"type=bind,src={artifact},dst=/artifact,readonly" in joined
    assert f"type=bind,src={data},dst=/input/input.csv,readonly" in joined
    assert "type=bind,src=/tmp/out,dst=/output" in joined
    assert command[command.index("python") :] == [
        "python", "/artifact/score.py",
        "--input", "/input/input.csv",
        "--output", "/output/predictions.csv",
        "--model-dir", "/artifact",
    ]  # fmt: skip
    assert "--env" not in command


def test_an_r_entry_point_runs_with_rscript(files):
    artifact, _ = files
    (artifact / "score.R").write_text("cat('x')\n")
    command = ScoringService().run_command("n", job(files, entry_point="score.R"), "/tmp/out")
    assert "Rscript" in command and "/artifact/score.R" in command


def test_the_scorer_runs_from_its_own_folder(files):
    # Legacy R scorers find model.rds from the working directory.
    service = ScoringService()
    nested = service.run_command("n", job(files, entry_point="pkg/score.R"), "/tmp/out")
    assert nested[nested.index("--workdir") + 1] == "/artifact/pkg"
    root = service.run_command("n", job(files), "/tmp/out")
    assert root[root.index("--workdir") + 1] == "/artifact"


def test_entry_points_must_stay_inside_the_artifact(files):
    for bad in ("../score.py", "/etc/score.py", "score.sh", ""):
        with pytest.raises(ValueError):
            job(files, entry_point=bad)


# ---------------------------------------------------------------- one run
def test_a_good_run_returns_checked_predictions_and_hashes(files):
    result = run(ScoringService(runner=FakeDocker(csv_text())).score(job(files)))
    assert result.status == "ok"
    assert result.predictions.row_ids == ROWS
    assert result.predictions.probabilities == [0.1, 0.5, 0.9]
    assert result.artifact_sha256 == sha256_path(files[0])
    assert result.input_sha256 == sha256_path(files[1])


def test_a_failing_scorer_is_not_executable_with_its_error(files):
    docker = FakeDocker(returncode=1, stderr="there is no package called 'glmnet'")
    result = run(ScoringService(runner=docker).score(job(files)))
    assert result.status == "not_executable" and "glmnet" in result.error


def test_protected_data_never_leaks_through_the_error(files):
    docker = FakeDocker(returncode=1, stderr="row r2 age=40 default=1")
    result = run(ScoringService(runner=docker).score(job(files, protected=True)))
    assert result.error == PROTECTED_ERROR_TEXT
    bad = FakeDocker("row_id,positive_probability\nr2,secret-row-value\n")
    result = run(ScoringService(runner=bad).score(job(files, protected=True)))
    assert result.status == "invalid_output" and "secret" not in result.error


def test_a_timeout_kills_the_container(files):
    docker = FakeDocker(timed_out=True)
    result = run(ScoringService(runner=docker).score(job(files, timeout_s=5)))
    assert result.status == "not_executable" and "timed out" in result.error
    assert any(c[1] == "kill" for c in docker.commands)


@pytest.mark.parametrize(
    "output, reason",
    [
        (None, "no predictions file"),
        ("id,p\nr1,0.1\n", "header"),
        (csv_text(probs=(0.1, 1.5, 0.9)), "outside 0 to 1"),
        (csv_text(probs=(0.1, float("nan"), 0.9)), "outside 0 to 1"),
        (csv_text(rows=["r1", "r2"], probs=(0.1, 0.2)), "2 predictions for 3 input rows"),
        (csv_text(rows=["r1", "r3", "r2"]), "do not match"),
        (csv_text(rows=["r1", "r1", "r2"]), "repeated"),
    ],
)
def test_bad_output_is_invalid(files, output, reason):
    result = run(ScoringService(runner=FakeDocker(output)).score(job(files)))
    assert result.status == "invalid_output" and reason in result.error


def test_a_missing_entry_point_is_refused_before_docker(files):
    from harness.schemas import SandboxUnavailable

    docker = FakeDocker(csv_text())
    with pytest.raises(SandboxUnavailable):
        run(ScoringService(runner=docker).score(job(files, entry_point="missing.py")))
    assert docker.commands == []


def test_scoring_waits_for_a_sandbox_slot(files):
    hold = asyncio.Event()
    docker = FakeDocker(csv_text(), hold=hold)
    service = ScoringService(runner=docker, slots=SandboxSlots(1))

    async def main():
        runs = [asyncio.create_task(service.score(job(files))) for _ in range(3)]
        await asyncio.sleep(0.05)
        assert docker.running == 1
        hold.set()
        return await asyncio.gather(*runs)

    results = run(main())
    assert all(r.status == "ok" for r in results) and docker.peak == 1


# ---------------------------------------------------------------- comparisons
def preds(probs, rows=ROWS):
    return Predictions(row_ids=list(rows), probabilities=list(probs), sha256="x")


def test_parity_passes_within_tolerance_and_names_the_worst_rows():
    assert compare_predictions(preds([0.1, 0.5, 0.9]), preds([0.1, 0.5, 0.9 + 1e-10]), 1e-8).status == "PASS"
    result = compare_predictions(preds([0.1, 0.6, 0.9]), preds([0.1, 0.5, 0.9]), 1e-8)
    assert result.status == "FAIL" and result.mismatched_rows == 1
    assert result.worst_row_ids == ["r2"] and result.max_abs_diff == pytest.approx(0.1)


def test_parity_fails_on_different_rows_before_comparing_values():
    short = compare_predictions(preds([0.1], ["r1"]), preds([0.1, 0.2], ["r1", "r2"]), 1e-8)
    assert short.status == "FAIL" and "rows against" in short.reason
    with pytest.raises(ValueError):
        preds([0.1])
    swapped = compare_predictions(preds([0.1, 0.5, 0.9], ["r1", "r3", "r2"]), preds([0.1, 0.5, 0.9]), 1e-8)
    assert swapped.status == "FAIL" and "position 1" in swapped.reason


def test_reproduction_check_statuses(files, tmp_path):
    service = ScoringService(runner=FakeDocker(csv_text()))
    check = run(service.reproduction_check(job(files)))
    assert check.status == "PASS" and check.reference.row_ids == ROWS

    archived = tmp_path / "archived.csv"
    archived.write_text(csv_text())
    check = run(service.reproduction_check(job(files), archive_reference_path=str(archived)))
    assert check.status == "PASS" and check.comparison.max_abs_diff == 0.0

    archived.write_text(csv_text(probs=(0.1, 0.5, 0.8)))
    check = run(service.reproduction_check(job(files), archive_reference_path=str(archived)))
    assert check.status == "FAIL" and check.reference is None

    archived.write_text("garbage")
    check = run(service.reproduction_check(job(files), archive_reference_path=str(archived)))
    assert check.status == "REVIEW" and check.reference is not None

    broken = ScoringService(runner=FakeDocker(returncode=1, stderr="SAS is not installed"))
    check = run(broken.reproduction_check(job(files)))
    assert check.status == "NOT_ASSESSABLE" and "not executable" in check.reason


def test_comparison_with_reference_is_never_a_pass_without_a_reference(files):
    service = ScoringService(runner=FakeDocker(csv_text()))
    assert run(service.compare_with_reference(job(files), None)).status == "NOT_ASSESSABLE"
    assert run(service.compare_with_reference(job(files), preds([0.1, 0.5, 0.9]))).status == "PASS"
    failing = ScoringService(runner=FakeDocker(returncode=2, stderr="boom"))
    assert run(failing.compare_with_reference(job(files), preds([0.1, 0.5, 0.9]))).status == "FAIL"


# ---------------------------------------------------------------- metrics bundle
class FakeSuite:
    def __init__(self):
        self.calls = []

    def evaluate(self, partition, input_path, model, challenger):
        self.calls.append((partition, challenger is not None))
        checks = [Check(check_id=f"disc.auc.{partition}", status="PASS", section=6,
                        partition=partition, values={"auc": 0.7213}, interval=(0.70, 0.745))]
        if challenger is not None:
            checks.append(Check(check_id=f"challenger.auc.{partition}", status="REVIEW", section=6,
                                partition=partition, values={"auc": 0.7442}))
        return checks


def test_the_metrics_bundle_holds_aggregates_from_every_partition(files, tmp_path):
    suite = FakeSuite()
    service = ScoringService(runner=FakeDocker(csv_text()))
    bundle = run(
        service.metrics_bundle(
            {"validation": job(files), "final_test": job(files, protected=True)},
            suite,
            policy_version="policy-3",
            challengers={"final_test": job(files, protected=True)},
        )
    )
    ids = [c.check_id for c in bundle.checks]
    assert ids == [
        "implementation.scoring.validation",
        "disc.auc.validation",
        "implementation.scoring.final_test",
        "challenger.scoring.final_test",
        "disc.auc.final_test",
        "challenger.auc.final_test",
    ]
    assert suite.calls == [("validation", False), ("final_test", True)]
    assert set(bundle.data_sha256) == {"validation", "final_test"}
    path = tmp_path / "metrics_bundle.json"
    bundle.write(path)
    loaded = MetricsBundle.model_validate_json(path.read_text())
    # The validator may quote what the bundle holds, and nothing else.
    assert unsupported_numbers("AUC 0.721, challenger 0.744, on 3 rows", loaded) == []
    assert unsupported_numbers("AUC 0.80", loaded) == ["0.80"]


def test_a_partition_that_does_not_score_is_visible_as_not_assessable(files):
    suite = FakeSuite()
    service = ScoringService(runner=FakeDocker(returncode=1, stderr="x"))
    bundle = run(service.metrics_bundle({"final_test": job(files, protected=True)}, suite, policy_version="p"))
    assert [(c.check_id, c.status) for c in bundle.checks] == [
        ("implementation.scoring.final_test", "NOT_ASSESSABLE")
    ]
    assert bundle.checks[0].note == PROTECTED_ERROR_TEXT and suite.calls == []


def test_checks_hold_finite_aggregates_only():
    with pytest.raises(ValueError):
        Check(check_id="x", status="PASS", values={"auc": float("inf")})
    with pytest.raises(ValueError):
        Check(check_id="x", status="PASS", values={"rows": [1, 2, 3]})
    with pytest.raises(ValueError):
        Check(check_id="x", status="PASS", row_level=[0.1])


def test_read_predictions_without_a_row_id_column_checks_the_count(tmp_path):
    data = tmp_path / "in.csv"
    data.write_text("age\n30\n40\n50\n")
    out = tmp_path / "out.csv"
    out.write_text(csv_text(rows=["0", "1", "2"]))
    assert read_predictions(out, data).row_ids == ["0", "1", "2"]
