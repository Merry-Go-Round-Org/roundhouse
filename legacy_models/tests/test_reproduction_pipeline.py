"""Fast tests for the isolated legacy reproduction CLI; no datasets or R needed."""
from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "reproduction" / "reproduce.py"
spec = importlib.util.spec_from_file_location("legacy_repro", SCRIPT)
repro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repro)


class LegacyReproductionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def fixture(path: Path, header: list[str], rows: list[list[str]]) -> None:
        with path.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)

    def fixtures(self):
        inputs = self.root / "released_inputs.csv"
        produced = self.root / "rebuilt_inputs.csv"
        reference = self.root / "released_scores.csv"
        scores = self.root / "refit_scores.csv"
        rows = [[f"val-{i:03d}", str(i * 0.25)] for i in range(100)]
        preds = [[f"val-{i:03d}", str(i / 101)] for i in range(100)]
        for path in (inputs, produced):
            self.fixture(path, ["row_id", "x"], rows)
        for path in (reference, scores):
            self.fixture(path, ["row_id", "positive_probability"], preds)
        return inputs, produced, reference, scores

    def test_eight_binary_models_do_not_conflate_cox_demo(self):
        self.assertEqual(len(repro.MODELS), 8)
        self.assertNotIn("credit_cox_python", repro.MODELS)
        self.assertEqual(repro.SEED, 20261104)
        self.assertEqual(repro.EXPECTED_PYTHON, "3.13.5")

    def test_data_sha_and_splits_are_pinned(self):
        self.assertEqual(len(repro.DATA["uci"][1]), 64)
        self.assertEqual(len(repro.DATA["ulb"][2]), 64)
        self.assertEqual(repro.DATA["ulb"][2],
                         "5d9edf4df20e866d3198e74893fd2e1cf75f5fc3c5b4e175efd5a4a478b827fa")
        self.assertEqual(repro.DATA["uci"][3]["test"], (5982, 1317))

    def test_average_precision_aliases_from_old_and_new_evaluators(self):
        self.assertEqual(
            repro.metric_value({"pr_auc_average_precision": 0.52}, "pr_auc_average_precision"),
            0.52)
        self.assertEqual(
            repro.metric_value({"pr_auc_average_precision_sklearn": 0.52},
                               "pr_auc_average_precision"), 0.52)
        with self.assertRaisesRegex(ValueError, "Missing or nonfinite"):
            repro.metric_value({"pr_auc_average_precision": float("nan")},
                               "pr_auc_average_precision")

    def test_reproduction_matches_exact_reference(self):
        result = repro.compare_development(*self.fixtures(), tolerance=1e-8)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["max_abs_prediction_difference"], 0)
        self.assertEqual(result["rows"], 100)

    def test_prediction_divergence_is_not_hidden(self):
        i, p, ref, scores = self.fixtures()
        rows = repro.fixture_rows(scores)
        rows[13]["positive_probability"] = "0.9"
        self.fixture(scores, ["row_id", "positive_probability"],
                     [[r["row_id"], r["positive_probability"]] for r in rows])
        outcome = repro.compare_development(i, p, ref, scores, 1e-8)
        self.assertEqual(outcome["status"], "DIVERGED")
        self.assertEqual(outcome["n_above_tolerance"], 1)

    def test_reordered_ids_are_rejected_even_when_probabilities_match(self):
        i, p, ref, scores = self.fixtures()
        rows = repro.fixture_rows(scores)
        rows[0], rows[1] = rows[1], rows[0]
        self.fixture(scores, ["row_id", "positive_probability"],
                     [[r["row_id"], r["positive_probability"]] for r in rows])
        with self.assertRaisesRegex(ValueError, "IDs/order"):
            repro.compare_development(i, p, ref, scores, 1e-8)

    def test_input_feature_drift_rejected(self):
        i, p, ref, scores = self.fixtures()
        rows = repro.fixture_rows(p)
        rows[0]["x"] = "1000"
        self.fixture(p, ["row_id", "x"],
                     [[r["row_id"], r["x"]] for r in rows])
        with self.assertRaisesRegex(ValueError, "feature values differ"):
            repro.compare_development(i, p, ref, scores, 1e-8)

    def test_wrong_sha_never_triggers_download(self):
        path = self.root / "uci/default of credit card clients.xls"
        path.parent.mkdir()
        path.write_text("tampered")
        with patch.object(repro, "checked_run", side_effect=AssertionError("network called")):
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                repro.source_paths(self.root, download_missing=False)

    def test_checked_run_reports_subprocess_errors(self):
        output = repro.checked_run([sys.executable, "-c", "print('done')"])
        self.assertEqual(output.strip(), "done")
        with self.assertRaisesRegex(RuntimeError, "exit 7"):
            repro.checked_run([sys.executable, "-c", "import sys; sys.exit(7)"])

    def test_heartbeat_is_visible_during_long_subprocess(self):
        from unittest.mock import MagicMock

        child = MagicMock()
        child.__enter__.return_value = child
        child.pid = 345
        child.returncode = 0
        child.communicate.side_effect = [
            subprocess.TimeoutExpired("Rscript", 30),
            ("finished", ""),
        ]
        with patch.object(repro.subprocess, "Popen", return_value=child):
            with patch("builtins.print") as printed:
                self.assertEqual(repro.checked_run(["Rscript", "train.R"]), "finished")
        printed.assert_called_once()
        self.assertIn("still running", printed.call_args.args[0])

    def test_missing_git_is_nonfatal_traceability_metadata(self):
        with patch.object(repro, "checked_run", side_effect=FileNotFoundError("git")):
            self.assertIsNone(repro.optional_source_commit())
        with patch.object(repro, "checked_run", side_effect=RuntimeError("not a checkout")):
            self.assertIsNone(repro.optional_source_commit())
        with patch.object(repro, "checked_run", return_value="abc123\n"):
            self.assertEqual(repro.optional_source_commit(), "abc123")

    def test_deterministic_env_is_set_per_subprocess(self):
        env = repro.deterministic_env()
        self.assertEqual(env["PYTHONHASHSEED"], "0")
        self.assertEqual(env["OPENBLAS_NUM_THREADS"], "1")
        self.assertEqual(env["TZ"], "UTC")

    def test_run_never_uses_released_directory_as_package(self):
        name = "credit_logistic_python"
        with self.assertRaises((TypeError, FileNotFoundError, ValueError)):
            # A non-existent release manifest must fail before any training:
            with patch.object(repro, "ROOT", self.root), patch.object(
                    repro, "checked_run", side_effect=AssertionError("would train")):
                repro.train_one(name, {"uci": self.root / "nonexistent"},
                                self.root / "scratch", {}, None)


if __name__ == "__main__":
    unittest.main()
