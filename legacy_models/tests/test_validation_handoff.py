"""Protect backend/frontend report handoff against mismatched or malicious evidence."""
from __future__ import annotations
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'integration' / 'build_validation_handoff.py'
spec = importlib.util.spec_from_file_location('exl_validation_handoff', MODULE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class ValidationHandoffTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.root = Path(self.d.name)
        self.report = {
            'schema_version': '1.0.0', 'report_kind': 'legacy_model_validation',
            'generated_at_utc': '2026-11-04T20:00:00+00:00', 'policy_version': '1.0.0',
            'execution_requested': True, 'model_id': 'credit_logistic_r',
            'domain': 'uci', 'task': 'binary_classification', 'model_kind': 'real_dataset',
            'artifact_sha256': 'a'*64, 'checks': [
                {'check_id':'artifact_integrity', 'status':'PASS', 'required':True,
                 'summary':'Matched', 'evidence_kind':'fresh_file_hash', 'details':{}},
                {'check_id':'split_provenance', 'status':'REVIEW', 'required':False,
                 'summary':'Partial', 'evidence_kind':'saved_split_evidence', 'details':{}}],
            'summary':{'overall_status':'REVIEW_REQUIRED', 'required_checks_passed':True,
                       'blocking_check_ids':[], 'review_check_ids':['split_provenance'],
                       'human_approval_granted':False},
            'saved_metrics':{'test':{'roc_auc':0.7,'rows':100,'positives':20}},
            'limitations':['No current temporal holdout']}
        self.index = {'schema_version':'1.0.0', 'report_kind':'legacy_model_validation_index',
                      'generated_at_utc':self.report['generated_at_utc'],
                      'policy_version':'1.0.0','execution_requested':True,
                      'no_human_approvals_issued':True,'comparability':{'uci':{'status':'review'}},
                      'models':[{'model_id':'credit_logistic_r',
                                 'overall_status':'REVIEW_REQUIRED','required_checks_passed':True,
                                 'report_path':'credit_logistic_r/validation_report.json'}]}
        self.write()

    def tearDown(self):
        self.d.cleanup()

    def write(self):
        path = self.root/'credit_logistic_r'
        path.mkdir(exist_ok=True)
        (path/'validation_report.json').write_text(json.dumps(self.report))
        (self.root/'validation_index.json').write_text(json.dumps(self.index))

    def test_handoff_preserves_governance_status(self):
        data = mod.build(self.root)
        r=data['models'][0]
        self.assertEqual(r['overall_status'],'REVIEW_REQUIRED')
        self.assertFalse(r['human_approval_granted'])
        self.assertEqual(r['performance']['source_kind'],'saved_evaluation_crosscheck')
        self.assertIs(r['performance']['new_holdout_evaluation'],False)
        self.assertEqual(r['checks'][1]['status'],'REVIEW')

    def test_blocks_report_path_traversal(self):
        self.index['models'][0]['report_path']='../../elsewhere.json'
        self.write()
        with self.assertRaises(mod.ContractError): mod.build(self.root)

    def test_blocks_report_identity_mismatch(self):
        self.report['model_id']='fraud_logistic_r'
        self.write()
        with self.assertRaises(mod.ContractError): mod.build(self.root)

    def test_blocks_auto_approval(self):
        self.report['summary']['human_approval_granted']=True
        self.write()
        with self.assertRaises(mod.ContractError): mod.build(self.root)

    def test_blocks_false_pass_from_nonpass_required_check(self):
        self.report['checks'][0]['status']='NOT_ASSESSABLE'
        self.write()
        with self.assertRaises(mod.ContractError): mod.build(self.root)

    def test_blocks_mismatched_execution_mode(self):
        self.report['execution_requested']=False
        self.write()
        with self.assertRaises(mod.ContractError): mod.build(self.root)

    def test_blocks_duplicate_check_ids(self):
        self.report['checks'].append(copy.deepcopy(self.report['checks'][0]))
        self.write()
        with self.assertRaises(mod.ContractError): mod.build(self.root)


if __name__ == '__main__':
    unittest.main()
