"""Tests for the portable upload ZIP builder; no ML libraries or R are required."""
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "integration" / "build_upload_bundles.py"
SPEC = importlib.util.spec_from_file_location("upload_builder", SCRIPT)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)

SCORER = """#!/usr/bin/env python3
import sys
from pathlib import Path
import argparse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'shared'))
from score_python import score
if __name__ == '__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--input',required=True)
    ap.add_argument('--output',required=True)
    args=ap.parse_args()
    score(Path(__file__).resolve().parent,args.input,args.output)
"""

FAKE_SHARED = """import csv
def score(package, input_csv, output_csv):
    with open(input_csv, newline='') as stream:
        rows = list(csv.DictReader(stream))
    with open(output_csv, 'w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['row_id', 'positive_probability'])
        writer.writeheader()
        for row in rows:
            writer.writerow({'row_id': row['row_id'], 'positive_probability': row['x']})
"""


class BundleBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.legacy = self.base / "legacy_models"
        self.model = self.legacy / "credit_logistic_python"
        self.model.mkdir(parents=True)
        shared = self.legacy / "shared"
        shared.mkdir()
        (shared / "score_python.py").write_text(FAKE_SHARED)
        (shared / "pipeline.py").write_text("SEED = 20261104\n")
        (shared / "train_python.py").write_text("def train(*args): pass\n")
        (self.model / "score.py").write_text(SCORER)
        (self.model / "train.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'shared'))\n"
            "from train_python import train\n")
        (self.model / "model.joblib").write_bytes(b"trusted-synthetic-artifact")
        (self.model / "schema.json").write_text('{"input":{},"output":{}}\n')
        (self.model / "evaluation.json").write_text('{"status":"synthetic-test"}\n')
        (self.model / "requirements.txt").write_text("# synthetic no dependencies\n")
        fixtures = self.model / "fixtures"
        fixtures.mkdir()
        (fixtures / "development_inputs.csv").write_text("row_id,x\nr1,0.2\nr2,0.8\n")
        (fixtures / "reference_predictions.csv").write_text(
            "row_id,positive_probability\nr1,0.2\nr2,0.8\n")
        artifact_hash = hashlib.sha256((self.model / "model.joblib").read_bytes()).hexdigest()
        self.manifest = {
            "name": "credit_logistic_python", "model_version": "1.0.0",
            "language": "Python", "task": "binary_classification",
            "artifact_paths": ["model.joblib"], "artifact_sha256": artifact_hash,
            "shared_pipeline_sha256": hashlib.sha256(
                (shared / "pipeline.py").read_bytes()).hexdigest(),
            "historical_shared_pipeline_sha256_claimed": "0" * 64,
            "scoring_code_sha256": "1" * 64,
            "model_code_sha256": "2" * 64,
            "purpose": "Synthetic packaging test", "intended_use": "Test",
            "model_owner": "Unit test", "risk_tier": "high",
            "tier_rationale": "Synthetic financial risk test",
        }
        (self.model / "manifest.json").write_text(json.dumps(self.manifest))

    def test_deterministic_zip_and_original_files_unmodified(self):
        original = (self.model / "score.py").read_bytes()
        original_manifest = (self.model / "manifest.json").read_bytes()
        content, warnings = builder.assemble("credit_logistic_python", self.legacy)
        a = self.base / "first.zip"
        b = self.base / "second.zip"
        builder.write_zip(a, content)
        builder.write_zip(b, content)
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(builder.verify_archive(a)["static_integrity"], "PASS")
        self.assertEqual(original, (self.model / "score.py").read_bytes())
        self.assertEqual(original_manifest, (self.model / "manifest.json").read_bytes())
        self.assertTrue(warnings)
        with zipfile.ZipFile(a) as archive:
            self.assertIn("shared/score_python.py", archive.namelist())
            self.assertNotIn("legacy_models/credit_logistic_python/manifest.json",
                             archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["shared_pipeline_sha256"],
                             hashlib.sha256(archive.read("shared/pipeline.py")).hexdigest())
            provenance = json.loads(archive.read("bundle_provenance.json"))
            self.assertEqual(provenance["source_shared_pipeline_sha256_claimed"],
                             hashlib.sha256((self.legacy / "shared/pipeline.py").read_bytes()).hexdigest())
            self.assertTrue(provenance["source_shared_pipeline_claim_matches_observed"])
            self.assertEqual(provenance["historical_shared_pipeline_sha256_claimed"], "0" * 64)
            self.assertFalse(provenance["historical_shared_pipeline_claim_matches_observed"])

    def test_isolated_smoke_replay(self):
        content, _ = builder.assemble("credit_logistic_python", self.legacy)
        path = self.base / "replay.zip"
        builder.write_zip(path, content)
        result = builder.smoke_test(path, 0)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["max_abs_difference"], 0)

    def test_private_fixtures_and_extra_data_are_excluded(self):
        (self.model / "fixtures" / "test_labels.csv").write_text("secret-labels")
        (self.model / "secret_scores.csv").write_text("private scores")
        (self.model / "private_test").mkdir()
        (self.model / "private_test" / "labels.json").write_text('{"test_labels": true}')
        content, _ = builder.assemble("credit_logistic_python", self.legacy)
        self.assertNotIn("fixtures/test_labels.csv", content)
        self.assertNotIn("secret_scores.csv", content)
        self.assertNotIn("private_test/labels.json", content)

    def test_reject_stale_current_pipeline_hash(self):
        self.manifest["shared_pipeline_sha256"] = "0" * 64
        (self.model / "manifest.json").write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, "shared_pipeline_sha256"):
            builder.assemble("credit_logistic_python", self.legacy)

    def test_reject_corrupted_artifact(self):
        (self.model / "model.joblib").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            builder.assemble("credit_logistic_python", self.legacy)

    def test_reject_symlink_in_package(self):
        target = self.base / "secret.json"
        target.write_text('{"secret":"do not package"}')
        link = self.model / "secret.json"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            builder.assemble("credit_logistic_python", self.legacy)

    def test_corrupt_zip_fails_verification(self):
        content, _ = builder.assemble("credit_logistic_python", self.legacy)
        path = self.base / "corrupt.zip"
        builder.write_zip(path, content)
        with zipfile.ZipFile(path, mode="a") as archive:
            archive.writestr("unexpected.txt", "injected")
        with self.assertRaisesRegex(ValueError, "file list"):
            builder.verify_archive(path)


if __name__ == "__main__":
    unittest.main()
