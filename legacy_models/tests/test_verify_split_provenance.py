"""Offline unit tests for retrospective split provenance checks (no full datasets)."""
import importlib.util
import json
import hashlib
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT = Path(__file__).resolve().parents[1] / 'shared' / 'verify_split_provenance.py'
if not SCRIPT.is_file():
    SCRIPT = Path(__file__).resolve().with_name('verify_split_provenance.py')
spec = importlib.util.spec_from_file_location('provenance_under_test', SCRIPT)
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)

class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sha256_and_artifact_checks(self):
        pkg = self.root / 'model'
        pkg.mkdir()
        art = pkg / 'model.joblib'
        art.write_bytes(b'example trusted artifact')
        expected = hashlib.sha256(art.read_bytes()).hexdigest()
        manifest = {'artifact_paths': ['model.joblib'], 'artifact_sha256': expected}
        self.assertEqual(v.artifact_status(pkg, manifest)['status'], 'match')
        art.write_bytes(b'tampered')
        self.assertEqual(v.artifact_status(pkg, manifest)['status'], 'mismatch')
        art.write_text('version https://git-lfs.github.com/spec/v1\n')
        self.assertEqual(v.artifact_status(pkg, manifest)['status'], 'lfs_pointer_not_downloaded')
        manifest['artifact_paths'] = ['../private.bin']
        self.assertEqual(v.artifact_status(pkg, manifest)['status'], 'unsafe_path')

    def test_fixture_alignment_and_missing_id(self):
        pkg = self.root / 'model'
        (pkg / 'fixtures').mkdir(parents=True)
        raw = pd.DataFrame({'row_id': ['id1', 'id2', 'id3'], 'x': [1.0, 2.0, 3.0]})
        split = np.array(['train', 'validation', 'validation'])
        inputs = pkg / 'fixtures/development_inputs.csv'
        reference = pkg / 'fixtures/reference_predictions.csv'
        inputs.write_text('row_id,x\nid2,2.0\nid3,3.0\n')
        reference.write_text('row_id,positive_probability\nid2,0.2\nid3,0.3\n')
        self.assertEqual(v.fixture_alignment(pkg, raw, split, ['x'])['status'], 'match')
        inputs.write_text('row_id,x\nid3,3.0\nid2,2.0\n')
        self.assertEqual(v.fixture_alignment(pkg, raw, split, ['x'])['status'],
                         'fixture_rows_not_first_validation')

    def test_r_fit_parameter_recovery_and_rejected_wrong_coefficients(self):
        import statsmodels.api as sm
        rng = np.random.default_rng(100)
        x = rng.normal(size=(240, 2))
        prob = 1 / (1 + np.exp(-(-0.3 + 0.9*x[:, 0] - 0.5*x[:, 1])))
        y = rng.binomial(1, prob)
        frame = pd.DataFrame({'row_id': [f'test-{i:03}' for i in range(len(x))],
                              'x1': x[:, 0], 'x2': x[:, 1], 'Class': y})
        parts = np.array(['train'] * 180 + ['validation'] * 60)
        train = x[:180]
        centre = train.mean(axis=0)
        scale = train.std(axis=0, ddof=1)
        fit = sm.GLM(y[:180], sm.add_constant((train-centre)/scale),
                     family=sm.families.Binomial()).fit(maxiter=150, tol=1e-9)
        beta = fit.params[1:] / scale
        b0 = fit.params[0] - centre @ beta
        path = self.root / 'coeffs.json'
        path.write_text(json.dumps({'feature_order': ['x1', 'x2'],
                                    'intercept': float(b0), 'coefficients': beta.tolist()}))
        reference = self.root / 'reference_predictions.csv'
        from scipy.special import expit
        expected_p = expit(b0 + x[180:] @ beta)
        pd.DataFrame({'row_id': frame.row_id.iloc[180:],
                      'positive_probability': expected_p}).to_csv(reference, index=False)
        record = v.refit_r_glm(frame, parts, ['x1', 'x2'], 'Class', path, reference)
        self.assertEqual(record['status'], 'pass', record)
        saved = json.loads(path.read_text()); saved['coefficients'][0] += 0.01
        path.write_text(json.dumps(saved))
        self.assertEqual(v.refit_r_glm(frame, parts, ['x1', 'x2'], 'Class', path, reference)['status'], 'mismatch')

    def test_python_replay_mismatch_exposes_diagnostics(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch
        class ConstantModel:
            def fit(self, X, y):
                return self
            def predict_proba(self, X):
                z = np.full(len(X), 0.5)
                return np.column_stack([1-z, z])
        frame = pd.DataFrame({'row_id': ['a', 'b', 'c', 'd'],
                              'x': [1, 2, 3, 4], 'Class': [0, 1, 0, 1]})
        splits = np.array(['train', 'train', 'validation', 'validation'])
        root = self.root / 'fixtures'
        root.mkdir()
        inputs = root / 'development_inputs.csv'
        inputs.write_text('row_id,x\nc,3\nd,4\n')
        (root / 'reference_predictions.csv').write_text(
            'row_id,positive_probability\nc,0.1\nd,0.2\n')
        fake = SimpleNamespace(model_object=lambda *args: ConstantModel())
        with patch.dict(sys.modules, {'pipeline': fake}):
            result = v.refit_python(frame, splits, ['x'], 'Class',
                                    'fraud_xgboost_python', inputs)
        self.assertEqual(result['status'], 'mismatch')
        self.assertAlmostEqual(result['max_abs_fixture_difference'], 0.4)
        self.assertAlmostEqual(result['mean_abs_fixture_difference'], 0.35)
        self.assertEqual(result['n_fixture_differences_above_tolerance'], 2)


if __name__ == '__main__':
    unittest.main()