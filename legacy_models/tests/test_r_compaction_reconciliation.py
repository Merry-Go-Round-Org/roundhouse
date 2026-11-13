"""Test historical UCI reconciliation with an explicitly repackaged RDS.

Synthetic metadata only; never edits original reconciliation or source artifacts.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("standardizer", ROOT / "standardize_exl_models.py")
standardizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(standardizer)


class RepackagingReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.old = "a" * 64
        self.new = "b" * 64
        self.good = {
            "artifact_sha256": self.new,
            "artifact_compaction": {
                "format": "glm_coefficients_only_v1",
                "original_artifact_sha256": self.old,
                "retrained": False,
                "development_rows_checked": 100,
                "max_absolute_prediction_difference": 0.0,
            }
        }
        self.match = {"status": "match"}

    def test_original_fingerprint_is_unchanged(self):
        self.assertEqual(
            standardizer.reconciliation_artifact_binding(
                "credit_logistic_r", {"artifact_sha256": self.old},
                self.old, self.match),
            "original_reconciled_artifact")

    def test_verified_repackaging_retains_original_hash(self):
        self.assertEqual(
            standardizer.reconciliation_artifact_binding(
                "credit_logistic_r", self.good, self.old, self.match),
            "manifest_declared_compaction_requires_fresh_validation")

    def test_rejects_unsupported_and_tampered_compaction(self):
        cases = [
            {"format": "other"},
            {"original_artifact_sha256": "c" * 64},
            {"retrained": True},
            {"development_rows_checked": 99},
            {"max_absolute_prediction_difference": 1e-3},
            {"max_absolute_prediction_difference": float("nan")},
        ]
        for patch in cases:
            with self.subTest(patch=patch):
                manifest = dict(self.good)
                manifest["artifact_compaction"] = {**self.good["artifact_compaction"], **patch}
                self.assertIsNone(standardizer.reconciliation_artifact_binding(
                    "credit_logistic_r", manifest, self.old, self.match))
        self.assertIsNone(standardizer.reconciliation_artifact_binding(
            "credit_logistic_r", self.good, self.old, {"status": "failed"}))
        self.assertIsNone(standardizer.reconciliation_artifact_binding(
            "credit_logistic_python", self.good, self.old, self.match))

    def test_split_audit_retains_historical_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            models = Path(folder) / "legacy_models"
            models.mkdir()
            record = {
                "frozen_split_sha256": "d" * 64,
                "baseline_r_glm_artifact_sha256": self.old,
                "split_counts": {
                    "validation": {"rows": 6037, "positives": 1351},
                    "test": {"rows": 5982, "positives": 1317}
                }
            }
            (models / "UCI_SPLIT_RECONCILIATION.json").write_text(json.dumps(record))
            eval_ = {
                "validation": {"n": 6037, "positives": 1351},
                "test": {"n": 5982, "positives": 1317}
            }
            result = standardizer.split_provenance(
                models, "credit_logistic_r", self.good, eval_, self.match)
            self.assertEqual(result["reconciliation_artifact_binding"],
                             "manifest_declared_compaction_requires_fresh_validation")
            self.assertTrue(result["packaging_only_successor_requires_fresh_provenance"])
            self.assertEqual(result["status"], "reconciliation_bound_not_fit_attested")
            self.assertEqual(json.loads((models / "UCI_SPLIT_RECONCILIATION.json").read_text()),
                             record)


if __name__ == "__main__":
    unittest.main()
