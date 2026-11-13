"""Unit tests for cross-platform saved-artifact inference comparison (no deserialization)."""
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'provenance'
spec = importlib.util.spec_from_file_location('inference_comparison', ROOT / 'compare_saved_artifact_inference.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SavedArtifactInferenceTests(unittest.TestCase):
    def test_single_binary32_ulp_is_diagnostic_not_auto_pass(self):
        import struct
        successor = struct.unpack('!f', struct.pack('!I', 0x3e800001))[0]
        self.assertEqual(m.float32_ulp_distance(0.25, successor), 1)
        self.assertGreater(abs(successor - 0.25), 1e-10)

    def test_identical_saved_artifact_outputs_pass_and_divergence_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            models = root / 'legacy_models'
            predictions = root / 'predictions'
            predictions.mkdir()
            for name in m.MODELS:
                pkg = models / name
                fixture = pkg / 'fixtures'
                fixture.mkdir(parents=True)
                (pkg / 'model.joblib').write_bytes(b'example-saved-model-' + name.encode())
                (pkg / 'manifest.json').write_text(json.dumps({
                    'artifact_sha256': m.digest(pkg / 'model.joblib'),
                    'feature_order': ['x'],
                }))
                (fixture / 'development_inputs.csv').write_text('row_id,x\na,1\nb,2\n')
                (fixture / 'reference_predictions.csv').write_text(
                    'row_id,positive_probability\na,0.1\nb,0.2\n')
                for platform in ('macos', 'linux_amd64'):
                    (predictions / f'{name}_{platform}.csv').write_text(
                        'row_id,positive_probability\na,0.1\nb,0.2\n')
            report = m.compare(models, predictions)
            self.assertEqual(report['status'], 'PASS')
            (predictions / 'credit_xgboost_python_linux_amd64.csv').write_text(
                'row_id,positive_probability\na,0.1\nb,0.25\n')
            report = m.compare(models, predictions)
            self.assertEqual(report['status'], 'FAIL')
            self.assertAlmostEqual(report['models']['credit_xgboost_python']['max_absolute_macos_vs_linux'], 0.05)
            self.assertTrue(report['models']['credit_xgboost_python']['ulp_analysis_is_diagnostic_only'])
            (predictions / 'credit_xgboost_python_linux_amd64.csv').write_text(
                'row_id,positive_probability\nb,0.2\na,0.1\n')
            with self.assertRaises(ValueError):
                m.compare(models, predictions)


if __name__ == '__main__':
    unittest.main()
