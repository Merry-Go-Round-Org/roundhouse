#!/usr/bin/env python3
"""Compare identical, trusted saved XGBoost artifacts scored on macOS and Linux.

No model deserialization or training occurs here. Input/output CSVs are local,
trusted development fixtures only. A failed or missing comparison is not a pass.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import struct
from datetime import datetime, timezone
from pathlib import Path

MODELS = ('credit_xgboost_python', 'fraud_xgboost_python')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1048576), b''):
            h.update(part)
    return h.hexdigest()


def rows(path, columns):
    with Path(path).open(newline='', encoding='utf-8-sig') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != columns:
            raise ValueError(f'{path}: unexpected CSV columns')
        data = list(reader)
    if not data or any(None in row for row in data):
        raise ValueError(f'{path}: missing or malformed records')
    ids = [r['row_id'] for r in data]
    if len(set(ids)) != len(ids) or any(not rid.strip() for rid in ids):
        raise ValueError(f'{path}: duplicate or empty IDs')
    return data


def float32_ulp_distance(left, right):
    """IEEE-754 binary32 ULP distance for finite nonnegative probabilities.

    Diagnostic only. ULP proximity does NOT override the fixed absolute
    acceptance tolerance or convert any observed numeric FAIL to a PASS.
    """
    if not (math.isfinite(left) and math.isfinite(right) and
            0 <= left <= 1 and 0 <= right <= 1):
        raise ValueError('ULP operands must be finite probabilities')
    l_bits = struct.unpack('!I', struct.pack('!f', left))[0]
    r_bits = struct.unpack('!I', struct.pack('!f', right))[0]
    return abs(l_bits - r_bits)


def compare(models_dir, predictions_dir, tolerance=1e-10):
    findings = {}
    for name in MODELS:
        pkg = Path(models_dir) / name
        manifest = json.loads((pkg / 'manifest.json').read_text())
        artifact = pkg / 'model.joblib'
        actual_sha = digest(artifact)
        if actual_sha != manifest.get('artifact_sha256'):
            raise ValueError(f'{name}: saved artifact checksum mismatch (or LFS pointer)')
        fixture = pkg / 'fixtures/development_inputs.csv'
        expected = pkg / 'fixtures/reference_predictions.csv'
        fixture_ids = [row['row_id'] for row in rows(fixture, list(['row_id', *manifest['feature_order']]))]
        reference = rows(expected, ['row_id', 'positive_probability'])
        if [r['row_id'] for r in reference] != fixture_ids:
            raise ValueError(f'{name}: development reference ID alignment mismatch')
        p = {}
        for tag in ('macos', 'linux_amd64'):
            result = rows(Path(predictions_dir) / f'{name}_{tag}.csv', ['row_id', 'positive_probability'])
            if [r['row_id'] for r in result] != fixture_ids:
                raise ValueError(f'{name}: {tag} result row alignment mismatch')
            values = [float(r['positive_probability']) for r in result]
            if any(not math.isfinite(v) or not 0 <= v <= 1 for v in values):
                raise ValueError(f'{name}: {tag} probabilities outside [0,1]')
            p[tag] = values
        ref = [float(r['positive_probability']) for r in reference]
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in ref):
            raise ValueError(f'{name}: invalid saved reference probabilities')
        mac_diff = max(abs(a - b) for a, b in zip(p['macos'], ref))
        linux_diff = max(abs(a - b) for a, b in zip(p['linux_amd64'], ref))
        cross_diff = max(abs(a - b) for a, b in zip(p['macos'], p['linux_amd64']))
        cross_ulp = max(float32_ulp_distance(a, b)
                        for a, b in zip(p['macos'], p['linux_amd64']))
        mac_reference_ulp = max(float32_ulp_distance(a, b)
                                for a, b in zip(p['macos'], ref))
        linux_reference_ulp = max(float32_ulp_distance(a, b)
                                  for a, b in zip(p['linux_amd64'], ref))
        findings[name] = {
            'model_id': name, 'artifact_sha256': actual_sha,
            'development_fixture_sha256': digest(fixture),
            'rows': len(fixture_ids),
            'max_absolute_macos_vs_linux': cross_diff,
            'max_absolute_macos_vs_reference': mac_diff,
            'max_absolute_linux_vs_reference': linux_diff,
            'max_float32_ulp_distance_macos_vs_linux': cross_ulp,
            'max_float32_ulp_distance_macos_vs_reference': mac_reference_ulp,
            'max_float32_ulp_distance_linux_vs_reference': linux_reference_ulp,
            'ulp_analysis_is_diagnostic_only': True,
            'tolerance_absolute': tolerance,
            'status': 'PASS' if max(cross_diff, mac_diff, linux_diff) <= tolerance else 'FAIL',
            'scoring_inference_only': True,
            'macos_output_sha256': digest(Path(predictions_dir) / f'{name}_macos.csv'),
            'linux_output_sha256': digest(Path(predictions_dir) / f'{name}_linux_amd64.csv'),
        }
    return {
        'schema_version': '1.0.0',
        'report_kind': 'saved_artifact_inference_portability',
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'comparison_environment': platform.platform(),
        'source_execution_environments': ['macos', 'linux_amd64'],
        'not_a_training_reproduction': True,
        'not_a_new_holdout': True,
        'models': findings,
        'status': 'PASS' if all(m['status'] == 'PASS' for m in findings.values()) else 'FAIL',
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--models-dir', default='legacy_models')
    ap.add_argument('--predictions-dir', default='datasets/reports/provenance/inference_outputs')
    ap.add_argument('--out', default='datasets/reports/provenance/inference_portability.json')
    ap.add_argument('--absolute-tolerance', type=float, default=1e-10)
    ap.add_argument('--strict', action='store_true')
    a = ap.parse_args(argv)
    if not 0 < a.absolute_tolerance <= 1e-10:
        ap.error('Tolerance cannot be relaxed above 1e-10')
    result = compare(a.models_dir, a.predictions_dir, a.absolute_tolerance)
    path = Path(a.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    for name, item in result['models'].items():
        print(name, item['status'], 'mac_linux_max_abs:', item['max_absolute_macos_vs_linux'])
    print('Saved', path)
    if a.strict and result['status'] != 'PASS':
        raise SystemExit('STRICT FAIL: saved-artifact inference is not portable within tolerance')


if __name__ == '__main__':
    main()
