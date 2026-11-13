"""Tests require only the Python standard library (no live models or datasets)."""
import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[2] / 'validate_all_models.py'
spec = importlib.util.spec_from_file_location('validation_suite_under_test', MODULE_PATH)
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)


class ValidationSuiteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def fixture(self):
        f = self.root / 'development_inputs.csv'
        q = self.root / 'reference_predictions.csv'
        f.write_text('row_id,x\na,1\nb,2\n')
        q.write_text('row_id,positive_probability\na,0.1\nb,0.2\n')
        return f, q

    def test_metric_arithmetic_clean(self):
        data = {'rows': 10, 'positives': 4, 'prevalence': 0.4,
                'roc_auc': 0.75, 'gini': 0.5, 'pr_auc': 0.5,
                'precision': 0.75, 'recall': 0.75,
                'specificity': 5/6, 'f1': 0.75, 'brier': 0.2,
                'log_loss': 0.6, 'calibration_gap': 0.01, 'threshold': 0.3,
                'confusion_matrix': {'tn': 5, 'fp': 1, 'fn': 1, 'tp': 3},
                'bootstrap': {}}
        self.assertEqual(v.metric_issues(data, 'test', 1e-6), [])
        data['confusion_matrix']['tp'] = 2
        self.assertTrue(v.metric_issues(data, 'test', 1e-6))

    def test_metric_rejects_infinite_and_inconsistent(self):
        part = {'rows': 10, 'positives': 2, 'roc_auc': float('nan')}
        self.assertTrue(v.metric_issues(part, 'test', 1e-6))

    def test_fixture_comparison_exact_ids(self):
        f, reference = self.fixture()
        output = self.root / 'output.csv'
        output.write_text(reference.read_text())
        self.assertEqual(v.compare_fixture(output, reference,
                                           ['row_id', 'positive_probability'], 1e-10)['rows'], 2)
        output.write_text('row_id,positive_probability\nb,0.1\na,0.2\n')
        with self.assertRaises(ValueError):
            v.compare_fixture(output, reference, ['row_id', 'positive_probability'], 1e-10)

    def test_fixture_checks_input_order_not_just_reference(self):
        fixture, reference = self.fixture()
        output = self.root / 'output.csv'
        # Both reference and output are in an order different from input.
        reference.write_text('row_id,positive_probability\nb,0.2\na,0.1\n')
        output.write_text(reference.read_text())
        with self.assertRaises(ValueError):
            v.compare_fixture(output, reference,
                              ['row_id', 'positive_probability'], 1e-10, fixture)

    def test_fixture_detects_tampering(self):
        _, reference = self.fixture()
        output = self.root / 'output.csv'
        output.write_text('row_id,positive_probability\na,0.1\nb,0.3\n')
        with self.assertRaises(ValueError):
            v.compare_fixture(output, reference, ['row_id', 'positive_probability'], 1e-10)

    def test_negative_fixture_construction(self):
        fixture, _ = self.fixture()
        for kind in ('missing_row_id', 'duplicated_row_id', 'target_leakage'):
            out = self.root / (kind + '.csv')
            v.negative_fixture(fixture, out, kind, 'uci')
            fields, rows = v.csv_rows(out)
            if kind == 'missing_row_id':
                self.assertNotIn('row_id', fields)
            elif kind == 'duplicated_row_id':
                self.assertEqual(rows[0]['row_id'], rows[1]['row_id'])
            else:
                self.assertIn('default payment next month', fields)

    def test_schema_contract(self):
        pkg = self.root / 'model'
        pkg.mkdir()
        manifest = {'feature_order': ['x']}
        schema = {'input': {'required': ['row_id', 'x']},
                  'output': {'required': ['row_id', 'positive_probability'],
                             'properties': {'positive_probability': {'minimum': 0, 'maximum': 1}}}}
        (pkg / 'schema.json').write_text(json.dumps(schema))
        self.assertEqual(v.schema_check(pkg, manifest)['status'], 'PASS')
        manifest['feature_order'].append('y')
        self.assertEqual(v.schema_check(pkg, manifest)['status'], 'FAIL')

    def test_executable_scorer_and_negative_cases(self):
        pkg = self.root / 'model'
        fixtures = pkg / 'fixtures'
        fixtures.mkdir(parents=True)
        (fixtures / 'development_inputs.csv').write_text('row_id,x\na,1\nb,2\n')
        (fixtures / 'reference_predictions.csv').write_text(
            'row_id,positive_probability\na,0.1\nb,0.2\n')
        (pkg / 'score.py').write_text('''
import argparse,csv,sys
p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output');a=p.parse_args()
with open(a.input) as f:
 r=csv.DictReader(f);fields=r.fieldnames;rows=list(r)
if fields!=['row_id','x'] or len(set(z['row_id'] for z in rows))!=len(rows):sys.exit(2)
with open(a.output,'w') as f:
 w=csv.writer(f);w.writerow(['row_id','positive_probability'])
 for z in rows:w.writerow([z['row_id'],float(z['x'])/10])
''')
        checks = v.execute_fixture_checks(pkg, 'Python', 'uci',
             {'scoring': {'timeout_seconds': 10, 'absolute_tolerance': 1e-10}})
        self.assertEqual(len(checks), 4)
        self.assertTrue(all(c['status'] == 'PASS' for c in checks), checks)

    def test_missing_runtime_is_not_pass(self):
        pkg = self.root / 'model'
        f = pkg / 'fixtures'
        f.mkdir(parents=True)
        (f / 'development_inputs.csv').write_text('row_id,x\na,1\nb,2\n')
        (f / 'reference_predictions.csv').write_text('row_id,positive_probability\na,0.1\nb,0.2\n')
        checks = v.execute_fixture_checks(pkg, 'R', 'uci',
              {'scoring': {'timeout_seconds': 10, 'absolute_tolerance': 1e-10}})
        self.assertNotEqual(checks[0]['status'], 'PASS')


if __name__ == '__main__':
    unittest.main()