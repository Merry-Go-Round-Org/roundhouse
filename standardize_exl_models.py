#!/usr/bin/env python3
"""Read-only, versioned EXL model-evidence standardization.

Does NOT fit, score or alter models or evaluation source files. Reports saved
observations and evidence quality, with comparability gates based on split
counts and optional verified split SHA-256 fingerprints.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

NAMES = (
    'credit_logistic_r', 'credit_logistic_python', 'credit_xgboost_python',
    'credit_lightgbm_python', 'fraud_logistic_r', 'fraud_logistic_python',
    'fraud_xgboost_python', 'fraud_lightgbm_python', 'credit_cox_python',
)
METRICS = ('roc_auc', 'gini', 'pr_auc', 'precision', 'recall', 'specificity',
           'f1', 'brier', 'log_loss', 'calibration_gap')

def get(obj, *keys):
    for key in keys:
        if not isinstance(obj, dict) or key not in obj:
            return None
        obj = obj[key]
    return obj

def first(d, *keys):
    for key in keys:
        v = d.get(key)
        if v is not None:
            return v
    return None

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def read_json(path):
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding='utf-8'))

def normal_metric(part):
    p = part if isinstance(part, dict) else {}
    matrix = p.get('confusion_matrix') or {}
    uncertainty = first(p, 'uncertainty', 'bootstrap_uncertainty') or {}
    budget = p.get('alert_budget') or {}
    baseline = p.get('baseline_train_prevalence')
    if isinstance(baseline, dict):
        baseline = None  # documented separately; not a simple prevalence scalar
    return {
        'rows': first(p, 'rows', 'n'),
        'positives': first(p, 'positive_count', 'positives'),
        'prevalence': first(p, 'prevalence', 'positive_prevalence'),
        'roc_auc': p.get('roc_auc'),
        'gini': p.get('gini'),
        'pr_auc': first(p, 'pr_auc_average_precision_sklearn', 'pr_auc_average_precision'),
        'pr_auc_definition': 'sklearn.average_precision_score (non-interpolated)',
        'precision': p.get('precision'),
        'recall': p.get('recall'),
        'specificity': p.get('specificity'),
        'f1': p.get('f1'),
        'brier': p.get('brier'),
        'log_loss': p.get('log_loss'),
        'calibration_gap': p.get('calibration_gap_observed_minus_expected'),
        'threshold': first(p, 'threshold_selected_on_validation', 'threshold_validation_f1'),
        'confusion_matrix': {k: matrix.get(k) for k in ('tn', 'fp', 'fn', 'tp')},
        'bootstrap': {
            'method': uncertainty.get('method'),
            'resamples': first(uncertainty, 'valid_resamples', 'n_resamples'),
            'roc_auc_ci95': uncertainty.get('roc_auc_ci95'),
            'pr_auc_ci95': uncertainty.get('pr_auc_ci95'),
        },
        'calibration_bins_available': bool(first(p, 'calibration_fixed_probability_bins', 'calibration_bins')),
        'alert_budget': ({
            'rate': budget.get('rate'),
            'count': budget.get('count'),
            'precision': first(budget, 'precision_at_budget', 'precision'),
            'recall': first(budget, 'fraud_recall_at_budget', 'recall'),
        } if budget else None),
    }

def artifact_check(package, manifest):
    refs = manifest.get('artifact_paths') or []
    if isinstance(refs, str):
        refs = [refs]
    if not refs:
        return {'status': 'missing_reference', 'files': {}}
    outcomes = {}
    for ref in refs:
        if not isinstance(ref, str) or Path(ref).is_absolute() or '..' in Path(ref).parts:
            outcomes[str(ref)] = 'invalid_path'
            continue
        path = package / ref
        if not path.is_file():
            outcomes[ref] = 'missing'
        elif len(refs) != 1 or not manifest.get('artifact_sha256'):
            outcomes[ref] = 'unverifiable_hash'
        elif digest(path) == manifest['artifact_sha256']:
            outcomes[ref] = 'match'
        else:
            outcomes[ref] = 'mismatch'
    return {'status': 'match' if outcomes and all(x == 'match' for x in outcomes.values()) else 'failed',
            'files': outcomes}


def parity_evidence(models_dir, name):
    """Re-evaluate the committed R and Python development fixtures."""
    if not name.endswith('_r'):
        return {'status': 'not_applicable_independent_model'}
    domain = 'uci' if name.startswith('credit_') else 'ulb'
    base = models_dir / 'evidence' / 'r_parity'
    record = read_json(base / (domain + '_result.json'))
    if not isinstance(record, dict):
        return {'status': 'not_assessable_missing_evidence'}
    try:
        arrays = []
        for kind in ('r', 'python'):
            with (base / (domain + '_' + kind + '.csv')).open(newline='', encoding='utf-8-sig') as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != ['row_id', 'positive_probability']:
                    raise ValueError('Unexpected parity CSV columns')
                entries = list(reader)
            ids = [entry['row_id'] for entry in entries]
            probabilities = [float(entry['positive_probability']) for entry in entries]
            if not ids or len(ids) != len(set(ids)) or not all(ids):
                raise ValueError('Missing or repeated fixture row ID')
            if any(not math.isfinite(p) or p < 0 or p > 1 for p in probabilities):
                raise ValueError('Invalid fixture probability')
            arrays.append((ids, probabilities))
        (rids, rp), (pids, pp) = arrays
        if rids != pids:
            raise ValueError('Fixture row order differs')
        atol = float(record['tolerance_absolute'])
        rtol = float(record['tolerance_relative'])
        maxdiff = max(abs(a - b) for a, b in zip(rp, pp))
        if (record.get('passed') is not True or record.get('row_alignment') != 'exact_order'
            or record.get('parity_contract_version') != '1.0.0' or atol != 1e-8 or rtol != 0
            or len(rids) != int(record['n_rows']) or maxdiff > atol
            or abs(maxdiff - float(record['max_absolute_difference'])) > 1e-12):
            raise ValueError('Parity JSON and fixtures are inconsistent')
        return {'status': 'passed_development_fixture_only', 'rows_verified': len(rids),
                'max_absolute_difference': maxdiff, 'tolerance_absolute': atol,
                'tolerance_relative': rtol, 'row_alignment': 'exact_order',
                'kind': 'fitted_R_coefficients_replayed_in_Python'}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'status': 'failed_evidence_verification', 'detail': str(exc)}



def _corroborate_independent_replay(models_dir, name, manifest, result, artifact):
    """Retrospective reproduction evidence is distinct from old fit-time attestation."""
    if 'mismatch' in result.get('status', ''):
        return result
    domain = 'uci' if name.startswith('credit_') else 'ulb'
    audit = read_json(models_dir.parent / 'datasets/reports/provenance/split_provenance.json')
    if not isinstance(audit, dict) or audit.get('report_kind') != 'independent_split_provenance_reproduction':
        return result
    aggregation = audit.get('aggregation')
    if aggregation is not None:
        # Aggregated evidence is accepted only while both source audits remain
        # present and byte-identical to those strictly verified by the merger.
        if (not isinstance(aggregation, dict) or aggregation.get('status') != 'complete'
            or aggregation.get('kind') != 'two_environment_verified_artifact_replay'):
            return result
        sources = aggregation.get('source_reports') or {}
        expected_names = {'macos': 'split_provenance_macos.json',
                          'linux_amd64': 'split_provenance_linux.json'}
        if set(sources) != set(expected_names):
            return result
        source_root = models_dir.parent / 'datasets/reports/provenance'
        for tag, filename in expected_names.items():
            meta = sources.get(tag) or {}
            path = source_root / filename
            if (meta.get('filename') != filename or not path.is_file()
                or digest(path) != meta.get('sha256')):
                return result
    domain_data = (audit.get('domains') or {}).get(domain) or {}
    entry = (domain_data.get('models') or {}).get(name) or {}
    if aggregation is not None:
        env = entry.get('verification_environment')
        if (env not in ('macos', 'linux_amd64')
            or entry.get('supporting_source_report_sha256') !=
                aggregation['source_reports'][env]['sha256']):
            return result
    if (domain_data.get('split_definition_verification') != 'pass'
        or entry.get('status') != 'retrospectively_reproduced_artifact_verified'
        or (entry.get('fit_reproduction') or {}).get('status') != 'pass'
        or (entry.get('development_fixture_alignment') or {}).get('status') != 'match'
        or (entry.get('artifact') or {}).get('status') != 'match'
        or artifact['status'] != 'match'
        or entry.get('artifact_sha256') != manifest.get('artifact_sha256')):
        return result
    known = {
        'uci': ('e8feb21a0c57c5d6c8344af11aaaab079064f259e4861faad8f4aef40fe6d6cb',
                '30c6be3abd8dcfd3e6096c828bad8c2f011238620f5369220bd60cfc82700933',
                'uci/default of credit card clients.xls'),
        'ulb': ('5d9edf4df20e866d3198e74893fd2e1cf75f5fc3c5b4e175efd5a4a478b827fa',
                '76274b691b16a6c49d3f159c883398e03ccd6d1ee12d9d8ee38f4b4b98551a89',
                'ulb/creditcard.csv'),
    }[domain]
    expected = domain_data.get('canonical_split_sha256')
    if (expected != known[0] or entry.get('split_sha256') != known[0]
        or domain_data.get('data_sha256') != known[1]):
        return result
    original = models_dir.parent / 'datasets' / known[2]
    if not original.is_file() or digest(original) != known[1]:
        return result
    local_split = models_dir / 'evidence' / (domain + '_split_ids.csv')
    if local_split.is_file():
        if digest(local_split) != expected:
            result['status'] = 'local_split_hash_mismatch'
            return result
    elif domain == 'uci':
        return result
    result['status'] = 'retrospective_fit_reproduced_artifact_verified'
    result['retrospective_reproduction'] = {
        'status': 'passed',
        'fit_time_attestation': False,
        'canonical_split_sha256': expected,
        'max_abs_fixture_difference': (entry.get('fit_reproduction') or {}).get('max_abs_fixture_difference'),
        'max_abs_parameter_difference': (entry.get('fit_reproduction') or {}).get('max_abs_parameter_difference'),
        'source': 'datasets/reports/provenance/split_provenance.json',
        'verification_environment': entry.get('verification_environment'),
        'cross_environment_divergences': entry.get('cross_environment_divergences') or [],
        'evidence_kind': ('cross_environment_complementary_refits' if aggregation is not None
                          else 'single_environment_refit'),
    }
    return result


def reconciliation_artifact_binding(name, manifest, target_hash, artifact):
    """Preserve historical reconciliation SHA when validating a packaging-only RDS successor.

    Manifest compaction metadata is a claim requiring fresh independent replay,
    not proof of historical fit-time provenance or regulatory approval.
    """
    if artifact.get('status') != 'match' or not isinstance(target_hash, str):
        return None
    if manifest.get('artifact_sha256') == target_hash:
        return 'original_reconciled_artifact'
    if name != 'credit_logistic_r':
        return None
    compaction = manifest.get('artifact_compaction')
    if not isinstance(compaction, dict):
        return None
    difference = compaction.get('max_absolute_prediction_difference')
    if (compaction.get('format') != 'glm_coefficients_only_v1'
        or compaction.get('original_artifact_sha256') != target_hash
        or compaction.get('retrained') is not False
        or compaction.get('development_rows_checked') != 100
        or not isinstance(difference, (int, float)) or isinstance(difference, bool)
        or not math.isfinite(difference) or difference < 0 or difference > 1e-12):
        return None
    return 'manifest_declared_compaction_requires_fresh_validation'


def split_provenance(models_dir, name, manifest, evaluation, artifact):
    """Bind UCI reconciliation to artifact hashes, without inventing fit-time attestations."""
    domain = 'uci' if name.startswith('credit_') else 'ulb'
    at_fit = first(manifest, 'frozen_split_sha256', 'split_sha256') or first(
        evaluation, 'frozen_split_sha256', 'split_sha256')
    result = {'status': 'fit_time_recorded' if at_fit else 'not_attested_at_fit',
              'fit_time_sha256': at_fit, 'reconciliation_sha256': None,
              'local_split_matches': None}
    if domain != 'uci':
        return _corroborate_independent_replay(models_dir, name, manifest, result, artifact)
    rec = read_json(models_dir / 'UCI_SPLIT_RECONCILIATION.json')
    if not isinstance(rec, dict) or not isinstance(rec.get('frozen_split_sha256'), str):
        return result
    target_hash = (rec.get('baseline_r_glm_artifact_sha256') if name.endswith('_r') else
                   (rec.get('retrained_python_artifacts') or {}).get(name))
    binding = reconciliation_artifact_binding(name, manifest, target_hash, artifact)
    if (binding is None or any(first(evaluation.get(part) or {}, 'rows', 'n') != (rec.get('split_counts', {}).get(part) or {}).get('rows')
                               or first(evaluation.get(part) or {}, 'positive_count', 'positives') != (rec.get('split_counts', {}).get(part) or {}).get('positives')
                               for part in ('validation', 'test'))):
        result['status'] = 'reconciliation_artifact_or_counts_mismatch'
        return result
    result['reconciliation_artifact_binding'] = binding
    if binding != 'original_reconciled_artifact':
        result['packaging_only_successor_requires_fresh_provenance'] = True
    expected = rec['frozen_split_sha256']
    result['reconciliation_sha256'] = expected
    path = models_dir / 'evidence' / 'uci_split_ids.csv'
    result['local_split_matches'] = digest(path) == expected if path.is_file() else None
    if result['local_split_matches'] is False:
        result['status'] = 'local_split_hash_mismatch'
    elif at_fit is not None and at_fit != expected:
        result['status'] = 'fit_time_vs_reconciliation_mismatch'
    elif at_fit == expected and result['local_split_matches'] is True:
        result['status'] = 'fit_time_and_reconciliation_sha_verified'
    else:
        result['status'] = 'reconciliation_bound_not_fit_attested'
    return _corroborate_independent_replay(models_dir, name, manifest, result, artifact)

def inventory(models_dir):
    output = []
    for name in NAMES:
        package = models_dir / name
        errors = []
        try:
            m = read_json(package / 'manifest.json')
            e = read_json(package / 'evaluation.json')
        except (ValueError, OSError) as ex:
            m, e = None, None
            errors.append(f'invalid_json: {ex}')
        if m is None:
            errors.append('manifest_missing')
            m = {}
        if e is None:
            errors.append('evaluation_missing')
            e = {}
        synthetic = name == 'credit_cox_python'
        domain = 'survival_synthetic' if synthetic else ('uci' if name.startswith('credit_') else 'ulb')
        binary = not synthetic
        artifact = artifact_check(package, m)
        if artifact['status'] != 'match':
            errors.append('artifact_integrity_not_confirmed')
        fairness = read_json(package / 'fairness.json') if domain == 'uci' else None
        inline_groups = e.get('fairness_test_descriptive') if domain == 'uci' else None
        external_groups = fairness.get('rows') if isinstance(fairness, dict) else None
        valid_external = external_groups if isinstance(external_groups, list) and all(
            isinstance(g, dict) and isinstance(g.get('group'), dict) for g in external_groups) else []
        valid_inline = inline_groups if isinstance(inline_groups, list) and all(
            isinstance(g, dict) and isinstance(g.get('group'), dict) for g in inline_groups) else []
        selected_groups = valid_external or valid_inline
        if synthetic:
            evidence = {'c_index': e.get('c_index'), 'horizon': e.get('horizon'),
                        'time_unit': e.get('time_unit'), 'event_counts': e.get('events'),
                        'synthetic_only': True}
        else:
            evidence = {
                'validation': normal_metric(e.get('validation')),
                'test': normal_metric(e.get('test')),
                'split_reference': first(e, 'split_reference', 'split_file') or first(m, 'split_definition', 'split_reference'),
                'split_sha256_recorded': first(m, 'frozen_split_sha256', 'split_sha256') or first(e, 'frozen_split_sha256', 'split_sha256'),
                'split_provenance': split_provenance(models_dir, name, m, e, artifact),
                'fairness': {
                    'status': ('not_assessable_no_demographics' if domain == 'ulb' else
                               'descriptive_available' if selected_groups else 'not_assessable_missing_evidence'),
                    'group_count': len(selected_groups),
                    'unstable_groups': sum(bool(g.get('unstable', False)) for g in selected_groups),
                    'source': 'fairness.json' if valid_external else ('evaluation.json' if valid_inline else None),
                },
                'parity': parity_evidence(models_dir, name),
            }
            for p in ('validation', 'test'):
                values = evidence[p]
                if values['rows'] is None or values['positives'] is None:
                    errors.append(p + '_sample_counts_missing')
        output.append({
            'name': name,
            'dataset': domain,
            'task': 'survival_analysis' if synthetic else 'binary_classification',
            'model_kind': 'synthetic_demo' if synthetic else 'real_dataset',
            'algorithm': m.get('algorithm') or e.get('algorithm'),
            'artifact_integrity': artifact,
            'status': m.get('status', 'unknown'),
            'evidence': evidence,
            'errors': errors,
        })
    return output

def comparability(models):
    groups = {}
    for domain in ('uci', 'ulb'):
        members = [m for m in models if m['dataset'] == domain]
        partitions = {}
        for item in members:
            ev = item['evidence']
            partitions[item['name']] = {
                part: {k: ev[part][k] for k in ('rows', 'positives')}
                for part in ('validation', 'test')}
        counts = [json.dumps(x, sort_keys=True) for x in partitions.values()]
        same = len(set(counts)) == 1 and all('null' not in x for x in counts)
        proofs = [m['evidence'].get('split_provenance', {}) for m in members]
        fit_shas = [p.get('fit_time_sha256') for p in proofs]
        fit_time_same = same and all(fit_shas) and len(set(fit_shas)) == 1
        retro_same = same and len(members) == 4 and all(
            p.get('status') == 'retrospective_fit_reproduced_artifact_verified' for p in proofs)
        platform_divergence = retro_same and any(
            (p.get('retrospective_reproduction') or {}).get('cross_environment_divergences')
            for p in proofs)
        reconciliation_supported = same and domain == 'uci' and all(
            p.get('status') in ('reconciliation_bound_not_fit_attested',
                                'fit_time_and_reconciliation_sha_verified') for p in proofs)
        bad = any(p.get('status') in ('local_split_hash_mismatch',
                                      'fit_time_vs_reconciliation_mismatch',
                                      'reconciliation_artifact_or_counts_mismatch') for p in proofs)
        groups[domain] = {
            'status': ('mismatch_not_comparable' if not same or bad else
                       'cross_environment_fits_reproduced_on_canonical_split_with_divergences'
                       if platform_divergence else
                       'retrospective_fits_reproduced_on_canonical_split' if retro_same else
                       'same_fit_time_fingerprint' if fit_time_same else
                       'reconciliation_supported_membership_unattested' if reconciliation_supported else
                       'counts_only_membership_unverified'),
            'partition_counts': partitions,
            'split_provenance': {m['name']: m['evidence'].get('split_provenance') for m in members},
            'note': (
                'Each model reproduced on the canonical training split in at least one verified environment. '
                'Cross-platform XGBoost refit divergences are documented; this is retrospective, '
                'not original fit-time SHA attestation.' if platform_divergence else
                'Independent train-only fits reproduce committed development scores on the canonical split; '
                'this is retrospective, not original fit-time SHA attestation.' if retro_same else
                'Matching counts do not prove identical IDs. Historical fit-time SHA attestation is absent.'
            ),
        }
    return groups

def display(v):
    return '—' if v is None else f'{v:.4f}' if isinstance(v, (float, int)) else str(v)

def markdown_report(models, compare):
    lines = ['# EXL Standardized Model Evidence', '',
             'Read-only normalization of saved evaluations; **no retraining or rescoring**. Not regulatory approval.', '',
             '## Split comparability', '']
    for domain, entry in compare.items():
        lines.append(f'- **{domain.upper()}**: {entry["status"]}.')
    lines += ['', '## Binary classification — saved final-test metrics', '',
              '| Model | ROC-AUC | PR-AUC (AP) | F1 | Recall | Brier | Integrity |',
              '|---|---:|---:|---:|---:|---:|---|']
    for m in models:
        if m['task'] != 'binary_classification':
            continue
        t = m['evidence']['test']
        lines.append('| ' + ' | '.join([m['name'], display(t['roc_auc']), display(t['pr_auc']),
                                           display(t['f1']), display(t['recall']), display(t['brier']),
                                           m['artifact_integrity']['status']]) + ' |')
    lines += ['', 'Metrics across the same domain should **not** be ranked while split status is `mismatch_not_comparable`.',
              'PR-AUC uses non-interpolated scikit-learn average precision.', '',
              '## Synthetic survival model (not comparable to binary AUC)', '']
    for m in models:
        if m['task'] == 'survival_analysis':
            v = m['evidence'].get('c_index') or {}
            lines.append(f'- **{m["name"]}**: train C-index {display(v.get("train"))}; '
                         f'validation {display(v.get("validation"))}; test {display(v.get("test"))}. '
                         'Synthetic demonstration only.')
    lines += ['', '## Evidence caveats', '',
              '- Artifact hashes verify local file integrity against manifests, not model validity or trusted provenance.',
              '- R coefficient-replay parity is separate from independently fitted Python logistic models.',
              '- Historical test labels were previously inspected; this is not a new untouched holdout.',
              '- ULB has no protected-class data; fairness is not assessable. UCI fairness is descriptive, not clearance.',
              '- Missing CIs, calibration bins, parity evidence, or exact row-ID split digests remain not assessable.',
              '- Do not commit raw data, ignored frozen split CSV, local backups or private test predictions.', '']
    return '\n'.join(lines)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--models-dir', default='legacy_models')
    parser.add_argument('--out', default='datasets/reports/standardized')
    parser.add_argument('--strict', action='store_true', help='Exit nonzero if artifacts fail or split mismatches remain')
    a = parser.parse_args()
    models = inventory(Path(a.models_dir))
    compare = comparability(models)
    result = {'report_version': '1.0.0', 'report_kind': 'read_only_saved_evidence_normalization',
              'models': models, 'comparability': compare}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'standardized_metrics.json').write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    with (out / 'standardized_metrics.csv').open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['model','dataset','kind','test_rows','test_positives','roc_auc','pr_auc','precision','recall','f1','brier','calibration_gap','integrity','fairness_status','split_status'])
        for m in models:
            ev=m['evidence']
            t=ev.get('test',{})
            writer.writerow([m['name'],m['dataset'],m['model_kind'],t.get('rows'),t.get('positives'),
                t.get('roc_auc'),t.get('pr_auc'),t.get('precision'),t.get('recall'),t.get('f1'),
                t.get('brier'),t.get('calibration_gap'),m['artifact_integrity']['status'],
                ev.get('fairness',{}).get('status','not_applicable'),compare.get(m['dataset'],{}).get('status','not_applicable')])
    (out / 'standardized_report.md').write_text(markdown_report(models,compare))
    for domain, item in compare.items():
        print(f'{domain.upper()} comparability: {item["status"]}')
    print(f'Created: {out}/standardized_metrics.json, standardized_metrics.csv, standardized_report.md')
    if a.strict and (any(m['errors'] for m in models) or any(x['status']=='mismatch_not_comparable' for x in compare.values()) or any(m['evidence'].get('parity', {}).get('status') == 'failed_evidence_verification' for m in models if m['task'] == 'binary_classification')):
        raise SystemExit('STRICT FAIL: inspect missing artifact evidence and/or split mismatches')

if __name__ == '__main__':
    main()
