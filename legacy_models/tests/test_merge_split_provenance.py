"""Offline provenance aggregation tests: same artifacts, two distinct runtimes, no false positives."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1] / 'shared'
sys.path.insert(0, str(HERE))
import merge_split_provenance as agg


class CrossEnvironmentMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.models = self.root / 'legacy_models'
        self.data = self.root / 'datasets'
        self.models.mkdir()
        (self.models / 'evidence').mkdir()
        self.sources = {}
        local_sha = {}
        for domain in ('uci', 'ulb'):
            p = self.data / agg.DATA_PATH[domain]
            p.parent.mkdir(parents=True)
            p.write_bytes(('data ' + domain).encode())
            split = self.models / 'evidence' / (domain + '_split_ids.csv')
            split.write_text('row_id,split\nx,train\n')
            local_sha[domain] = {'data': agg.sha256(p), 'split': agg.sha256(split)}
        self.shas_patch = patch.dict(agg.SHAS, local_sha)
        self.shas_patch.start()
        self.addCleanup(self.shas_patch.stop)
        self.counts_patch = patch.dict(agg.COUNTS, {
            'uci': {'train': (10, 4), 'validation': (4, 1), 'test': (5, 2)},
            'ulb': {'train': (12, 3), 'validation': (6, 1), 'test': (6, 1)},
        })
        self.counts_patch.start()
        self.addCleanup(self.counts_patch.stop)
        self.manifests = {}
        for domain, names in agg.MODELS.items():
            for name in names:
                directory = self.models / name
                directory.mkdir()
                art = directory / ('model.rds' if name.endswith('_r') else 'model.joblib')
                art.write_bytes(('artifact:' + name).encode())
                manifest = {'artifact_paths': [art.name], 'artifact_sha256': agg.sha256(art)}
                (directory / 'manifest.json').write_text(json.dumps(manifest))
                self.manifests[name] = manifest
                fixtures = directory / 'fixtures'
                fixtures.mkdir()
                (fixtures / 'development_inputs.csv').write_text('row_id,x\nz,1\n')
                (fixtures / 'reference_predictions.csv').write_text('row_id,positive_probability\nz,0.2\n')
        for label, platform in (('macos', 'Darwin'), ('linux_amd64', 'Linux')):
            self.sources[label] = {
                'schema_version': '1.0.0',
                'report_kind': 'independent_split_provenance_reproduction',
                'replay_python_requested': True,
                'historical_fit_time_attestation': False,
                'execution_environment': {'platform': platform, 'python': '3.13', 'xgboost': '3.1.3'},
                'domains': {},
            }
            for domain, names in agg.MODELS.items():
                entries = {}
                for name in names:
                    # Mac reproduces credit XGB; Linux reproduces fraud XGB.
                    mismatch = (name == 'fraud_xgboost_python' and label == 'macos'
                                or name == 'credit_xgboost_python' and label == 'linux_amd64')
                    tol = 1e-8 if name.endswith('_r') else 1e-7
                    err = 0.03 if mismatch else 1e-12
                    sha = self.manifests[name]['artifact_sha256']
                    entries[name] = {
                        'status': 'not_reproduced' if mismatch else 'retrospectively_reproduced_artifact_verified',
                        'model_id': name, 'artifact_sha256': sha,
                        'split_sha256': agg.SHAS[domain]['split'],
                        'artifact': {'status': 'match', 'expected_sha256': sha, 'actual_sha256': sha},
                        'development_fixture_alignment': {'status': 'match', 'rows': 100},
                        'development_fixture_sha256': agg.sha256(self.models / name / 'fixtures/development_inputs.csv'),
                        'reference_predictions_sha256': agg.sha256(self.models / name / 'fixtures/reference_predictions.csv'),
                        'fit_reproduction': {
                            'status': 'mismatch' if mismatch else 'pass',
                            'tolerance_absolute': tol,
                            'max_abs_fixture_difference': err,
                            'train_rows': agg.COUNTS[domain]['train'][0],
                            'train_positives': agg.COUNTS[domain]['train'][1],
                            **({'max_abs_parameter_difference': 1e-12} if name.endswith('_r') else {}),
                        },
                    }
                self.sources[label]['domains'][domain] = {
                    'data_sha256': agg.SHAS[domain]['data'],
                    'canonical_split_sha256': agg.SHAS[domain]['split'],
                    'split_definition_verification': 'pass',
                    'partition_counts': {k: {'rows': v[0], 'positives': v[1]}
                                         for k,v in agg.COUNTS[domain].items()},
                    'models': entries,
                }
        self.mac_path = self.root / 'macos.json'
        self.linux_path = self.root / 'linux.json'

    def audit(self):
        self.mac_path.write_text(json.dumps(self.sources['macos']))
        self.linux_path.write_text(json.dumps(self.sources['linux_amd64']))
        return agg.merge_reports(self.sources['macos'], self.sources['linux_amd64'],
                                 self.models, self.data, self.mac_path, self.linux_path)

    def test_complementary_replays_pass_with_explicit_divergences(self):
        result = self.audit()
        self.assertFalse(result['historical_fit_time_attestation'])
        self.assertEqual(result['aggregation']['status'], 'complete')
        self.assertEqual(len(result['domains']['uci']['models']), 4)
        self.assertEqual(len(result['domains']['ulb']['models']), 4)
        credit = result['domains']['uci']['models']['credit_xgboost_python']
        fraud = result['domains']['ulb']['models']['fraud_xgboost_python']
        self.assertEqual(credit['verification_environment'], 'macos')
        self.assertEqual(fraud['verification_environment'], 'linux_amd64')
        self.assertEqual(credit['cross_environment_divergences'][0]['environment'], 'linux_amd64')
        self.assertEqual(fraud['cross_environment_divergences'][0]['environment'], 'macos')
        self.assertEqual(credit['cross_environment_divergences'][0]['status'], 'mismatch')
        self.assertEqual(fraud['status'], 'retrospectively_reproduced_artifact_verified')

    def test_missing_reproduction_on_both_is_rejected(self):
        rec = self.sources['macos']['domains']['uci']['models']['credit_xgboost_python']
        rec['status'] = 'not_reproduced'
        rec['fit_reproduction']['status'] = 'mismatch'
        rec['fit_reproduction']['max_abs_fixture_difference'] = 0.03
        with self.assertRaisesRegex(ValueError, 'no successful independent refit'):
            self.audit()

    def test_tampered_report_artifact_is_rejected(self):
        rec = self.sources['linux_amd64']['domains']['ulb']['models']['fraud_xgboost_python']
        rec['artifact']['actual_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'source run did not hash-match'):
            self.audit()

    def test_false_pass_with_error_above_tolerance_is_rejected(self):
        entry = self.sources['macos']['domains']['uci']['models']['credit_xgboost_python']
        entry['fit_reproduction']['max_abs_fixture_difference'] = 0.03
        with self.assertRaisesRegex(ValueError, 'reported fit status inconsistent'):
            self.audit()

    def test_relaxed_tolerance_is_rejected(self):
        entry = self.sources['linux_amd64']['domains']['ulb']['models']['fraud_xgboost_python']
        entry['fit_reproduction']['tolerance_absolute'] = 0.1
        with self.assertRaisesRegex(ValueError, 'tolerance was relaxed'):
            self.audit()

    def test_wrong_local_artifact_or_split_is_rejected(self):
        (self.models / 'credit_logistic_python' / 'model.joblib').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'current artifact not SHA-verified'):
            self.audit()
        # This applies even if the two source JSON files both claim success.

    def test_fixture_modified_since_audit_is_rejected(self):
        fixture = self.models / 'credit_logistic_python' / 'fixtures/reference_predictions.csv'
        fixture.write_text('row_id,positive_probability\nz,0.9\n')
        with self.assertRaisesRegex(ValueError, 'fixture digest'):
            self.audit()

    def test_incomplete_source_report_is_rejected(self):
        del self.sources['linux_amd64']['domains']['uci']['models']['credit_logistic_r']
        with self.assertRaisesRegex(ValueError, 'inventory incomplete'):
            self.audit()

    def test_reports_from_same_platform_rejected(self):
        self.sources['macos']['execution_environment']['platform'] = 'Linux'
        with self.assertRaisesRegex(ValueError, 'expected Darwin'):
            self.audit()

    def test_merged_run_records_sha_of_each_source(self):
        report = self.audit()
        self.assertEqual(report['aggregation']['source_reports']['macos']['sha256'], agg.sha256(self.mac_path))
        self.assertEqual(report['aggregation']['source_reports']['linux_amd64']['sha256'], agg.sha256(self.linux_path))


if __name__ == '__main__':
    unittest.main()
