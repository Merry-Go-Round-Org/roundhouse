"""Regression tests for consuming SHA-bound complementary provenance reports."""
import importlib.util
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('standardizer_crossplatform', ROOT / 'standardize_exl_models.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)

UCI = 'e8feb21a0c57c5d6c8344af11aaaab079064f259e4861faad8f4aef40fe6d6cb'
DATA = '30c6be3abd8dcfd3e6096c828bad8c2f011238620f5369220bd60cfc82700933'


class StandardizerCrossEnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.models = self.root / 'legacy_models'
        (self.models / 'evidence').mkdir(parents=True)
        (self.models / 'evidence' / 'uci_split_ids.csv').write_text('split sample')
        original = self.root / 'datasets/uci/default of credit card clients.xls'
        original.parent.mkdir(parents=True)
        original.write_bytes(b'fake local source')
        self.audit_dir = self.root / 'datasets/reports/provenance'
        self.audit_dir.mkdir(parents=True)
        self.sources = {}
        for platform, filename in [('macos', 'split_provenance_macos.json'),
                                   ('linux_amd64', 'split_provenance_linux.json')]:
            p = self.audit_dir / filename
            p.write_text(json.dumps({'platform': platform, 'fixture': 100}))
            self.sources[platform] = {
                'filename': filename, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
            }
        self.model_sha = 'a' * 64
        self.entry = {
            'status': 'retrospectively_reproduced_artifact_verified',
            'artifact_sha256': self.model_sha, 'split_sha256': UCI,
            'artifact': {'status': 'match'},
            'development_fixture_alignment': {'status': 'match'},
            'fit_reproduction': {'status': 'pass', 'max_abs_fixture_difference': 1e-15},
            'verification_environment': 'macos',
            'supporting_source_report_sha256': self.sources['macos']['sha256'],
            'cross_environment_divergences': [{
                'environment': 'linux_amd64', 'status': 'mismatch',
                'max_abs_fixture_difference': 0.030629813671112005,
            }],
        }
        self.audit = {
            'report_kind': 'independent_split_provenance_reproduction',
            'aggregation': {
                'status': 'complete',
                'kind': 'two_environment_verified_artifact_replay',
                'source_reports': self.sources,
            },
            'domains': {'uci': {
                'split_definition_verification': 'pass',
                'canonical_split_sha256': UCI, 'data_sha256': DATA,
                'models': {'credit_xgboost_python': self.entry},
            }},
        }
        self.path = self.audit_dir / 'split_provenance.json'
        self.path.write_text(json.dumps(self.audit))
        self.orig_digest = s.digest

    def check(self):
        def digest(path):
            if path.name == 'uci_split_ids.csv':
                return UCI
            if path.name.endswith('.xls'):
                return DATA
            return self.orig_digest(path)
        with patch.object(s, 'digest', side_effect=digest):
            return s._corroborate_independent_replay(
                self.models, 'credit_xgboost_python', {'artifact_sha256': self.model_sha},
                {'status': 'reconciliation_bound_not_fit_attested'}, {'status': 'match'})

    def test_verified_aggregation_records_selected_platform_and_divergence(self):
        value = self.check()
        self.assertEqual(value['status'], 'retrospective_fit_reproduced_artifact_verified')
        self.assertEqual(value['retrospective_reproduction']['verification_environment'], 'macos')
        self.assertEqual(value['retrospective_reproduction']['evidence_kind'],
                         'cross_environment_complementary_refits')
        self.assertEqual(len(value['retrospective_reproduction']['cross_environment_divergences']), 1)

    def test_modified_source_is_not_credited(self):
        (self.audit_dir / 'split_provenance_linux.json').write_text('modified!')
        self.assertEqual(self.check()['status'], 'reconciliation_bound_not_fit_attested')

    def test_modified_reference_sha_is_not_credited(self):
        self.entry['supporting_source_report_sha256'] = 'b'*64
        self.path.write_text(json.dumps(self.audit))
        self.assertEqual(self.check()['status'], 'reconciliation_bound_not_fit_attested')

    def test_comparability_marks_divergence_as_retroactive_not_fit_time(self):
        models = []
        for name in ('credit_logistic_r', 'credit_logistic_python',
                     'credit_xgboost_python', 'credit_lightgbm_python'):
            models.append({'dataset': 'uci', 'name': name, 'evidence': {
                'validation': {'rows': 6037, 'positives': 1351},
                'test': {'rows': 5982, 'positives': 1317},
                'split_provenance': {
                    'status': 'retrospective_fit_reproduced_artifact_verified',
                    'retrospective_reproduction': {
                        'cross_environment_divergences': [self.entry['cross_environment_divergences'][0]]
                        if name == 'credit_xgboost_python' else [],
                    },
                },
            }})
        # Need the other domain to avoid assumptions about optional empty inventory.
        d = s.comparability(models)
        self.assertEqual(d['uci']['status'],
                         'cross_environment_fits_reproduced_on_canonical_split_with_divergences')
        self.assertIn('retrospective', d['uci']['note'])


if __name__ == '__main__':
    unittest.main()
