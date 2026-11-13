"""P0 dataset upload staging tests: real canonical splits, synthetic source bytes."""
import importlib.util
import json
import hashlib
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "p0_datasets", ROOT / "legacy_models/integration/prepare_dataset_uploads.py")
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


class DatasetUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "upload"

    def test_uci_descriptor_and_committed_split_verified(self):
        result = stage.stage_dataset("uci", self.output, self.root)
        self.assertEqual(result["descriptor"]["status"], "PASS")
        self.assertEqual(result["split"]["status"], "PASS")
        self.assertEqual(result["split"]["rows"], 30000)
        self.assertEqual(result["source_status"], "NOT_VERIFIED")
        self.assertFalse(result["upload_ready"])
        self.assertEqual(len(result["linked_models"]), 4)
        self.assertEqual(result["split"]["sha256"],
                         "e8feb21a0c57c5d6c8344af11aaaab079064f259e4861faad8f4aef40fe6d6cb")

    def test_ulb_split_recreated_with_exact_sha(self):
        result = stage.stage_dataset("ulb", self.output, self.root)
        self.assertEqual(result["split"]["status"], "PASS")
        self.assertEqual(result["split"]["rows"], 284807)
        self.assertEqual(result["split"]["partition_counts"],
                         {"train": 162992, "validation": 61675, "test": 60140})
        self.assertEqual(result["split"]["sha256"],
                         "5d9edf4df20e866d3198e74893fd2e1cf75f5fc3c5b4e175efd5a4a478b827fa")
        # Re-running must never change the hash-pinned split.
        second = stage.stage_dataset("ulb", self.output, self.root)
        self.assertEqual(second["split"], result["split"])

    def test_modified_uci_split_fails_closed(self):
        stage.stage_dataset("uci", self.output, self.root)
        split = self.output / "uci/uci_split_ids.csv"
        content = split.read_text(encoding="utf-8")
        split.write_text(content.replace("uci-000001", "uci-009999", 1), encoding="utf-8")
        with self.assertRaises(ValueError):
            stage.stage_dataset("uci", self.output, self.root, verify_only=True)
        with self.assertRaises(ValueError):
            stage.stage_dataset("uci", self.output, self.root)

    def test_source_requires_real_verified_data(self):
        with self.assertRaisesRegex(ValueError, "Missing or unsafe original source"):
            stage.stage_dataset("uci", self.output, self.root, with_source=True)

    def test_exact_source_copy_streams_and_refuses_tamper(self):
        original = self.root / "source.csv"
        original.write_bytes(b"a,b\\n1,2\\n")
        output = self.root / "dest.csv"
        expected = hashlib.sha256(original.read_bytes()).hexdigest()
        stage.copy_checked(original, output, expected)
        self.assertEqual(stage.digest(output), expected)
        stage.copy_checked(original, output, expected)
        output.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "refuse overwrite"):
            stage.copy_checked(original, output, expected)

    def test_required_all_three_flag_on_main_cli(self):
        rc = stage.main(["--out-dir", str(self.output), "--data-root", str(self.root),
                         "--dataset", "uci"])
        self.assertEqual(rc, 0)
        report = json.loads((self.output / "build_report.json").read_text())
        self.assertFalse(report["all_upload_ready"])
        self.assertFalse(report["datasets"][0]["upload_ready"])
        self.assertEqual(report["datasets"][0]["source_status"], "NOT_VERIFIED")
        rc = stage.main(["--out-dir", str(self.output), "--data-root", str(self.root),
                         "--dataset", "uci", "--with-source"])
        self.assertEqual(rc, 1)
        report = json.loads((self.output / "build_report.json").read_text())
        self.assertFalse(report["all_upload_ready"])
        self.assertEqual(report["datasets"][0]["status"], "FAIL")

if __name__ == "__main__":
    unittest.main()
