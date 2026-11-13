"""Unit tests for plain-Git model-artifact migration (no datasets or R required)."""
import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "plain_git_migration", ROOT / "legacy_models/tools/migrate_lfs_to_git.py"
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class PlainGitMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.file = Path(self.temp.name) / "model.rds"
        self.file.write_bytes(b"compact fake RDS bytes, synthetic fixture only")
        self.sha = hashlib.sha256(self.file.read_bytes()).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def test_attributes_accept_original_or_already_converted_rules(self):
        original = list(migration.OLD_RULES)
        plain = list(migration.OLD_RULES.values())
        expected = "\n".join(plain) + "\n"
        for lines in (original, plain, [original[0], plain[1]]):
            with self.subTest(lines=lines):
                self.assertEqual(migration.normalize_model_attributes(lines), expected)

    def test_attributes_reject_missing_or_conflicting_rules(self):
        original = list(migration.OLD_RULES)
        plain = list(migration.OLD_RULES.values())
        for lines in ([original[0]], [original[0], original[0], original[1]],
                      [original[0], plain[0], original[1]]):
            with self.subTest(lines=lines):
                with self.assertRaisesRegex(ValueError, "Unexpected existing LFS"):
                    migration.normalize_model_attributes(lines)

    def test_raw_git_staging_replaces_pointer_not_binary(self):
        import subprocess

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            rel = "legacy_models/credit_logistic_python/model.joblib"
            file = root / rel
            file.parent.mkdir(parents=True)
            (root / ".gitattributes").write_text(
                "legacy_models/**/*.joblib -filter -diff -merge -text\n"
            )
            file.write_text(
                "version https://git-lfs.github.com/spec/v1\n"
                + "oid sha256:" + "0" * 64 + "\nsize 10\n"
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "--", ".gitattributes", rel],
                           cwd=root, check=True)
            binary = b"\x01\x02nonpointer\x00data"
            file.write_bytes(binary)
            original_root = migration.ROOT
            try:
                migration.ROOT = root
                migration.stage_unfiltered_blob(rel)
                staged = subprocess.check_output(
                    ["git", "show", ":" + rel], cwd=root
                )
                self.assertEqual(staged, binary)
                self.assertEqual(int(subprocess.check_output(
                    ["git", "cat-file", "-s", ":" + rel], cwd=root
                )), len(binary))
            finally:
                migration.ROOT = original_root

    def test_lfs_pointer_reads_hash_and_size(self):
        pointer = (f"version https://git-lfs.github.com/spec/v1\n"
                   f"oid sha256:{self.sha}\nsize 42946693\n").encode()
        self.assertEqual(migration.lfs_pointer(pointer), (self.sha, 42946693))

    def test_lfs_pointer_rejects_real_binary(self):
        with self.assertRaises(ValueError):
            migration.lfs_pointer(self.file.read_bytes())

    def test_compact_r_artifact_requires_original_hash_and_evidence(self):
        manifest = {"artifact_sha256": self.sha, "artifact_compaction": {
            "format": "glm_coefficients_only_v1",
            "original_artifact_sha256": "f" * 64,
            "retrained": False,
            "development_rows_checked": 100,
            "max_absolute_prediction_difference": 0.0,
        }}
        actual, size = migration.verify_binary(
            "fraud_logistic_r", manifest, self.file, "f" * 64, 42946693
        )
        self.assertEqual(actual, self.sha)
        self.assertEqual(size, self.file.stat().st_size)
        manifest["artifact_compaction"]["original_artifact_sha256"] = "a" * 64
        with self.assertRaisesRegex(ValueError, "lineage"):
            migration.verify_binary("fraud_logistic_r", manifest, self.file,
                                    "f" * 64, 42946693)
        manifest["artifact_compaction"]["original_artifact_sha256"] = "f" * 64
        manifest["artifact_compaction"]["max_absolute_prediction_difference"] = 0.1
        with self.assertRaisesRegex(ValueError, "lineage"):
            migration.verify_binary("fraud_logistic_r", manifest, self.file,
                                    "f" * 64, 42946693)

    def test_python_requires_same_hash_as_old_artifact(self):
        manifest = {"artifact_sha256": self.sha}
        self.assertEqual(
            migration.verify_binary("credit_logistic_python", manifest, self.file,
                                    self.sha, 999), (self.sha, self.file.stat().st_size)
        )
        with self.assertRaisesRegex(ValueError, "changed"):
            migration.verify_binary("credit_logistic_python", manifest, self.file,
                                    "1" * 64, 999)

    def test_rejects_lfs_pointer_in_working_tree(self):
        self.file.write_text("version https://git-lfs.github.com/spec/v1\n"
                             + "oid sha256:" + "1" * 64 + "\nsize 100\n")
        with self.assertRaisesRegex(ValueError, "LFS pointer"):
            migration.verify_binary("credit_logistic_python",
                                    {"artifact_sha256": self.sha},
                                    self.file, self.sha, 100)


if __name__ == "__main__":
    unittest.main()
