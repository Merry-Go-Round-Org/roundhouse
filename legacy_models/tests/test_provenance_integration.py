"""Regression tests for standardiser ingestion of independently reproduced provenance."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / 'standardize_exl_models.py').is_file():
    ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('standardizer_under_test', ROOT / 'standardize_exl_models.py')
s = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(s)

UCI = 'e8feb21a0c57c5d6c8344af11aaaab079064f259e4861faad8f4aef40fe6d6cb'
UCI_DATA = '30c6be3abd8dcfd3e6096c828bad8c2f011238620f5369220bd60cfc82700933'
ULB = '5d9edf4df20e866d3198e74893fd2e1cf75f5fc3c5b4e175efd5a4a478b827fa'
ULB_DATA = '76274b691b16a6c49d3f159c883398e03ccd6d1ee12d9d8ee38f4b4b98551a89'


class ReplayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.models = self.root / 'legacy_models'
        (self.models / 'evidence').mkdir(parents=True)
        self.artifact_sha = 'b' * 64
        self.audit = self.root / 'datasets/reports/provenance/split_provenance.json'
        self.audit.parent.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, domain, name):
        split_sha, dataset_sha = (UCI, UCI_DATA) if domain == 'uci' else (ULB, ULB_DATA)
        self.audit.write_text(json.dumps({
            'report_kind': 'independent_split_provenance_reproduction',
            'domains': {domain: {
                'split_definition_verification': 'pass',
                'canonical_split_sha256': split_sha, 'data_sha256': dataset_sha,
                'models': {name: {
                    'status': 'retrospectively_reproduced_artifact_verified',
                    'artifact_sha256': self.artifact_sha, 'split_sha256': split_sha,
                    'artifact': {'status': 'match'},
                    'development_fixture_alignment': {'status': 'match'},
                    'fit_reproduction': {'status': 'pass', 'max_abs_fixture_difference': 1e-14},
                }},
            }},
        }))
        data = self.root / 'datasets' / ('uci/default of credit card clients.xls' if domain == 'uci' else 'ulb/creditcard.csv')
        data.parent.mkdir(parents=True, exist_ok=True)
        data.write_bytes(b'original raw file for unit test; checksum mocked')
        split = self.models / 'evidence' / (domain + '_split_ids.csv')
        split.write_text('row_id,split\n')
        return split_sha, dataset_sha

    def test_replay_is_adopted_only_when_evidence_and_hashes_match(self):
        sha, data_sha = self.report('uci', 'credit_logistic_r')
        result = {'status': 'reconciliation_bound_not_fit_attested'}
        with patch.object(s, 'digest', side_effect=lambda p: sha if p.name.endswith('_split_ids.csv') else data_sha):
            fixed = s._corroborate_independent_replay(
                self.models, 'credit_logistic_r', {'artifact_sha256': self.artifact_sha},
                result.copy(), {'status': 'match'})
        self.assertEqual(fixed['status'], 'retrospective_fit_reproduced_artifact_verified')
        self.assertIs(fixed['retrospective_reproduction']['fit_time_attestation'], False)
        with patch.object(s, 'digest', return_value='0' * 64):
            tampered = s._corroborate_independent_replay(
                self.models, 'credit_logistic_r', {'artifact_sha256': self.artifact_sha},
                result.copy(), {'status': 'match'})
        self.assertNotEqual(tampered['status'], 'retrospective_fit_reproduced_artifact_verified')

    def test_no_false_upgrade_for_unverified_artifact_or_failed_replay(self):
        self.report('ulb', 'fraud_logistic_python')
        original = {'status': 'not_attested_at_fit'}
        check = s._corroborate_independent_replay(self.models, 'fraud_logistic_python',
                          {'artifact_sha256': self.artifact_sha}, original.copy(), {'status': 'failed'})
        self.assertEqual(check, original)
        entry = json.loads(self.audit.read_text())
        entry['domains']['ulb']['models']['fraud_logistic_python']['fit_reproduction']['status'] = 'mismatch'
        self.audit.write_text(json.dumps(entry))
        check = s._corroborate_independent_replay(self.models, 'fraud_logistic_python',
                          {'artifact_sha256': self.artifact_sha}, original.copy(), {'status': 'match'})
        self.assertEqual(check, original)

    def test_comparability_requires_all_four_model_reproductions(self):
        names = ['credit_logistic_r', 'credit_logistic_python', 'credit_xgboost_python', 'credit_lightgbm_python']
        def model(name, status):
            return {'dataset': 'uci', 'name': name, 'evidence': {
                'validation': {'rows': 6037, 'positives': 1351},
                'test': {'rows': 5982, 'positives': 1317},
                'split_provenance': {'status': status}}}
        items = [model(n, 'retrospective_fit_reproduced_artifact_verified') for n in names]
        self.assertEqual(s.comparability(items)['uci']['status'],
                         'retrospective_fits_reproduced_on_canonical_split')
        items[-1]['evidence']['split_provenance']['status'] = 'not_attested_at_fit'
        self.assertEqual(s.comparability(items)['uci']['status'],
                         'counts_only_membership_unverified')

if __name__ == '__main__':
    unittest.main()