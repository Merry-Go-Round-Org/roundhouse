#!/usr/bin/env python3
"""Independent, non-mutating replay of EXL legacy model split provenance.

The output is retrospective computational evidence, NOT a fit-time attestation.
Never score or re-fit on held-out test rows. Only models and development fixtures
from a trusted checkout may be inspected. Full raw datasets stay local.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

SHAS = {
    'uci': {
        'data': '30c6be3abd8dcfd3e6096c828bad8c2f011238620f5369220bd60cfc82700933',
        'split': 'e8feb21a0c57c5d6c8344af11aaaab079064f259e4861faad8f4aef40fe6d6cb',
    },
    'ulb': {
        'data': '76274b691b16a6c49d3f159c883398e03ccd6d1ee12d9d8ee38f4b4b98551a89',
        'split': '5d9edf4df20e866d3198e74893fd2e1cf75f5fc3c5b4e175efd5a4a478b827fa',
    },
}
MODELS = {
    'uci': ('credit_logistic_r', 'credit_logistic_python', 'credit_xgboost_python', 'credit_lightgbm_python'),
    'ulb': ('fraud_logistic_r', 'fraud_logistic_python', 'fraud_xgboost_python', 'fraud_lightgbm_python'),
}
COUNTS = {
    'uci': {'train': (17981, 3968), 'validation': (6037, 1351), 'test': (5982, 1317)},
    'ulb': {'train': (162992, 356), 'validation': (61675, 59), 'test': (60140, 77)},
}


def sha256(path: Path) -> str:
    hash_ = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(1048576), b''):
            hash_.update(block)
    return hash_.hexdigest()


def json_file(path: Path) -> dict:
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError(f'Expected JSON object: {path}')
    return data


def artifact_status(package: Path, manifest: dict) -> dict:
    refs = manifest.get('artifact_paths')
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, list) or len(refs) != 1:
        return {'status': 'unverifiable_reference'}
    ref = refs[0]
    if not isinstance(ref, str) or Path(ref).is_absolute() or '..' in Path(ref).parts:
        return {'status': 'unsafe_path'}
    artifact = package / ref
    expected = manifest.get('artifact_sha256')
    if not isinstance(expected, str) or len(expected) != 64:
        return {'status': 'missing_manifest_sha'}
    if not artifact.is_file():
        return {'status': 'missing_artifact', 'expected_sha256': expected}
    with artifact.open('rb') as f:
        header = f.read(45)
    if header.startswith(b'version https://git-lfs.github.com/spec/'):
        return {'status': 'lfs_pointer_not_downloaded', 'expected_sha256': expected}
    actual = sha256(artifact)
    return {'status': 'match' if actual == expected else 'mismatch',
            'expected_sha256': expected, 'actual_sha256': actual}


def fixture_alignment(package: Path, df: pd.DataFrame, partitions: np.ndarray, feature_names: list[str]) -> dict:
    """Compare committed development fixture to the FIRST validation rows, including input values."""
    path = package / 'fixtures/development_inputs.csv'
    reference = package / 'fixtures/reference_predictions.csv'
    if not path.is_file() or not reference.is_file():
        return {'status': 'missing_fixture'}
    got = pd.read_csv(path, dtype={'row_id': str})
    expected = df.loc[partitions == 'validation', ['row_id', *feature_names]].head(len(got)).reset_index(drop=True)
    if not 0 < len(got) <= 100 or list(got.columns) != ['row_id', *feature_names]:
        return {'status': 'fixture_schema_mismatch'}
    if not got.row_id.equals(expected.row_id):
        return {'status': 'fixture_rows_not_first_validation'}
    try:
        x = got[feature_names].to_numpy(dtype=float)
        y = expected[feature_names].to_numpy(dtype=float)
    except (ValueError, TypeError):
        return {'status': 'fixture_nonnumeric'}
    if not np.isfinite(x).all() or not np.allclose(x, y, rtol=1e-12, atol=1e-10):
        return {'status': 'fixture_feature_mismatch'}
    scores = pd.read_csv(reference, dtype={'row_id': str})
    if list(scores.columns) != ['row_id', 'positive_probability'] or not scores.row_id.equals(got.row_id):
        return {'status': 'reference_ids_mismatch'}
    p = scores.positive_probability.to_numpy(dtype=float)
    if not np.isfinite(p).all() or (p < 0).any() or (p > 1).any():
        return {'status': 'invalid_reference_probabilities'}
    return {'status': 'match', 'rows': len(got)}


def refit_r_glm(df: pd.DataFrame, partitions: np.ndarray, features: list[str], target: str,
                coef_path: Path, fixture_path: Path, atol: float = 1e-8) -> dict:
    """Independent Python GLM with R-equivalent unpenalized binomial likelihood and sample SD."""
    import statsmodels.api as sm
    if not coef_path.is_file():
        return {'status': 'missing_r_coefficients'}
    saved = json_file(coef_path)
    if saved.get('feature_order') != features:
        return {'status': 'feature_order_mismatch'}
    mask = partitions == 'train'
    X = df.loc[mask, features].to_numpy(dtype=float)
    y = df.loc[mask, target].to_numpy(dtype=int)
    center, scale = X.mean(axis=0), X.std(axis=0, ddof=1)
    scale[~np.isfinite(scale) | (scale == 0)] = 1
    design = sm.add_constant((X - center) / scale, has_constant='add')
    result = sm.GLM(y, design, family=sm.families.Binomial()).fit(maxiter=150, tol=1e-9)
    if not result.converged or not np.isfinite(result.params).all():
        return {'status': 'fit_did_not_converge'}
    raw_coef = result.params[1:] / scale
    raw_intercept = float(result.params[0] - center @ raw_coef)
    recorded = np.array(saved['coefficients'], dtype=float)
    if recorded.shape != raw_coef.shape or not math.isfinite(float(saved['intercept'])):
        return {'status': 'invalid_stored_coefficients'}
    diff = np.abs(raw_coef - recorded)
    max_coeff = float(max(np.max(diff), abs(raw_intercept - float(saved['intercept']))))
    result_record = {'status': 'pass' if max_coeff <= atol else 'mismatch',
                     'method': 'independent_statsmodels_GLM_Binomial_R_sample_sd',
                     'max_abs_parameter_difference': max_coeff, 'tolerance_absolute': atol,
                     'train_rows': int(mask.sum()), 'train_positives': int(y.sum())}
    # Verify the independently refitted model also reproduces the R development scores.
    reference = pd.read_csv(fixture_path, dtype={'row_id': str})
    valid = df.loc[partitions == 'validation', ['row_id', *features]].head(len(reference))
    if reference.row_id.tolist() != valid.row_id.tolist():
        result_record['status'] = 'fixture_ids_mismatch'
        return result_record
    z = valid[features].to_numpy(dtype=float)
    from scipy.special import expit
    pred = expit(raw_intercept + z @ raw_coef)
    stored_prob = reference.positive_probability.to_numpy(dtype=float)
    max_prob = float(np.max(np.abs(pred - stored_prob)))
    result_record['max_abs_fixture_difference'] = max_prob
    if max_prob > atol:
        result_record['status'] = 'fixture_scores_mismatch'
    return result_record


def refit_python(df: pd.DataFrame, partitions: np.ndarray, features: list[str], target: str,
                 name: str, fixture_path: Path, tol: float = 1e-7) -> dict:
    """Freshly fit on train only; compare held-out DEVELOPMENT fixture scores, never test."""
    from pipeline import model_object
    algorithm = next(a for a in ('logistic', 'xgboost', 'lightgbm') if a in name)
    mask = partitions == 'train'
    model = model_object(algorithm, 'uci' if name.startswith('credit_') else 'ulb',
                         df.loc[mask, target].astype(int).to_numpy())
    model.fit(df.loc[mask, features], df.loc[mask, target].astype(int).to_numpy())
    fixture = pd.read_csv(fixture_path, dtype={'row_id': str})
    expected = df.loc[partitions == 'validation', ['row_id', *features]].head(len(fixture))
    if fixture.row_id.tolist() != expected.row_id.tolist():
        return {'status': 'fixture_ids_mismatch'}
    p = model.predict_proba(expected[features])[:, 1]
    ref_path = fixture_path.parent / 'reference_predictions.csv'
    ref = pd.read_csv(ref_path, dtype={'row_id': str})
    if ref.row_id.tolist() != expected.row_id.tolist():
        return {'status': 'reference_ids_mismatch'}
    differences = np.abs(p - ref.positive_probability.to_numpy(dtype=float))
    error = float(np.max(differences))
    return {'status': 'pass' if error <= tol else 'mismatch',
            'method': 'independent_refit_on_verified_train_split',
            'max_abs_fixture_difference': error,
            'mean_abs_fixture_difference': float(differences.mean()),
            'n_fixture_differences_above_tolerance': int((differences > tol).sum()),
            'tolerance_absolute': tol,
            'train_rows': int(mask.sum()),
            'train_positives': int(df.loc[mask, target].astype(int).sum())}


def audit_domain(domain: str, models_dir: Path, data_root: Path,
                 replay_python: bool, require_artifacts: bool) -> dict:
    shared = models_dir / 'shared'
    sys.path.insert(0, str(shared.resolve()))
    from pipeline import DATA, read_data, freeze_split
    source = data_root / ('uci/default of credit card clients.xls' if domain == 'uci' else 'ulb/creditcard.csv')
    result = {'dataset': domain, 'data_sha256': SHAS[domain]['data'],
              'canonical_split_sha256': SHAS[domain]['split'],
              'status': 'not_verified', 'models': {}}
    if not source.is_file():
        result['reason'] = 'raw_source_missing'
        return result
    if sha256(source) != SHAS[domain]['data']:
        result['reason'] = 'raw_source_sha_mismatch'
        return result
    data, _ = read_data(domain, source)
    split_csv = models_dir / 'evidence' / (domain + '_split_ids.csv')
    # ULB's canonical split is reproducibly generated, without touching tracked evidence.
    if domain == 'ulb' and not split_csv.is_file():
        from restore_ulb_split import restore
        with tempfile.TemporaryDirectory() as tmp:
            split_csv = restore(Path(tmp) / 'ulb_split_ids.csv')
            return audit_with_split(domain, models_dir, data, split_csv, DATA[domain],
                                    replay_python, require_artifacts)
    return audit_with_split(domain, models_dir, data, split_csv, DATA[domain],
                            replay_python, require_artifacts)


def audit_with_split(domain, models_dir, data, split_csv, spec, replay_python, require_artifacts):
    from pipeline import freeze_split
    expected_sha = SHAS[domain]['split']
    outcome = {'dataset': domain, 'data_sha256': SHAS[domain]['data'],
               'canonical_split_sha256': expected_sha, 'status': 'failed', 'models': {}}
    if not split_csv.is_file() or sha256(split_csv) != expected_sha:
        outcome['reason'] = 'canonical_split_sha_mismatch_or_missing'
        return outcome
    # freeze_split verifies raw data row IDs, predictor-profile grouping (UCI), temporal order (ULB)
    if split_csv.parent.resolve() == (models_dir / 'evidence').resolve():
        partitions = freeze_split(domain, data, split_csv.parent)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            temp_path = Path(tmp) / (domain + '_split_ids.csv')
            temp_path.write_bytes(split_csv.read_bytes())
            partitions = freeze_split(domain, data, temp_path.parent)
    n = len(data)
    if n != sum(v[0] for v in COUNTS[domain].values()):
        outcome['reason'] = 'source_rows_count_incorrect'
        return outcome
    for part, (size, positives) in COUNTS[domain].items():
        mask = partitions == part
        if int(mask.sum()) != size or int(data.loc[mask, spec['target']].sum()) != positives:
            outcome['reason'] = 'source_frozen_partition_counts_incorrect'
            return outcome
    outcome['partition_counts'] = {k: {'rows': v[0], 'positives': v[1]} for k,v in COUNTS[domain].items()}
    outcome['split_definition_verification'] = 'pass'
    all_pass = True
    for name in MODELS[domain]:
        pkg = models_dir / name
        entry = {'status': 'not_verified', 'model_id': name}
        try:
            manifest = json_file(pkg / 'manifest.json')
            evaluation = json_file(pkg / 'evaluation.json')
            artifact = artifact_status(pkg, manifest)
            entry['artifact'] = artifact
            entry['artifact_sha256'] = manifest.get('artifact_sha256')
            entry['split_sha256'] = expected_sha
            data_sha = manifest.get('dataset_sha256') or manifest.get('dataset_sha256_expected') or manifest.get('dataset_sha256_observed_this_run')
            if data_sha is not None and data_sha != SHAS[domain]['data']:
                raise ValueError('model dataset SHA differs from verified source')
            for key in ('validation','test'):
                observed = evaluation.get(key) or {}
                rows = observed.get('rows', observed.get('n'))
                positives = observed.get('positive_count', observed.get('positives'))
                if rows != COUNTS[domain][key][0] or positives != COUNTS[domain][key][1]:
                    raise ValueError(f'{key} evaluation counts differ from frozen source')
            if manifest.get('feature_order') != spec['features']:
                raise ValueError('model feature order differs from frozen source')
            if name.endswith('_r') and manifest.get('language') != 'R':
                raise ValueError('R model language mismatch')
            if name.endswith('_python') and manifest.get('language') != 'Python':
                raise ValueError('Python model language mismatch')
            reference = manifest.get('split_definition') or manifest.get('split_reference')
            if not reference or not str(reference).endswith(domain + '_split_ids.csv'):
                raise ValueError('model split reference missing or mismatched')
            alignment = fixture_alignment(pkg, data, partitions, spec['features'])
            entry['development_fixture_alignment'] = alignment
            # Bind the replay evidence to the exact development fixtures that
            # the verifier observed; the merger checks them again locally.
            entry['development_fixture_sha256'] = (
                sha256(pkg / 'fixtures/development_inputs.csv')
                if (pkg / 'fixtures/development_inputs.csv').is_file() else None)
            entry['reference_predictions_sha256'] = (
                sha256(pkg / 'fixtures/reference_predictions.csv')
                if (pkg / 'fixtures/reference_predictions.csv').is_file() else None)
            if alignment['status'] != 'match':
                raise ValueError('development fixture mismatch: ' + alignment['status'])
            if name.endswith('_r'):
                replay = refit_r_glm(data, partitions, spec['features'], spec['target'],
                                     pkg / 'r_coefficients.json', pkg / 'fixtures/reference_predictions.csv')
            elif replay_python:
                replay = refit_python(data, partitions, spec['features'], spec['target'], name,
                                      pkg / 'fixtures/development_inputs.csv')
            else:
                replay = {'status': 'not_run', 'reason': 'add --replay-python to independently refit'}
            entry['fit_reproduction'] = replay
            if replay['status'] != 'pass':
                entry['status'] = 'not_reproduced'
                all_pass = False
            elif artifact['status'] == 'match':
                entry['status'] = 'retrospectively_reproduced_artifact_verified'
            else:
                entry['status'] = 'retrospectively_reproduced_artifact_unverified'
                if require_artifacts:
                    all_pass = False
        except Exception as exc:
            entry['status'] = 'failed'
            entry['reason'] = f'{type(exc).__name__}: {exc}'
            all_pass = False
        outcome['models'][name] = entry
    outcome['status'] = ('retrospective_model_fit_reproduction_verified'
                         if all_pass else 'partial_reproduction_or_unverified_artifacts')
    outcome['provenance_class'] = 'independent_retrospective_reproduction_not_fit_time_attestation'
    outcome['limitation'] = ('Historical training executions did not record fit-time split SHA for all models. '
                             'Reproduction corroborates the canonical split but does not create a past attestation.')
    return outcome


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--models-dir', default='legacy_models')
    cli.add_argument('--data-root', default='datasets')
    cli.add_argument('--out', default='datasets/reports/provenance/split_provenance.json')
    cli.add_argument('--replay-python', action='store_true', help='Independently refit six Python models on training rows only')
    cli.add_argument('--allow-missing-artifacts', action='store_true',
                     help='Permit post-hoc refit but label artifact binding unverifiable')
    cli.add_argument('--strict', action='store_true', help='Nonzero exit unless every model reproduced and artifact verified')
    args = cli.parse_args(argv)
    models_dir, data_root = Path(args.models_dir).resolve(), Path(args.data_root).resolve()
    audits = {domain: audit_domain(domain, models_dir, data_root, args.replay_python,
                                   not args.allow_missing_artifacts) for domain in ('uci','ulb')}
    import sklearn, xgboost, lightgbm, scipy, statsmodels
    environment = {'python': platform.python_version(), 'platform': platform.system(),
                   'machine': platform.machine(), 'xgboost': xgboost.__version__,
                   'numpy': np.__version__, 'pandas': pd.__version__,
                   'scikit_learn': sklearn.__version__, 'lightgbm': lightgbm.__version__,
                   'scipy': scipy.__version__, 'statsmodels': statsmodels.__version__}
    report = {'schema_version': '1.0.0', 'report_kind': 'independent_split_provenance_reproduction',
              'execution_environment': environment,
              'generated_at_utc': datetime.now(timezone.utc).isoformat(),
              'replay_python_requested': args.replay_python,
              'historical_fit_time_attestation': False,
              'domains': audits,
              'scope': 'Research only; train-only refit, validation development fixtures, no test-set scoring'}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print('Replay environment:', json.dumps(environment, sort_keys=True))
    for domain, item in audits.items():
        print(f"{domain.upper()}: {item['status']}")
        for model, entry in item.get('models', {}).items():
            fit = entry.get('fit_reproduction', {})
            print(' ', model, entry['status'], 'fit:', fit.get('status'),
                  'artifact:', entry.get('artifact',{}).get('status'))
            if fit.get('status') == 'mismatch':
                print('    mismatch max_abs:', fit.get('max_abs_fixture_difference'),
                      'tolerance:', fit.get('tolerance_absolute'),
                      'rows_above_tolerance:', fit.get('n_fixture_differences_above_tolerance'))
    print('Saved', out)
    if args.strict and any(item['status'] != 'retrospective_model_fit_reproduction_verified' for item in audits.values()):
        raise SystemExit('STRICT FAIL: provenance reproduction incomplete; inspect JSON report')

if __name__ == '__main__':
    main()