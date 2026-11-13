"""Standard-library tests for frozen-bin PSI, validator and monitoring parity."""
import csv
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from legacy_models import stability as psi

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "psi_monitor_adapter", ROOT / "legacy_models/integration/monitor_psi.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)
spec = importlib.util.spec_from_file_location("psi_validator", ROOT / "validate_all_models.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def write_csv(path, headers, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)


class FrozenPSITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = self.root / "model"
        self.model.mkdir()
        (self.model / "model.joblib").write_bytes(b"test artifact only - never deserialize")
        self.artifact_sha = hashlib.sha256(
            (self.model / "model.joblib").read_bytes()).hexdigest()
        self.manifest_path = self.model / "manifest.json"
        self.manifest_path.write_text(json.dumps({
            "name": "my_model", "task": "binary_classification", "language": "Python",
            "feature_order": ["x"], "artifact_sha256": self.artifact_sha,
        }))
        self.reference_inputs = self.root / "reference_inputs.csv"
        self.reference_scores = self.root / "reference_scores.csv"
        self.current_inputs = self.root / "inputs.csv"
        self.current_scores = self.root / "scores.csv"
        self.write_pair(self.reference_inputs, self.reference_scores, 240, 0.1)
        self.write_pair(self.current_inputs, self.current_scores, 120, 0.1)

    def write_pair(self, inputs, scores, count, value):
        write_csv(inputs, ["row_id", "x"],
                  [(f"r{i:06d}", value) for i in range(count)])
        write_csv(scores, ["row_id", "positive_probability"],
                  [(f"r{i:06d}", value) for i in range(count)])

    def baseline(self):
        return psi.freeze_baseline(
            model_id="my_model", feature_order=["x"],
            reference_inputs=self.reference_inputs,
            reference_scores=self.reference_scores,
            reference_scope="development", artifact_sha256=self.artifact_sha,
        )

    def test_identical_constant_no_drift(self):
        base = self.baseline()
        self.assertTrue(psi.validate_baseline(base))
        result = psi.evaluate_window(
            baseline=base, current_inputs=self.current_inputs,
            current_scores=self.current_scores, expected_model_id="my_model",
            expected_artifact_sha256=self.artifact_sha)
        self.assertEqual(result["band"], "green")
        self.assertFalse(result["revalidation_required"])
        # Additive 0.5 smoothing makes unequal-size constant cohorts have
        # a small nonzero PSI despite identical raw distributions.
        self.assertLess(result["score"]["psi"], 0.10)
        self.assertLess(result["features"]["x"]["psi"], 0.10)
        self.assertEqual(result["features"]["x"]["bin_definition"]["constant"], 0.1)
        self.assertEqual(len(result["features"]["x"]["bins"]), 3)

    def test_shifted_constant_sets_red_and_revalidation(self):
        self.write_pair(self.current_inputs, self.current_scores, 120, 0.9)
        result = psi.evaluate_window(
            baseline=self.baseline(), current_inputs=self.current_inputs,
            current_scores=self.current_scores)
        self.assertEqual(result["score_band"], "red")
        self.assertEqual(result["feature_band"], "red")
        self.assertEqual(result["red_features"], ["x"])
        self.assertTrue(result["revalidation_required"])
        self.assertEqual(result["status"], "REVIEW")

    def test_score_shift_only_does_not_invent_feature_shift(self):
        write_csv(self.current_scores, ["row_id", "positive_probability"],
                  [(f"r{i:06d}", 0.9) for i in range(120)])
        result = psi.evaluate_window(
            baseline=self.baseline(), current_inputs=self.current_inputs,
            current_scores=self.current_scores)
        self.assertEqual(result["score_band"], "red")
        self.assertEqual(result["feature_band"], "green")
        self.assertEqual(result["red_features"], [])
        self.assertTrue(result["revalidation_required"])

    def test_missing_numeric_is_explicit_bin(self):
        ref = [1.0] * 200 + [None] * 40
        new = [1.0] * 100 + [None] * 20
        baseline = psi.freeze_column(ref)
        result = psi.compare_column(baseline, new)
        self.assertEqual(result["missing_reference_count"], 40)
        self.assertEqual(result["missing_current_count"], 20)
        self.assertEqual(result["band"], "green")
        self.assertEqual(baseline["labels"][-1], "missing")

    def test_nonfinite_values_fail_closed(self):
        for bad in (float("nan"), float("inf"), "nan", "-inf", "not-a-number"):
            with self.subTest(bad=str(bad)):
                with self.assertRaises(ValueError):
                    psi.freeze_column([1.0] * 239 + [bad])
        with self.assertRaises(ValueError):
            psi.freeze_column([0.2] * 200 + [1.2], score=True)

    def test_categorical_unknown_and_other_known(self):
        ref = ["a"] * 210 + ["b"] * 20 + ["c"] * 10
        frozen = psi.freeze_column(ref, kind="categorical", config={"max_categories": 1})
        self.assertEqual(frozen["retained_categories"], ["a"])
        outcome = psi.compare_column(frozen, ["a"] * 100 + ["c"] * 10 + ["new"] * 10,
                                     config={"max_categories": 1})
        self.assertEqual(outcome["unknown_current_count"], 10)
        self.assertEqual(outcome["bins"][1]["current_count"], 10)
        self.assertEqual(outcome["bins"][2]["current_count"], 10)

    def test_quantile_bins_frozen_and_open_ended(self):
        ref = [float(i) for i in range(240)]
        frozen = psi.freeze_column(ref)
        self.assertLessEqual(len(frozen["edges"]), 9)
        outcome = psi.compare_column(frozen, [-9999.0] * 60 + [9999.0] * 60)
        self.assertEqual(outcome["current_rows"], 120)
        self.assertEqual(outcome["bins"][0]["current_count"], 60)
        self.assertEqual(outcome["bins"][-2]["current_count"], 60)

    def test_small_cohort_never_green(self):
        frozen = psi.freeze_column([0.5] * 25)
        outcome = psi.compare_column(frozen, [0.5] * 25)
        self.assertEqual(outcome["status"], "NOT_ASSESSABLE")
        self.assertEqual(outcome["band"], "not_assessable")
        self.assertIsNotNone(outcome["psi"])

    def test_exact_policy_cutoffs(self):
        frozen = psi.freeze_column([1.0] * 200 + [2.0] * 200)
        new = [1.0] * 120 + [2.0] * 80
        prelim = psi.compare_column(frozen, new)
        value = prelim["psi"]
        self.assertGreater(value, 0)
        cfg = {"green_below": value, "red_above": value * 2}
        self.assertEqual(psi.compare_column(frozen, new, config=cfg)["band"], "amber")
        cfg["red_above"] = value / 2
        cfg["green_below"] = value / 4
        self.assertEqual(psi.compare_column(frozen, new, config=cfg)["band"], "red")

    def test_tampering_and_model_binding_rejected(self):
        baseline = self.baseline()
        baseline["columns"]["x"]["counts"][0] += 1
        with self.assertRaisesRegex(ValueError, "checksum"):
            psi.evaluate_window(baseline=baseline,
                                current_inputs=self.current_inputs,
                                current_scores=self.current_scores)
        baseline = self.baseline()
        with self.assertRaisesRegex(ValueError, "another model"):
            psi.evaluate_window(baseline=baseline,
                                current_inputs=self.current_inputs,
                                current_scores=self.current_scores,
                                expected_model_id="other")
        with self.assertRaisesRegex(ValueError, "different model artifact"):
            psi.evaluate_window(baseline=baseline,
                                current_inputs=self.current_inputs,
                                current_scores=self.current_scores,
                                expected_artifact_sha256="0" * 64)

    def test_incorrect_row_order_and_target_leakage_rejected(self):
        base = self.baseline()
        write_csv(self.current_scores, ["row_id", "positive_probability"],
                  [(f"r{i:06d}", 0.1) for i in reversed(range(120))])
        with self.assertRaisesRegex(ValueError, "order"):
            psi.evaluate_window(baseline=base,
                                current_inputs=self.current_inputs,
                                current_scores=self.current_scores)
        write_csv(self.current_inputs, ["row_id", "x", "Class"],
                  [(f"r{i:06d}", 0.1, 0) for i in range(120)])
        with self.assertRaisesRegex(ValueError, "forbidden"):
            psi.evaluate_window(baseline=base,
                                current_inputs=self.current_inputs,
                                current_scores=self.current_scores)

    def test_reusing_frozen_reference_rows_is_rejected(self):
        baseline = self.baseline()
        # Different file path and order cannot make the same cohort independent.
        self.write_pair(self.current_inputs, self.current_scores, 240, 0.1)
        with self.assertRaisesRegex(ValueError, "reuses"):
            psi.evaluate_window(
                baseline=baseline, current_inputs=self.current_inputs,
                current_scores=self.current_scores)
        order = list(reversed(range(240)))
        write_csv(self.current_inputs, ["row_id", "x"],
                  [(f"r{i:06d}", 0.1) for i in order])
        write_csv(self.current_scores, ["row_id", "positive_probability"],
                  [(f"r{i:06d}", 0.1) for i in order])
        with self.assertRaisesRegex(ValueError, "reuses"):
            psi.evaluate_window(
                baseline=baseline, current_inputs=self.current_inputs,
                current_scores=self.current_scores)

    def test_invalid_reference_scope_rejected(self):
        with self.assertRaisesRegex(ValueError, "development"):
            psi.freeze_baseline(
                model_id="my_model", feature_order=["x"],
                reference_inputs=self.reference_inputs,
                reference_scores=self.reference_scores, reference_scope="test")

    def test_monitor_and_validator_use_same_evaluation(self):
        baseline = monitor.freeze(
            manifest_path=self.manifest_path,
            reference_inputs=self.reference_inputs,
            reference_scores=self.reference_scores,
            policy=psi.policy_config(),
        )
        baseline_dir = self.root / "baselines" / "my_model"
        baseline_dir.mkdir(parents=True)
        baseline_path = baseline_dir / "baseline.json"
        baseline_path.write_text(json.dumps(baseline))
        current_dir = self.root / "windows" / "my_model"
        current_dir.mkdir(parents=True)
        self.write_pair(current_dir / "inputs.csv", current_dir / "scores.csv", 120, 0.9)
        actual = monitor.evaluate(
            baseline_path=baseline_path, current_inputs=current_dir / "inputs.csv",
            current_scores=current_dir / "scores.csv", manifest_path=self.manifest_path)
        evidence, checks = validator.evaluate_model_psi(
            "my_model", self.model, True,
            {"stability": psi.policy_config()},
            self.root / "baselines", self.root / "windows")
        self.assertEqual(actual, evidence)
        self.assertEqual({c["check_id"] for c in checks}, {"score_psi", "feature_psi"})
        self.assertTrue(all(c["status"] == "REVIEW" for c in checks))
        self.assertTrue(evidence["revalidation_required"])

    def test_freeze_evaluate_cli_roundtrip(self):
        baseline_path = self.root / "baseline_cli.json"
        result_path = self.root / "window_cli.json"
        policy_path = self.root / "policy.json"
        policy_path.write_text(json.dumps({"stability": psi.policy_config()}))
        rc = monitor.main([
            "freeze", "--manifest", str(self.manifest_path),
            "--reference-inputs", str(self.reference_inputs),
            "--reference-scores", str(self.reference_scores),
            "--policy", str(policy_path), "--output", str(baseline_path),
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(baseline_path.is_file())
        self.write_pair(self.current_inputs, self.current_scores, 120, 0.9)
        rc = monitor.main([
            "evaluate", "--baseline", str(baseline_path),
            "--manifest", str(self.manifest_path),
            "--current-inputs", str(self.current_inputs),
            "--current-scores", str(self.current_scores),
            "--output", str(result_path),
        ])
        self.assertEqual(rc, 0)
        outcome = json.loads(result_path.read_text())
        self.assertEqual(outcome["band"], "red")
        self.assertTrue(outcome["revalidation_required"])

    def test_missing_evidence_is_not_assessable(self):
        evidence, checks = validator.evaluate_model_psi(
            "my_model", self.model, True,
            {"stability": psi.policy_config()}, None, None)
        self.assertIsNone(evidence)
        self.assertTrue(all(c["status"] == "NOT_ASSESSABLE" for c in checks))

    def test_cox_is_not_applicable(self):
        evidence, checks = validator.evaluate_model_psi(
            "synthetic_demo", self.model, False,
            {"stability": psi.policy_config()}, None, None)
        self.assertIsNone(evidence)
        self.assertTrue(all(c["status"] == "NOT_APPLICABLE" for c in checks))


if __name__ == "__main__":
    unittest.main()
