"""Guard against conflating retrospective fit reproduction with model governance approval."""
import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validation_governance_under_test', ROOT / 'validate_all_models.py')
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)


class GovernanceFindingTests(unittest.TestCase):
    def test_combined_reproduction_is_a_technical_pass_not_approval(self):
        item = {
            'name': 'credit_xgboost_python', 'dataset': 'uci',
            'task': 'binary_classification', 'status': 'executed_full_data',
            'model_kind': 'real_dataset',
            'artifact_integrity': {'status': 'match'},
            'errors': [],
            'evidence': {
                'validation': {}, 'test': {},
                'split_provenance': {
                    'status': 'retrospective_fit_reproduced_artifact_verified',
                    'retrospective_reproduction': {
                        'cross_environment_divergences': [
                            {'environment': 'linux_amd64', 'status': 'mismatch',
                             'max_abs_fixture_difference': 0.030629813671112005}
                        ],
                    },
                },
                'fairness': {}, 'parity': {},
            },
        }
        policy = {'metric_consistency': {'absolute_tolerance': 1e-6},
                  'required_binary_checks': [], 'required_extra_checks': {}}
        compare = {'uci': {
            'status': 'cross_environment_fits_reproduced_on_canonical_split_with_divergences',
        }}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / 'credit_xgboost_python'
            pkg.mkdir()
            (pkg / 'manifest.json').write_text(json.dumps({'feature_order': [],
                'artifact_sha256': 'a' * 64}))
            with patch.object(v, 'metric_issues', return_value=[]), \
                 patch.object(v, 'schema_check', return_value=v.check(
                     'schema_contract', 'PASS', True, 'ok', 'static')), \
                 patch.object(v, 'saved_artifact_inference_check',
                              return_value=v.check('cross_platform_saved_artifact_inference',
                                                   'NOT_ASSESSABLE', False, 'missing', 'governance_limitation')):
                result = v.report_model(item, root, compare, policy, False)
        checks = {x['check_id']: x for x in result['checks']}
        self.assertEqual(checks['split_provenance']['status'], 'PASS')
        self.assertEqual(checks['historical_fit_time_attestation']['status'], 'REVIEW')
        self.assertEqual(checks['cross_platform_refit']['status'], 'REVIEW')
        self.assertEqual(checks['cross_platform_saved_artifact_inference']['status'], 'NOT_ASSESSABLE')
        self.assertFalse(result['summary']['human_approval_granted'])

    def test_numeric_inference_mismatch_is_review_not_hidden_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            models_dir = root / 'legacy_models'
            module_path = models_dir / 'provenance/compare_saved_artifact_inference.py'
            module_path.parent.mkdir(parents=True)
            actual = {
                'artifact_sha256': 'a'*64,
                'development_fixture_sha256': 'b'*64,
                'rows': 100,
                'max_absolute_macos_vs_linux': 1.4901161193847656e-8,
                'max_absolute_macos_vs_reference': 0.0,
                'max_absolute_linux_vs_reference': 1.4901161193847656e-8,
                'max_float32_ulp_distance_macos_vs_linux': 1,
                'max_float32_ulp_distance_macos_vs_reference': 0,
                'max_float32_ulp_distance_linux_vs_reference': 1,
                'ulp_analysis_is_diagnostic_only': True,
                'tolerance_absolute': 1e-10,
                'status': 'FAIL',
                'macos_output_sha256': 'c'*64,
                'linux_output_sha256': 'd'*64,
            }
            module_path.write_text(
                'def compare(models_dir, predictions_dir):\n'
                '    return {"models": {"credit_xgboost_python": ' + repr(actual) + '}}\n'
            )
            report = root / 'datasets/reports/provenance/inference_portability.json'
            report.parent.mkdir(parents=True)
            saved = {
                'schema_version': '1.0.0',
                'report_kind': 'saved_artifact_inference_portability',
                'source_execution_environments': ['macos', 'linux_amd64'],
                'not_a_training_reproduction': True,
                'models': {'credit_xgboost_python': actual},
            }
            report.write_text(json.dumps(saved))
            finding = v.saved_artifact_inference_check(models_dir, 'credit_xgboost_python')
            self.assertEqual(finding['status'], 'REVIEW')
            self.assertEqual(finding['details']['numeric_parity_status'], 'FAIL')
            self.assertEqual(finding['details']['comparison']['max_absolute_macos_vs_linux'],
                             1.4901161193847656e-8)
            # Tampering must remain a hard failure, not be downgraded.
            saved['models']['credit_xgboost_python'] = {**actual, 'status': 'PASS'}
            report.write_text(json.dumps(saved))
            tampered = v.saved_artifact_inference_check(models_dir, 'credit_xgboost_python')
            self.assertEqual(tampered['status'], 'FAIL')

    def test_inference_unexecuted_is_not_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = v.saved_artifact_inference_check(Path(tmp) / 'legacy_models',
                                                     'credit_xgboost_python')
        self.assertEqual(result['status'], 'NOT_ASSESSABLE')
        self.assertFalse(result['required'])


if __name__ == '__main__':
    unittest.main()
