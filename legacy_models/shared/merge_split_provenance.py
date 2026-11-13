#!/usr/bin/env python3
"""Merge independently verified macOS/Linux provenance replay reports.

Requires each one of the eight release models to be reproduced in at least one
verified environment. Never hides failures in the other environment, changes
scoring tolerances, touches model artifacts, or invents fit-time attestations.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

from verify_split_provenance import MODELS, SHAS, COUNTS, sha256, json_file, artifact_status

DATA_PATH = {
    'uci': Path('uci/default of credit card clients.xls'),
    'ulb': Path('ulb/creditcard.csv'),
}


def _must(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _finite(value: object, label: str) -> float:
    _must(isinstance(value, (float, int)) and not isinstance(value, bool), f'{label}: missing/non-numeric')
    number = float(value)
    _must(math.isfinite(number), f'{label}: non-finite')
    return number


def _validate_one_environment(label: str, report: dict) -> None:
    _must(report.get('report_kind') == 'independent_split_provenance_reproduction',
          f'{label}: incorrect report kind')
    _must(report.get('schema_version') == '1.0.0', f'{label}: unknown schema version')
    _must(report.get('replay_python_requested') is True, f'{label}: Python refits not requested')
    _must(report.get('historical_fit_time_attestation') is False,
          f'{label}: claims unsupported historical attestation')
    env = report.get('execution_environment') or {}
    expected = 'Darwin' if label == 'macos' else 'Linux'
    _must(env.get('platform') == expected, f'{label}: expected {expected}, got {env.get("platform")}')
    _must(bool(env.get('xgboost')) and bool(env.get('python')),
          f'{label}: missing recorded execution environment')


def _validate_domain(label: str, domain: str, record: dict) -> None:
    _must(isinstance(record, dict), f'{label}/{domain}: missing domain')
    _must(record.get('split_definition_verification') == 'pass',
          f'{label}/{domain}: split definition not checked')
    _must(record.get('data_sha256') == SHAS[domain]['data'] and
          record.get('canonical_split_sha256') == SHAS[domain]['split'],
          f'{label}/{domain}: raw-data or split SHA mismatch')
    expected_counts = {part: {'rows': n, 'positives': k}
                       for part, (n, k) in COUNTS[domain].items()}
    _must(record.get('partition_counts') == expected_counts,
          f'{label}/{domain}: frozen partition membership counts differ')
    _must(set((record.get('models') or {}).keys()) == set(MODELS[domain]),
          f'{label}/{domain}: model inventory incomplete or unexpected')


def _validate_model(label: str, name: str, entry: dict, package: Path, artifact_sha: str,
                    canonical_split: str) -> None:
    prefix = f'{label}/{name}'
    _must(isinstance(entry, dict), f'{prefix}: missing model entry')
    _must(entry.get('model_id') == name, f'{prefix}: incorrect model ID')
    _must(entry.get('artifact_sha256') == artifact_sha and
          entry.get('split_sha256') == canonical_split,
          f'{prefix}: artifact/split fingerprint mismatch')
    observed_artifact = entry.get('artifact') or {}
    _must(observed_artifact.get('status') == 'match' and
          observed_artifact.get('expected_sha256') == artifact_sha and
          observed_artifact.get('actual_sha256') == artifact_sha,
          f'{prefix}: source run did not hash-match the actual artifact')
    _must((entry.get('development_fixture_alignment') or {}).get('status') == 'match',
          f'{prefix}: fixture not aligned with frozen validation partition')
    fixture_dir = package / 'fixtures'
    for key, path in (('development_fixture_sha256', fixture_dir / 'development_inputs.csv'),
                      ('reference_predictions_sha256', fixture_dir / 'reference_predictions.csv')):
        _must(path.is_file() and entry.get(key) == sha256(path),
              f'{prefix}: fixture digest does not match the currently committed fixture')
    rep = entry.get('fit_reproduction') or {}
    _must(rep.get('status') in ('pass', 'mismatch'),
          f'{prefix}: fit was not evaluated')
    tol = _finite(rep.get('tolerance_absolute'), prefix + ' tolerance')
    _must(0 < tol <= (1e-8 if name.endswith('_r') else 1e-7),
          f'{prefix}: tolerance was relaxed')
    value = _finite(rep.get('max_abs_fixture_difference'), prefix + ' max difference')
    _must(rep.get('train_rows') == COUNTS['uci' if name.startswith('credit_') else 'ulb']['train'][0] and
          rep.get('train_positives') == COUNTS['uci' if name.startswith('credit_') else 'ulb']['train'][1],
          f'{prefix}: fit used unexpected train counts')
    if name.endswith('_r'):
        params = _finite(rep.get('max_abs_parameter_difference'), prefix + ' parameter diff')
        actual_pass = (value <= tol and params <= tol)
    else:
        actual_pass = value <= tol
    _must((rep['status'] == 'pass') == actual_pass,
          f'{prefix}: reported fit status inconsistent with measured difference')
    expected_status = ('retrospectively_reproduced_artifact_verified' if actual_pass
                       else 'not_reproduced')
    _must(entry.get('status') == expected_status,
          f'{prefix}: reported model status inconsistent with fit')


def merge_reports(mac: dict, linux: dict, models_dir: Path, data_root: Path,
                  mac_path: Path, linux_path: Path) -> dict:
    """Validate both full reports and local SHA-pinned artifacts, then combine successes."""
    models_dir, data_root = Path(models_dir), Path(data_root)
    sources = {'macos': mac, 'linux_amd64': linux}
    paths = {'macos': Path(mac_path), 'linux_amd64': Path(linux_path)}
    for tag, value in sources.items():
        _validate_one_environment(tag, value)
    report_sources = {
        tag: {'filename': path.name,
              'sha256': sha256(path),
              'execution_environment': value['execution_environment']}
        for tag, (path, value) in ((key, (paths[key], sources[key])) for key in sources)
    }
    _must(paths['macos'].resolve() != paths['linux_amd64'].resolve(),
          'Reports must come from different files and environments')
    merged = {}
    for domain, names in MODELS.items():
        data_path = data_root / DATA_PATH[domain]
        _must(data_path.is_file() and sha256(data_path) == SHAS[domain]['data'],
              f'{domain}: local raw dataset missing or SHA mismatch')
        split_path = models_dir / 'evidence' / f'{domain}_split_ids.csv'
        _must(split_path.is_file() and sha256(split_path) == SHAS[domain]['split'],
              f'{domain}: local frozen split missing or SHA mismatch; restore ULB first')
        for tag, report in sources.items():
            _validate_domain(tag, domain, (report.get('domains') or {}).get(domain))
        combined = {}
        disagreements = []
        for name in names:
            package = models_dir / name
            manifest = json_file(package / 'manifest.json')
            local_artifact = artifact_status(package, manifest)
            _must(local_artifact.get('status') == 'match', f'{name}: current artifact not SHA-verified')
            sha = manifest['artifact_sha256']
            entries = {tag: sources[tag]['domains'][domain]['models'][name] for tag in sources}
            for tag, entry in entries.items():
                _validate_model(tag, name, entry, package, sha, SHAS[domain]['split'])
            passed = [tag for tag, entry in entries.items()
                      if entry['fit_reproduction']['status'] == 'pass']
            _must(passed, f'{name}: no successful independent refit in either environment')
            selected = passed[0]
            original = entries[selected]
            rejected = [
                {'environment': tag,
                 'status': entry['fit_reproduction']['status'],
                 'max_abs_fixture_difference': entry['fit_reproduction']['max_abs_fixture_difference'],
                 'tolerance_absolute': entry['fit_reproduction']['tolerance_absolute']}
                for tag, entry in entries.items() if tag not in passed
            ]
            if rejected:
                disagreements.append({'model_id': name, 'nonreproducing_environments': rejected})
            combined[name] = {
                **original,
                'verification_environment': selected,
                'supporting_source_report_sha256': report_sources[selected]['sha256'],
                'cross_environment_reproductions': {
                    tag: {'status': entry['fit_reproduction']['status'],
                          'max_abs_fixture_difference': entry['fit_reproduction']['max_abs_fixture_difference'],
                          'tolerance_absolute': entry['fit_reproduction']['tolerance_absolute']}
                    for tag, entry in entries.items()},
                'cross_environment_divergences': rejected,
            }
        merged[domain] = {
            'dataset': domain,
            'data_sha256': SHAS[domain]['data'],
            'canonical_split_sha256': SHAS[domain]['split'],
            'split_definition_verification': 'pass',
            'partition_counts': {part: {'rows': count, 'positives': positive}
                                 for part, (count, positive) in COUNTS[domain].items()},
            'models': combined,
            'status': 'cross_environment_model_fit_reproduction_verified',
            'cross_environment_divergences': disagreements,
            'provenance_class': 'independent_retrospective_reproduction_not_fit_time_attestation',
            'limitation': ('Models are reproduced on the canonical split in at least one environment. '
                           'Hardware/OS-dependent XGBoost refit divergences remain; this is not original fit-time attestation.'),
        }
    return {
        'schema_version': '1.0.0',
        'report_kind': 'independent_split_provenance_reproduction',
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'replay_python_requested': True,
        'historical_fit_time_attestation': False,
        'aggregation': {'status': 'complete',
                        'kind': 'two_environment_verified_artifact_replay',
                        'source_reports': report_sources,
                        'note': ('Every original model re-fitted in at least one verified environment; '
                                 'cross-OS XGBoost mismatches are retained in each domain.')},
        'domains': merged,
        'scope': 'Retrospective train-only refits and development fixtures; no fresh holdout or fit-time attestation',
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mac-report', default='datasets/reports/provenance/split_provenance_macos.json')
    parser.add_argument('--linux-report', default='datasets/reports/provenance/split_provenance_linux.json')
    parser.add_argument('--models-dir', default='legacy_models')
    parser.add_argument('--data-root', default='datasets')
    parser.add_argument('--out', default='datasets/reports/provenance/split_provenance.json')
    parser.add_argument('--strict', action='store_true', help='Reject incomplete evidence (also default)')
    args = parser.parse_args(argv)
    paths = [Path(args.mac_report).resolve(), Path(args.linux_report).resolve()]
    output = Path(args.out).resolve()
    if output in paths:
        parser.error('Output must not overwrite either original source audit')
    for path in paths:
        if not path.is_file():
            parser.error(f'Missing source report {path}; run the two independent platform audits first')
    try:
        merged = merge_reports(json_file(paths[0]), json_file(paths[1]),
                               Path(args.models_dir).resolve(), Path(args.data_root).resolve(),
                               paths[0], paths[1])
    except (ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f'STRICT FAIL: provenance reports cannot be safely merged: {exc}')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(merged, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    for domain, record in merged['domains'].items():
        print(f'{domain.upper()}: {record["status"]}')
        for name, item in record['models'].items():
            print(' ', name, 'PASS on', item['verification_environment'])
        for disagreement in record['cross_environment_divergences']:
            print('  CROSS-ENVIRONMENT LIMITATION:', disagreement['model_id'],
                  disagreement['nonreproducing_environments'])
    print('Saved verified cross-environment report:', output)


if __name__ == '__main__':
    main()
