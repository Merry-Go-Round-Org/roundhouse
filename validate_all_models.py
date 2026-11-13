#!/usr/bin/env python3
"""Read-only validation suite for EXL legacy model packages.

Saved metrics are cross-checked, NOT recomputed from hidden final-test labels.
Scoring execution is opt-in and must run only against locally trusted artifacts.
No training, no final-test input, no changes to model packages.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import platform
import shutil
import subprocess
import sys
import tempfile
from legacy_models.stability import evaluate_window, policy_config
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATUSES = {'PASS', 'FAIL', 'REVIEW', 'NOT_ASSESSABLE', 'NOT_APPLICABLE'}


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_standardizer(path):
    spec = importlib.util.spec_from_file_location('exl_standardizer', path)
    if spec is None or spec.loader is None:
        raise ValueError('Cannot load the existing standardize_exl_models.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(check_id, status, required, summary, kind, details=None):
    if status not in STATUSES:
        raise ValueError(f'Invalid check status: {status}')
    return {'check_id': check_id, 'status': status, 'required': required,
            'summary': summary, 'evidence_kind': kind, 'details': details or {}}


def finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def metric_issues(part, name, tolerance):
    issues = []
    rows, positives = part.get('rows'), part.get('positives')
    if not isinstance(rows, int) or rows <= 0 or not isinstance(positives, int) or not (0 < positives < rows):
        issues.append(f'{name}: invalid sample counts')
        return issues
    for key in ('roc_auc', 'gini', 'pr_auc', 'precision', 'recall', 'specificity',
                'f1', 'brier', 'log_loss', 'calibration_gap', 'threshold', 'prevalence'):
        if not finite(part.get(key)):
            issues.append(f'{name}: {key} is missing or non-finite')
    if issues:
        return issues
    for key in ('roc_auc', 'pr_auc', 'precision', 'recall', 'specificity', 'f1',
                'brier', 'threshold', 'prevalence'):
        if not 0 <= part[key] <= 1:
            issues.append(f'{name}: {key} outside [0,1]')
    if part['log_loss'] < 0 or not -1 <= part['gini'] <= 1 or not -1 <= part['calibration_gap'] <= 1:
        issues.append(f'{name}: invalid gini, log loss or calibration gap')
    if abs(part['gini'] - (2 * part['roc_auc'] - 1)) > tolerance:
        issues.append(f'{name}: gini and ROC-AUC disagree')
    if abs(part['prevalence'] - positives / rows) > tolerance:
        issues.append(f'{name}: prevalence and counts disagree')
    matrix = part.get('confusion_matrix') or {}
    values = [matrix.get(k) for k in ('tn', 'fp', 'fn', 'tp')]
    if any(not isinstance(x, int) or x < 0 for x in values):
        issues.append(f'{name}: invalid confusion matrix')
        return issues
    tn, fp, fn, tp = values
    if tn + fp + fn + tp != rows or tp + fn != positives:
        issues.append(f'{name}: confusion matrix totals disagree with cohort')
    def ratio(a, b):
        return a / b if b else 0.0
    derived = {'precision': ratio(tp, tp + fp), 'recall': ratio(tp, tp + fn),
               'specificity': ratio(tn, tn + fp),
               'f1': ratio(2 * tp, 2 * tp + fp + fn)}
    for metric, actual in derived.items():
        if abs(part[metric] - actual) > tolerance:
            issues.append(f'{name}: {metric} differs from confusion matrix')
    bootstrap = part.get('bootstrap') or {}
    for key in ('roc_auc_ci95', 'pr_auc_ci95'):
        ci = bootstrap.get(key)
        if ci is not None and (not isinstance(ci, list) or len(ci) != 2 or
                               any(not finite(x) or x < 0 or x > 1 for x in ci) or ci[0] > ci[1]):
            issues.append(f'{name}: invalid {key}')
    budget = part.get('alert_budget')
    if budget:
        if any(not finite(budget.get(key)) or not 0 <= budget[key] <= 1
               for key in ('rate', 'precision', 'recall')):
            issues.append(f'{name}: invalid alert budget rates')
        if not isinstance(budget.get('count'), int) or not 0 < budget['count'] <= rows:
            issues.append(f'{name}: invalid alert budget count')
    return issues


def csv_rows(path):
    with Path(path).open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        if fields is None or len(fields) != len(set(fields)):
            raise ValueError('Missing or duplicate CSV header')
        rows = list(reader)
    if not rows or any(None in row for row in rows):
        raise ValueError('Empty CSV or malformed row')
    return fields, rows


def compare_fixture(output, reference, required_columns, tolerance, input_csv=None):
    fields, actual = csv_rows(output)
    ref_fields, expected = csv_rows(reference)
    if fields != required_columns or ref_fields != required_columns:
        raise ValueError('Output/reference schema differs from the scoring contract')
    ids = [r['row_id'] for r in actual]
    if len(ids) != len(set(ids)) or any(not rid or not rid.strip() for rid in ids):
        raise ValueError('Output row_id is blank or duplicated')
    if ids != [r['row_id'] for r in expected]:
        raise ValueError('Output row IDs/order differ from reference')
    if input_csv is not None:
        input_fields, inputs = csv_rows(input_csv)
        if 'row_id' not in input_fields or ids != [r['row_id'] for r in inputs]:
            raise ValueError('Scorer output is not aligned with input row IDs/order')
    max_diff = 0.0
    for got, want in zip(actual, expected):
        a, b = float(got['positive_probability']), float(want['positive_probability'])
        if not finite(a) or not finite(b) or not 0 <= a <= 1 or not 0 <= b <= 1:
            raise ValueError('Non-finite or out-of-bounds probability')
        max_diff = max(max_diff, abs(a - b))
    if max_diff > tolerance:
        raise ValueError(f'Fixture difference {max_diff:.4g} > {tolerance:.4g}')
    return {'rows': len(ids), 'max_absolute_difference': max_diff, 'tolerance_absolute': tolerance}


def command_for(package, language, input_path, output_path):
    script = package / ('score.R' if language == 'R' else 'score.py')
    if not script.is_file():
        raise FileNotFoundError(f'Missing scorer {script.name}')
    interpreter = shutil.which('Rscript') if language == 'R' else sys.executable
    if not interpreter:
        raise RuntimeError('Rscript is unavailable in this environment')
    return [interpreter, str(script), '--input', str(input_path), '--output', str(output_path),
            '--package', str(package)] if language == 'R' else [
        interpreter, str(script), '--input', str(input_path), '--output', str(output_path)]


def invoke(package, language, input_path, output_path, timeout):
    cmd = command_for(package, language, input_path, output_path)
    return subprocess.run(cmd, cwd=package, text=True, capture_output=True,
                          timeout=timeout, check=False)


def negative_fixture(path, destination, kind, domain):
    fields, rows = csv_rows(path)
    if len(rows) < 2:
        raise ValueError('Need two fixture rows for negative tests')
    if kind == 'missing_row_id':
        fields = [f for f in fields if f != 'row_id']
        rows = [{f: row[f] for f in fields} for row in rows]
    elif kind == 'duplicated_row_id':
        rows[1]['row_id'] = rows[0]['row_id']
    elif kind == 'target_leakage':
        target = 'Class' if domain == 'ulb' else 'default payment next month'
        fields = [*fields, target]
        rows = [{**row, target: '0'} for row in rows]
    else:
        raise ValueError('Unknown negative test')
    with destination.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def execute_fixture_checks(package, language, domain, policy):
    fixture = package / 'fixtures' / 'development_inputs.csv'
    reference = package / 'fixtures' / 'reference_predictions.csv'
    if not fixture.is_file() or not reference.is_file():
        return [check('fixture_scoring', 'NOT_ASSESSABLE', True,
                      'Package has no saved development scoring fixtures', 'fresh_execution')]
    timeout = policy['scoring']['timeout_seconds']
    tol = policy['scoring']['absolute_tolerance']
    with tempfile.TemporaryDirectory(prefix='exl_validate_') as directory:
        work = Path(directory)
        output = work / 'output.csv'
        try:
            result = invoke(package, language, fixture, output, timeout)
            if result.returncode:
                missing_dependency = any(marker in result.stderr for marker in (
                    'ModuleNotFoundError', 'No module named', 'there is no package called',
                    'package is not available'))
                state = 'NOT_ASSESSABLE' if missing_dependency else 'FAIL'
                return [check('fixture_scoring', state, True,
                              'Required execution dependency unavailable' if missing_dependency
                              else 'Scorer exited nonzero on valid fixture', 'fresh_execution',
                              {'returncode': result.returncode, 'stderr_tail': result.stderr[-400:]})]
            result_info = compare_fixture(output, reference,
                                          ['row_id', 'positive_probability'], tol, fixture)
        except (OSError, ValueError, subprocess.TimeoutExpired) as ex:
            return [check('fixture_scoring', 'FAIL', True,
                          'Scoring output failed fixture contract', 'fresh_execution',
                          {'error': str(ex)[:400]})]
        except RuntimeError as ex:
            return [check('fixture_scoring', 'NOT_ASSESSABLE', True, str(ex), 'fresh_execution')]
        passed = [check('fixture_scoring', 'PASS', True,
                        'Trusted scorer reproduced the saved development fixture', 'fresh_execution', result_info)]
        for kind in ('missing_row_id', 'duplicated_row_id', 'target_leakage'):
            inp, out = work / f'{kind}.csv', work / f'{kind}_out.csv'
            try:
                negative_fixture(fixture, inp, kind, domain)
                attempt = invoke(package, language, inp, out, timeout)
                status = 'PASS' if attempt.returncode != 0 else 'FAIL'
                detail = {'returncode': attempt.returncode, 'negative_case': kind}
                message = 'Invalid input rejected' if status == 'PASS' else 'Scorer accepted invalid input'
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as ex:
                status, message, detail = 'FAIL', 'Could not complete negative-input test', {'error': str(ex)[:400]}
            passed.append(check('reject_' + kind, status, True, message, 'fresh_execution', detail))
        return passed


def schema_check(package, manifest):
    path = package / 'schema.json'
    try:
        schema = read_json(path)
        required = schema['input']['required']
        output = schema['output']['required']
        if 'row_id' not in required or any(f not in required for f in manifest['feature_order']):
            raise ValueError('Schema missing manifest features')
        if len(required) != len(set(required)):
            raise ValueError('Repeated required input fields')
        if output != ['row_id', 'positive_probability']:
            raise ValueError('Unexpected binary scoring output fields')
        properties = schema['output']['properties']['positive_probability']
        if properties.get('minimum') != 0 or properties.get('maximum') != 1:
            raise ValueError('Output probabilities must be bounded')
        return check('schema_contract', 'PASS', True, 'Manifest and binary scoring schema agree',
                     'static_contract')
    except (OSError, KeyError, TypeError, ValueError) as ex:
        return check('schema_contract', 'FAIL', True, 'Schema contract is inconsistent',
                     'static_contract', {'error': str(ex)[:400]})


def saved_artifact_inference_check(models_dir, name):
    """Recompute cross-OS saved-artifact score evidence; never accept a bare PASS."""
    location = models_dir.parent / 'datasets/reports/provenance'
    record = location / 'inference_portability.json'
    if not record.is_file():
        return check('cross_platform_saved_artifact_inference', 'NOT_ASSESSABLE', False,
                     'Identical saved-artifact scoring has not been compared across macOS and Linux',
                     'governance_limitation', {
                         'separate_from_training_refit': True,
                         'how_to_run': 'bash legacy_models/provenance/run_saved_artifact_inference.sh'})
    try:
        saved = read_json(record)
        if (saved.get('report_kind') != 'saved_artifact_inference_portability' or
            saved.get('schema_version') != '1.0.0' or
            saved.get('source_execution_environments') != ['macos', 'linux_amd64'] or
            saved.get('not_a_training_reproduction') is not True):
            raise ValueError('Unexpected or incomplete saved-artifact inference report')
        spec = importlib.util.spec_from_file_location(
            'exl_inference_comparison',
            models_dir / 'provenance/compare_saved_artifact_inference.py')
        if spec is None or spec.loader is None:
            raise ValueError('Inference comparison implementation unavailable')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rerun = module.compare(models_dir, location / 'inference_outputs')
        actual = rerun['models'][name]
        stored = (saved.get('models') or {}).get(name) or {}
        fields = ('artifact_sha256', 'development_fixture_sha256',
                  'rows', 'max_absolute_macos_vs_linux', 'max_absolute_macos_vs_reference',
                  'max_absolute_linux_vs_reference', 'tolerance_absolute',
                  'status', 'macos_output_sha256', 'linux_output_sha256',
                  'max_float32_ulp_distance_macos_vs_linux',
                  'max_float32_ulp_distance_macos_vs_reference',
                  'max_float32_ulp_distance_linux_vs_reference',
                  'ulp_analysis_is_diagnostic_only')
        if any(stored.get(field) != actual.get(field) for field in fields):
            raise ValueError('Saved inference report differs from recomputed predictions')
        numeric_status = actual['status']
        # The fixed numeric tolerance is not relaxed. An observed difference is
        # a governance REVIEW finding, distinct from the mandatory technical
        # validation gate. Retain FAIL in the numeric evidence, not as a fake PASS.
        governance_status = 'PASS' if numeric_status == 'PASS' else 'REVIEW'
        return check('cross_platform_saved_artifact_inference', governance_status, False,
                     'Same saved artifact meets exact development-fixture inference tolerance'
                     if numeric_status == 'PASS' else
                     'Cross-platform saved-artifact inference exceeds the fixed numeric tolerance; human review required',
                     'fresh_inference_comparison', {
                         'numeric_parity_status': numeric_status,
                         'comparison': actual,
                         'does_not_test_training_determinism': True,
                         'does_not_approve_deployment': True})
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return check('cross_platform_saved_artifact_inference', 'FAIL', False,
                     'Saved-artifact inference evidence is inconsistent or cannot be reproduced',
                     'fresh_inference_comparison', {'error': str(exc)[:400]})


def evaluate_model_psi(name, package, binary, policy, baselines_dir, windows_dir):
    """Optional new-window PSI; never use protected test metrics or development self-comparison."""
    if not binary:
        return None, [
            check('score_psi', 'NOT_APPLICABLE', False,
                  'Synthetic survival demo is outside binary PSI scope', 'applicability'),
            check('feature_psi', 'NOT_APPLICABLE', False,
                  'Synthetic survival demo has no monitored cohort', 'applicability'),
        ]
    if baselines_dir is None or windows_dir is None:
        message = ('No independent scored monitoring window and frozen DEVELOPMENT baseline '
                   'provided; saved test metrics and development fixtures are not a proxy')
        return None, [
            check('score_psi', 'NOT_ASSESSABLE', False, message, 'stability_missing_evidence'),
            check('feature_psi', 'NOT_ASSESSABLE', False, message, 'stability_missing_evidence'),
        ]
    reference = Path(baselines_dir) / name / 'baseline.json'
    window = Path(windows_dir) / name
    inputs, scores = window / 'inputs.csv', window / 'scores.csv'
    if not all(path.is_file() for path in (reference, inputs, scores)):
        message = 'Frozen baseline or scored monitoring CSVs are missing for this model'
        paths = {'baseline_exists': reference.is_file(), 'inputs_exist': inputs.is_file(),
                 'scores_exist': scores.is_file()}
        return None, [
            check('score_psi', 'NOT_ASSESSABLE', False, message, 'stability_missing_evidence', paths),
            check('feature_psi', 'NOT_ASSESSABLE', False, message, 'stability_missing_evidence', paths),
        ]
    try:
        baseline = read_json(reference)
        manifest = read_json(package / 'manifest.json')
        if baseline['policy'] != policy_config(policy.get('stability')):
            raise ValueError('Frozen stability policy differs from current approved validation policy')
        if baseline.get('feature_order') != manifest.get('feature_order'):
            raise ValueError('Frozen PSI features disagree with the current model manifest')
        result = evaluate_window(
            baseline=baseline, current_inputs=inputs, current_scores=scores,
            expected_model_id=name,
            expected_artifact_sha256=manifest['artifact_sha256'],
        )
        score = result['score']
        feature_results = result['features']
        statuses = {r['status'] for r in feature_results.values()}
        feature_status = ('NOT_ASSESSABLE' if 'NOT_ASSESSABLE' in statuses else
                          'REVIEW' if 'REVIEW' in statuses else 'PASS')
        score_check = check('score_psi', score['status'], False,
                            'Score PSI evaluated with frozen development bins; '
                            + score['band'], 'fresh_unlabelled_window_psi', {
                                'result': score, 'baseline_sha256': result['baseline_sha256'],
                                'revalidation_required': result['revalidation_required'],
                            })
        feature_check = check('feature_psi', feature_status, False,
                              'Per-feature PSI evaluated with identical frozen development bins; '
                              + result['feature_band'], 'fresh_unlabelled_window_psi', {
                                  'results': feature_results,
                                  'baseline_sha256': result['baseline_sha256'],
                                  'red_features': result['red_features'],
                                  'revalidation_required': result['revalidation_required'],
                              })
        return result, [score_check, feature_check]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Invalid/tampered monitoring evidence is FAIL, not an invented successful PSI.
        summary = 'PSI evidence invalid: check frozen checksum, model binding and row alignment'
        detail = {'error': str(exc)[:450]}
        return None, [
            check('score_psi', 'FAIL', False, summary, 'stability_integrity', detail),
            check('feature_psi', 'FAIL', False, summary, 'stability_integrity', detail),
        ]


def report_model(item, models_dir, comparability, policy, run_scorers,
                 psi_baselines_dir=None, psi_windows_dir=None):
    name, domain = item['name'], item['dataset']
    package = models_dir / name
    ev = item['evidence']
    binary = item['task'] == 'binary_classification'
    checks = []
    stability_evidence, stability_checks = evaluate_model_psi(
        name, package, binary, policy, psi_baselines_dir, psi_windows_dir)
    integrity = item['artifact_integrity']
    checks.append(check('artifact_integrity', 'PASS' if integrity['status'] == 'match' else 'FAIL',
                        True, 'Saved artifact matches manifest SHA-256' if integrity['status'] == 'match'
                        else 'Artifact hash missing or mismatched', 'fresh_file_hash', integrity))
    if binary:
        try:
            manifest = read_json(package / 'manifest.json')
            checks.append(schema_check(package, manifest))
        except (OSError, ValueError) as ex:
            checks.append(check('schema_contract', 'FAIL', True, 'Manifest could not be loaded',
                                'static_contract', {'error': str(ex)[:400]}))
        issues = item['errors'][:]
        for part in ('validation', 'test'):
            issues.extend(metric_issues(ev[part], part, policy['metric_consistency']['absolute_tolerance']))
        v, t = ev['validation'], ev['test']
        if finite(v.get('threshold')) and finite(t.get('threshold')) and abs(v['threshold'] - t['threshold']) > 1e-12:
            issues.append('Test threshold differs from validation-selected threshold')
        checks.append(check('saved_metric_consistency', 'FAIL' if issues else 'PASS', True,
                            'Saved metrics internally inconsistent' if issues else 'Saved validation/test metrics satisfy arithmetic invariants',
                            'saved_evaluation_crosscheck', {'issues': issues,
                                                           'does_not_recompute_test_metrics': True}))
        verification_path = models_dir / 'verification_evidence.json'
        if verification_path.is_file():
            try:
                historical = read_json(verification_path)
                match = next((entry for entry in historical.get('models', [])
                              if entry.get('package') == name), None)
                if match is not None:
                    old_sha = match.get('artifact_sha256')
                    current_sha = read_json(package / 'manifest.json').get('artifact_sha256')
                    stale = old_sha != current_sha
                    checks.append(check('historical_verification_lineage', 'REVIEW' if stale else 'PASS',
                                        False, 'Historical verification report refers to a different artifact; do not reuse its metrics'
                                        if stale else 'Saved verification hash still matches this artifact',
                                        'saved_historical_evidence', {'historical_artifact_sha256': old_sha,
                                                                      'current_artifact_sha256': current_sha}))
            except (OSError, ValueError, TypeError) as exc:
                checks.append(check('historical_verification_lineage', 'NOT_ASSESSABLE', False,
                                    'Historical verification report could not be parsed',
                                    'saved_historical_evidence', {'error': str(exc)[:250]}))
        compare_status = comparability[domain]['status']
        provenance_status = (ev.get('split_provenance') or {}).get('status')
        split_bad = ('mismatch' in compare_status or 'mismatch' in str(provenance_status))
        reproduced = (
            compare_status in (
                'retrospective_fits_reproduced_on_canonical_split',
                'cross_environment_fits_reproduced_on_canonical_split_with_divergences',
            )
            and provenance_status == 'retrospective_fit_reproduced_artifact_verified'
        )
        split_result = 'FAIL' if split_bad else 'PASS' if reproduced else 'REVIEW'
        checks.append(check(
            'split_provenance', split_result, True,
            'Split mismatch detected' if split_bad else
            'All four domain models independently reproduced on exact canonical training rows; historical fit-time SHA was not logged'
            if reproduced else 'Frozen split not independently reproduced for every model; do not claim identical fit membership',
            'retrospective_computational_reproduction' if reproduced else 'saved_split_evidence',
            {'comparability': compare_status,
             'model_split_provenance': ev.get('split_provenance'),
             'original_fit_time_attestation': False}
        ))
        # Governance disclosures are distinct from technical split reproduction:
        # old execution logs did not record every fit-time split fingerprint.
        recorded_sha = (ev.get('split_provenance') or {}).get('fit_time_sha256')
        checks.append(check(
            'historical_fit_time_attestation', 'REVIEW', False,
            'Historical fit-time SHA evidence is incomplete across the model portfolio; retrospective refits cannot recreate missing original execution logs',
            'governance_limitation', {
                'per_model_split_sha_recorded': bool(recorded_sha),
                'per_model_recorded_split_sha256': recorded_sha,
                'portfolio_fit_time_attestation_complete': False,
                'retrospective_reproduction_status': split_result,
                'historical_evidence_must_not_be_relabelled': True,
            }))
        divergences = ((ev.get('split_provenance') or {})
                       .get('retrospective_reproduction') or {}).get('cross_environment_divergences') or []
        checks.append(check(
            'cross_platform_refit', 'REVIEW' if divergences else 'NOT_APPLICABLE', False,
            'Saved-model training refits diverged in another verified operating environment; inspect deployment controls'
            if divergences else 'No documented cross-platform refit divergence for this model',
            'governance_limitation' if divergences else 'applicability', {
                'cross_environment_divergences': divergences,
                'inference_portability_is_a_separate_question': True,
            }))
        if name.endswith('xgboost_python'):
            checks.append(saved_artifact_inference_check(models_dir, name))
        fairness = ev.get('fairness') or {}
        fair_status = 'NOT_ASSESSABLE' if domain == 'ulb' or fairness.get('status', '').startswith('not_assessable') else 'REVIEW'
        checks.append(check('demographic_fairness', fair_status, False,
                            'ULB has no protected attributes' if domain == 'ulb' else
                            'UCI group metrics are descriptive, not formal fairness clearance',
                            'saved_fairness', fairness))
        parity = ev.get('parity') or {}
        if not isinstance(parity, dict):
            parity = {'status': 'not_assessable_legacy_evidence', 'raw_value': str(parity)}
        if name.endswith('_r'):
            parity_status = {'passed_development_fixture_only': 'PASS',
                             'failed_evidence_verification': 'FAIL'}.get(parity.get('status'), 'NOT_ASSESSABLE')
            checks.append(check('migration_parity', parity_status, True,
                                'R coefficient replay fixture evidence verified' if parity_status == 'PASS'
                                else 'R migration parity is not verified', 'saved_crossfile_fixture', parity))
        else:
            checks.append(check('migration_parity', 'NOT_APPLICABLE', False,
                                'Independent Python model, not R coefficient replay', 'applicability'))
        b = t.get('bootstrap') or {}
        uncertainty = bool(b.get('roc_auc_ci95') and b.get('pr_auc_ci95') and b.get('resamples'))
        checks.append(check('uncertainty_calibration', 'REVIEW' if uncertainty and t.get('calibration_bins_available') else 'NOT_ASSESSABLE',
                            False, 'Confidence intervals and calibration evidence available for human review' if uncertainty and t.get('calibration_bins_available')
                            else 'Calibration and/or confidence interval evidence incomplete',
                            'saved_evaluation', {'bootstrap': b, 'calibration_bins_available': t.get('calibration_bins_available')}))
        checks.append(check('out_of_time_stability', 'NOT_ASSESSABLE', False,
                            'No fresh independent post-training time cohort is validated in this suite',
                            'out_of_time_applicability'))
        if run_scorers and integrity['status'] == 'match':
            checks.extend(execute_fixture_checks(package, read_json(package / 'manifest.json').get('language'), domain, policy))
        else:
            checks.append(check('fixture_scoring', 'NOT_ASSESSABLE', True,
                                'Executable validation skipped; use --execute-scorers with trusted artifacts'
                                if not run_scorers else 'Artifact integrity failed: scorer not executed', 'fresh_execution'))
            for kind in ('missing_row_id', 'duplicated_row_id', 'target_leakage'):
                checks.append(check('reject_' + kind, 'NOT_ASSESSABLE', True,
                                    'Requires safe scorer execution', 'fresh_execution'))
    else:
        # The Cox artifact intentionally has no saved fixture or real cohort.
        checks.append(check('schema_contract', 'NOT_ASSESSABLE', False,
                            'Synthetic Cox demo uses a distinct survival schema', 'static_contract'))
        checks.append(check('synthetic_scope', 'REVIEW', False,
                            'C-index is synthetic, not real credit validation', 'saved_synthetic_demo',
                            {'c_index': ev.get('c_index'), 'events': ev.get('event_counts')}))
        checks.append(check('fixture_scoring', 'NOT_ASSESSABLE', False,
                            'No committed survival scoring fixture', 'fresh_execution'))
        checks.append(check('migration_parity', 'NOT_APPLICABLE', False,
                            'Cox demonstration is not an R migration', 'applicability'))
    checks.extend(stability_checks)
    if binary:
        required_ids = set(policy['required_binary_checks'])
        if name.endswith('_r'):
            required_ids.update(policy.get('required_extra_checks', {}).get('R_migration', []))
        actual_ids = {record['check_id'] for record in checks}
        if not required_ids.issubset(actual_ids):
            checks.append(check('policy_required_check_coverage', 'FAIL', True,
                                'Configured mandatory validation checks were not executed',
                                'validation_policy', {'missing_check_ids': sorted(required_ids - actual_ids)}))
        for record in checks:
            record['required'] = record['check_id'] in required_ids or record['check_id'] == 'policy_required_check_coverage'
    blockers = [x['check_id'] for x in checks if x['status'] == 'FAIL' or
                (x['required'] and x['status'] != 'PASS')]
    failures = [x for x in checks if x['status'] == 'FAIL']
    review = [x for x in checks if x['status'] in ('REVIEW', 'NOT_ASSESSABLE')]
    overall = 'VALIDATION_FAILED' if failures else 'VALIDATION_INCOMPLETE' if blockers else 'REVIEW_REQUIRED'
    return {'schema_version': '1.0.0', 'report_kind': 'legacy_model_validation',
            'model_id': name, 'domain': domain, 'task': item['task'],
            'model_kind': item['model_kind'], 'model_status': item['status'],
            'artifact_sha256': (read_json(package / 'manifest.json').get('artifact_sha256')
                                if (package / 'manifest.json').is_file() and
                                not any(x.startswith('invalid_json:') for x in item['errors']) else None),
            'checks': checks, 'stability': stability_evidence,
            'summary': {'overall_status': overall,
                                         'required_checks_passed': not bool(blockers),
                                         'blocking_check_ids': blockers,
                                         'review_check_ids': [x['check_id'] for x in review],
                                         'human_approval_granted': False},
            'saved_metrics': {'validation': ev['validation'], 'test': ev['test']} if binary else
                             {'c_index': ev.get('c_index'), 'synthetic_only': True},
            'limitations': ['Historical test set has previously been inspected; not a fresh holdout',
                            'Original fit-time split SHA was not recorded for every historical model; retrospective reproduction is not a contemporaneous attestation']
                           if domain == 'uci' else
                           ['ULB has no demographic fairness attributes; original fit-time split SHA was not recorded for every historical model']
                           if domain == 'ulb' else ['Synthetic demonstration only; not real credit validation']}


def render_summary(reports, comparison, generated_at):
    lines = ['# EXL Challenge 04 — Model Validation Suite v1', '',
             f'Generated (UTC): {generated_at}', '',
             '**Evidence, not model approval.** Test-set labels are not loaded or re-scored.',
             'Executable scorer checks use development fixtures only.', '',
             '## Validation outcomes', '',
             '| Model | Overall | Artifact | Scoring | Metrics | Migration parity | Fairness | Score PSI | Feature PSI |',
             '|---|---|---|---|---|---|---|---|---|']
    for r in reports:
        vals = {c['check_id']: c['status'] for c in r['checks']}
        def item(name):
            return vals.get(name, 'N/A')
        lines.append(f"| {r['model_id']} | {r['summary']['overall_status']} | {item('artifact_integrity')} | "
                     f"{item('fixture_scoring')} | {item('saved_metric_consistency')} | "
                     f"{item('migration_parity')} | {item('demographic_fairness')} | "
                     f"{item('score_psi')} | {item('feature_psi')} |")
    lines += ['', '## Cross-model split comparison', '']
    for domain, comp in comparison.items():
        lines.append(f"- **{domain.upper()}**: `{comp['status']}`. " + comp.get('note', ''))
    lines += ['', '## Governance limitations (not technical failure)', '',
              '- Historical fit-time split SHA attestation: REVIEW; retrospective reproductions are not original training logs.',
              '- Cross-platform XGBoost independent refits: REVIEW; neither mismatch is overridden by the combined success.',
              '- Saved-artifact inference portability is separate from refit reproducibility; NOT_ASSESSABLE until directly tested.',
              '- Governance findings do not grant human approval even when strict technical checks pass.', '']
    lines += ['', '## Saved final-test metrics (not fresh evaluation)', '',
              '| Model | ROC-AUC | AP (PR-AUC) | F1 | Recall | Brier |',
              '|---|---:|---:|---:|---:|---:|']
    def fmt(v):
        return '—' if v is None else f'{v:.4f}'
    for r in reports:
        if r['task'] == 'binary_classification':
            t = r['saved_metrics']['test']
            lines.append('| ' + ' | '.join([r['model_id'], fmt(t['roc_auc']), fmt(t['pr_auc']),
                                              fmt(t['f1']), fmt(t['recall']), fmt(t['brier'])]) + ' |')
    lines += ['', '## Interpretation / reviewer decision', '',
              '- `PASS` means the named, limited validation check passed; it is not a model-use approval.',
              '- `REVIEW` means evidence needs a human risk assessment or limitations remain.',
              '- `NOT_ASSESSABLE` means data or execution evidence is unavailable; never render it as green.',
              '- `NOT_APPLICABLE` is different from missing evidence.',
              '- PSI uses frozen development distributions and an independent scored current window; drift does not imply loss of accuracy.',
              '- Synthetic Cox C-index is excluded from binary ranking; ULB demographic fairness is unavailable.',
              '- A separate authenticated human governance review must decide any deployment.', '',
              '## Suggested frontend layout', '',
              'Use `validation_index.json` for inventory, per-model `validation_report.json` for tabs:',
              'Overview (overall + blockers), Performance (saved metrics/confusion),',
              'Execution (fixture/scorer checks), Migration (parity),',
              'Fairness & Calibration (applicability + uncertainty), and Evidence (provenance + limitations).',
              'Status and source kind must appear together; never label historical saved metrics as newly executed.', '']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models-dir', default='legacy_models')
    parser.add_argument('--policy', default='legacy_models/validation_policy.json')
    parser.add_argument('--out', default='datasets/reports/validation')
    parser.add_argument('--execute-scorers', action='store_true',
                        help='OPT IN: locally execute trusted, hash-verified joblib/RDS scorers')
    parser.add_argument('--strict', action='store_true', help='Exit nonzero if required checks are incomplete or failed')
    parser.add_argument('--psi-baselines-dir', type=Path, default=None,
                        help='Immutable development PSI baseline JSONs, named MODEL_ID/baseline.json')
    parser.add_argument('--psi-windows-dir', type=Path, default=None,
                        help='Scored monitoring windows, MODEL_ID/inputs.csv and scores.csv')
    args = parser.parse_args(argv)
    if (args.psi_baselines_dir is None) != (args.psi_windows_dir is None):
        parser.error('Both --psi-baselines-dir and --psi-windows-dir are required together')
    models_dir = Path(args.models_dir).resolve()
    policy = read_json(args.policy)
    if policy.get('policy_version') != '1.0.0':
        parser.error('Unknown validation policy version')
    standardizer = load_standardizer(ROOT / 'standardize_exl_models.py')
    models = standardizer.inventory(models_dir)
    compare = standardizer.comparability(models)
    generated_at = datetime.now(timezone.utc).isoformat()
    reports = [report_model(model, models_dir, compare, policy, args.execute_scorers,
                            args.psi_baselines_dir, args.psi_windows_dir)
               for model in models]
    for report in reports:
        report['generated_at_utc'] = generated_at
        report['policy_version'] = policy['policy_version']
        report['execution_requested'] = args.execute_scorers
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for report in reports:
        directory = out / report['model_id']
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'validation_report.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    summary = {'schema_version': '1.0.0', 'report_kind': 'legacy_model_validation_index',
               'generated_at_utc': generated_at, 'policy_version': policy['policy_version'],
               'execution_requested': args.execute_scorers, 'comparability': compare,
               'models': [{'model_id': r['model_id'], 'overall_status': r['summary']['overall_status'],
                           'required_checks_passed': r['summary']['required_checks_passed'],
                           'stability_band': (r['stability']['band'] if r['stability'] else
                                              'not_applicable' if r['task'] != 'binary_classification'
                                              else 'not_assessable'),
                           'revalidation_required': bool(r['stability'] and r['stability']['revalidation_required']),
                           'report_path': r['model_id'] + '/validation_report.json'} for r in reports],
               'no_human_approvals_issued': True}
    (out / 'validation_index.json').write_text(json.dumps(summary, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    (out / 'validation_summary.md').write_text(render_summary(reports, compare, generated_at), encoding='utf-8')
    count = {key: sum(r['summary']['overall_status'] == key for r in reports)
             for key in ('VALIDATION_FAILED', 'VALIDATION_INCOMPLETE', 'REVIEW_REQUIRED')}
    print('Model validation report generated:', out)
    print('Result counts:', count)
    if args.strict and any(not r['summary']['required_checks_passed'] for r in reports):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())