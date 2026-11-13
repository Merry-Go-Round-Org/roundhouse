"""Frozen-bin Population Stability Index (PSI), shared by validation and monitoring.

Python standard library only. Data is read-only and no model is deserialized.
PSI measures marginal drift, NOT accuracy, calibration, causality or approval.
The reference is fitted once on approved DEVELOPMENT predictions/features.
Future windows reuse these exact bins and counts; never refit on current data.
"""
from __future__ import annotations

import bisect
import csv
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

SCHEMA_VERSION = "1.0.0"
FORBIDDEN = frozenset({"Class", "default payment next month", "duration", "event"})
DEFAULT_POLICY = {
    "version": "1.0.0",
    "max_numeric_bins": 10,
    "max_categories": 20,
    "max_reference_categories": 1000,
    "smoothing_alpha": 0.5,
    "min_reference_rows": 200,
    "min_current_rows": 100,
    "green_below": 0.10,
    "red_above": 0.25,
}
BANDS = {"green": 0, "amber": 1, "red": 2}


def canonical_hash(payload):
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def policy_config(policy=None):
    conf = dict(DEFAULT_POLICY)
    if policy is not None:
        if not isinstance(policy, dict) or set(policy) - set(conf):
            raise ValueError("Unknown PSI policy field")
        conf.update(policy)
    if conf["version"] != SCHEMA_VERSION:
        raise ValueError("Unsupported PSI policy version")
    for key in ("max_numeric_bins", "max_categories", "max_reference_categories",
                "min_reference_rows", "min_current_rows"):
        if type(conf[key]) is not int or conf[key] < (2 if key == "max_numeric_bins" else 1):
            raise ValueError("Invalid PSI policy: " + key)
    if conf["max_reference_categories"] < conf["max_categories"]:
        raise ValueError("Reference category limit cannot be below retained category limit")
    for key in ("smoothing_alpha", "green_below", "red_above"):
        if type(conf[key]) not in (int, float) or not math.isfinite(conf[key]):
            raise ValueError("Invalid PSI policy: " + key)
    if (conf["smoothing_alpha"] <= 0 or not 0 < conf["green_below"] <
            conf["red_above"]):
        raise ValueError("PSI smoothing and bands must be positive and ordered")
    return conf


def missing(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def numeric(value, *, score=False):
    if missing(value):
        if score:
            raise ValueError("Missing predicted score")
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean supplied as numeric feature")
    try:
        answer = float(value)
    except (TypeError, ValueError, OverflowError) as ex:
        raise ValueError("Non-numeric feature value") from ex
    if not math.isfinite(answer):
        raise ValueError("NaN or infinite feature/score value")
    if score and not 0 <= answer <= 1:
        raise ValueError("Predicted score outside [0,1]")
    return answer


def categorical(value):
    if missing(value):
        return None
    if not isinstance(value, str):
        return str(value)
    return value


def quantiles(values, count):
    """Interior linear-interpolated quantiles; duplicate cutpoints are merged."""
    sorted_values = sorted(values)
    n = len(sorted_values)
    if n == 0:
        return []
    edges = []
    for i in range(1, count):
        pos = (n - 1) * i / count
        low = int(pos)
        high = min(low + 1, n - 1)
        val = sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (pos - low)
        if math.isfinite(val) and sorted_values[0] < val < sorted_values[-1]:
            if not edges or val > edges[-1]:
                edges.append(float(val))
    return edges


def freeze_column(values, *, kind="numeric", config=None, score=False):
    conf = policy_config(config)
    if kind not in ("numeric", "categorical") or (score and kind != "numeric"):
        raise ValueError("Unsupported PSI feature type")
    values = list(values)
    if not values:
        raise ValueError("Reference has no observations")
    if kind == "numeric":
        clean = [numeric(x, score=score) for x in values]
        valid = [x for x in clean if x is not None]
        constant = valid[0] if valid and min(valid) == max(valid) else None
        edges = [] if constant is not None or not valid else quantiles(valid, conf["max_numeric_bins"])
        labels = (["constant", "different_from_constant", "missing"]
                  if constant is not None else
                  [f"bin_{i}" for i in range(len(edges) + 1)] + ["missing"])
        descriptor = {"kind": "numeric", "edges": edges, "constant": constant,
                      "labels": labels}
    else:
        clean = [categorical(x) for x in values]
        frequency = Counter(x for x in clean if x is not None)
        if len(frequency) > conf["max_reference_categories"]:
            raise ValueError("Categorical feature has too many distinct reference values")
        retained = [x for x, _ in sorted(frequency.items(), key=lambda kv: (-kv[1], kv[0]))
                    [:conf["max_categories"]]]
        descriptor = {"kind": "categorical", "known_categories": sorted(frequency),
                      "retained_categories": retained,
                      "labels": [f"category_{i}" for i in range(len(retained))]
                                + ["other_known", "unknown", "missing"]}
    descriptor["counts"] = count_bins(descriptor, clean)
    descriptor["reference_rows"] = len(clean)
    return descriptor


def bucket(spec, value):
    if spec["kind"] == "numeric":
        value = numeric(value)
        if value is None:
            return len(spec["labels"]) - 1
        if spec.get("constant") is not None:
            return 0 if value == spec["constant"] else 1
        return bisect.bisect_right(spec["edges"], value)
    value = categorical(value)
    if value is None:
        return len(spec["labels"]) - 1
    retained = spec["retained_categories"]
    if value in retained:
        return retained.index(value)
    return len(retained) if value in spec["known_categories"] else len(retained) + 1


def count_bins(spec, values):
    counts = [0] * len(spec["labels"])
    for value in values:
        counts[bucket(spec, value)] += 1
    return counts


def compare_column(frozen, current, *, config=None):
    conf = policy_config(config)
    if not isinstance(frozen, dict) or frozen.get("kind") not in ("numeric", "categorical"):
        raise ValueError("Invalid frozen PSI column")
    if frozen["kind"] == "numeric":
        edges = frozen["edges"]
        if any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in edges):
            raise ValueError("Nonfinite frozen numeric edge")
        if edges != sorted(set(edges)):
            raise ValueError("Unsorted or repeated frozen numeric edges")
        if frozen.get("constant") is not None:
            numeric(frozen["constant"])
            if edges:
                raise ValueError("Constant feature must have no quantile edges")
            expected_bins = 3
        else:
            expected_bins = len(edges) + 2
    else:
        retained = frozen["retained_categories"]
        known = frozen["known_categories"]
        if len(set(retained)) != len(retained) or not set(retained).issubset(known):
            raise ValueError("Invalid frozen category mapping")
        expected_bins = len(retained) + 3
    reference = frozen["counts"]
    labels = frozen["labels"]
    if (len(labels) != expected_bins or len(reference) != expected_bins or
            any(type(x) is not int or x < 0 for x in reference) or
            sum(reference) != frozen["reference_rows"] or frozen["reference_rows"] <= 0):
        raise ValueError("Invalid frozen PSI counts/labels")
    current = list(current)
    if not current:
        raise ValueError("Current monitoring window is empty")
    fresh = count_bins(frozen, current)
    if sum(fresh) != len(current):
        raise ValueError("Current PSI bin counts are inconsistent")
    smoothing = conf["smoothing_alpha"]
    k = len(labels)
    n_reference, n_current = sum(reference), sum(fresh)
    p = [(x + smoothing) / (n_reference + smoothing * k) for x in reference]
    q = [(x + smoothing) / (n_current + smoothing * k) for x in fresh]
    contributions = [(y - x) * math.log(y / x) for x, y in zip(p, q)]
    psi = math.fsum(contributions)
    if psi < 0 and abs(psi) < 1e-13:
        psi = 0.0
    if not math.isfinite(psi) or psi < 0:
        raise ValueError("Invalid PSI calculation")
    band = ("green" if psi < conf["green_below"] else
            "amber" if psi <= conf["red_above"] else "red")
    reason = None
    if n_reference < conf["min_reference_rows"]:
        reason = "Reference cohort below minimum sample size"
    elif n_current < conf["min_current_rows"]:
        reason = "Current cohort below minimum sample size"
    if reason:
        band = "not_assessable"
    return {
        "status": "NOT_ASSESSABLE" if reason else ("PASS" if band == "green" else "REVIEW"),
        "band": band, "psi": psi, "reference_rows": n_reference, "current_rows": n_current,
        "smoothing_alpha": smoothing, "bin_definition": {k: v for k, v in frozen.items()
                                                      if k not in ("counts", "reference_rows")},
        "bins": [{"label": label, "reference_count": a, "current_count": b,
                  "reference_share_smoothed": x, "current_share_smoothed": y,
                  "psi_contribution": z}
                 for label, a, b, x, y, z in
                 zip(labels, reference, fresh, p, q, contributions)],
        "missing_reference_count": reference[-1],
        "missing_current_count": fresh[-1],
        "unknown_current_count": fresh[-2] if frozen["kind"] == "categorical" else None,
        "reason": reason or ("PSI is descriptive drift evidence, not model accuracy"),
    }


def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        if not fields or len(fields) != len(set(fields)) or "row_id" not in fields:
            raise ValueError("CSV needs unique column names including row_id")
        rows = list(reader)
    if not rows or any(None in row for row in rows):
        raise ValueError("Empty or malformed CSV")
    ids = [row["row_id"] for row in rows]
    if any(not x or not x.strip() for x in ids) or len(ids) != len(set(ids)):
        raise ValueError("row_id must be nonempty and unique")
    if FORBIDDEN.intersection(fields):
        raise ValueError("Target/time-to-event labels forbidden in PSI inputs")
    return fields, rows, ids


def read_aligned(inputs_path, scores_path, feature_order):
    fields, rows, ids = read_csv(inputs_path)
    score_fields, scores, score_ids = read_csv(scores_path)
    if score_fields != ["row_id", "positive_probability"]:
        raise ValueError("Scoring CSV contract must be row_id,positive_probability")
    if ids != score_ids:
        raise ValueError("Scored row IDs/order differ from feature inputs")
    if not feature_order or len(set(feature_order)) != len(feature_order):
        raise ValueError("Features must be nonempty and unique")
    if "row_id" in feature_order or FORBIDDEN.intersection(feature_order):
        raise ValueError("Identifiers/targets forbidden as monitored predictors")
    if not set(feature_order).issubset(fields):
        raise ValueError("Missing monitored model predictor")
    return {
        "rows": len(rows),
        "row_ids_sha256": canonical_hash(sorted(ids)),
        "features": {name: [row[name] for row in rows] for name in feature_order},
        "score": [numeric(row["positive_probability"], score=True) for row in scores],
    }


def freeze_baseline(*, model_id, feature_order, reference_inputs, reference_scores,
                    reference_scope, config=None, feature_kinds=None,
                    artifact_sha256=None):
    conf = policy_config(config)
    if not model_id or not isinstance(model_id, str):
        raise ValueError("model_id is required")
    if reference_scope != "development":
        raise ValueError("PSI reference must be an explicitly authorized development cohort")
    feature_kinds = feature_kinds or {}
    if set(feature_kinds) - set(feature_order):
        raise ValueError("Feature kinds contain unknown predictor")
    data = read_aligned(reference_inputs, reference_scores, feature_order)
    columns = {key: freeze_column(values, kind=feature_kinds.get(key, "numeric"),
                                  config=conf)
               for key, values in data["features"].items()}
    columns["__score__"] = freeze_column(data["score"], config=conf, score=True)
    baseline = {
        "schema_version": SCHEMA_VERSION, "kind": "frozen_stability_baseline",
        "model_id": model_id, "artifact_sha256": artifact_sha256,
        "reference_scope": reference_scope, "reference_rows": data["rows"],
        "reference_row_ids_sha256": data["row_ids_sha256"],
        "feature_order": list(feature_order), "policy": conf,
        "reference_input_sha256": file_hash(reference_inputs),
        "reference_scores_sha256": file_hash(reference_scores),
        "columns": columns,
    }
    baseline["baseline_sha256"] = canonical_hash(baseline)
    return baseline


def validate_baseline(baseline):
    if not isinstance(baseline, dict) or baseline.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported PSI baseline schema")
    if baseline.get("kind") != "frozen_stability_baseline" or baseline.get("reference_scope") != "development":
        raise ValueError("PSI baseline has unapproved scope/type")
    claimed = baseline.get("baseline_sha256")
    if not isinstance(claimed, str) or len(claimed) != 64:
        raise ValueError("Frozen baseline checksum missing")
    observed = canonical_hash({key: value for key, value in baseline.items()
                               if key != "baseline_sha256"})
    if observed != claimed:
        raise ValueError("Frozen baseline checksum mismatch")
    policy_config(baseline.get("policy"))
    features = baseline.get("feature_order")
    if not isinstance(features, list) or not features or len(features) != len(set(features)):
        raise ValueError("Invalid frozen feature list")
    if "row_id" in features or FORBIDDEN.intersection(features):
        raise ValueError("Identifier or target leaked into frozen feature list")
    if set(baseline.get("columns", {})) != set(features) | {"__score__"}:
        raise ValueError("Frozen PSI feature set mismatch")
    if baseline.get("reference_rows", 0) <= 0:
        raise ValueError("Frozen reference sample is empty")
    if not isinstance(baseline.get("reference_row_ids_sha256"), str) or len(baseline["reference_row_ids_sha256"]) != 64:
        raise ValueError("Frozen reference ID fingerprint missing")
    for field in list(features) + ["__score__"]:
        if baseline["columns"][field].get("reference_rows") != baseline["reference_rows"]:
            raise ValueError("Frozen PSI column count mismatch")
    return True


def evaluate_window(*, baseline, current_inputs, current_scores,
                    expected_model_id=None, expected_artifact_sha256=None):
    validate_baseline(baseline)
    if expected_model_id and baseline["model_id"] != expected_model_id:
        raise ValueError("Baseline bound to another model")
    if expected_artifact_sha256 and baseline["artifact_sha256"] != expected_artifact_sha256:
        raise ValueError("Baseline bound to a different model artifact")
    features = baseline["feature_order"]
    current = read_aligned(current_inputs, current_scores, features)
    if current["row_ids_sha256"] == baseline["reference_row_ids_sha256"]:
        raise ValueError("Current window reuses the exact frozen reference row-ID set")
    config = baseline["policy"]
    score = compare_column(baseline["columns"]["__score__"], current["score"], config=config)
    per_feature = {key: compare_column(baseline["columns"][key], current["features"][key],
                                       config=config) for key in features}
    assessed = [score] + list(per_feature.values())
    if any(r["status"] == "NOT_ASSESSABLE" for r in assessed):
        band = "not_assessable"
    else:
        band = max((r["band"] for r in assessed), key=lambda x: BANDS[x])
    score_band = score["band"]
    feature_band = ("not_assessable" if any(r["band"] == "not_assessable" for r in per_feature.values())
                    else max((r["band"] for r in per_feature.values()), key=lambda x: BANDS[x]))
    red_features = [name for name, r in per_feature.items() if r["band"] == "red"]
    return {
        "schema_version": SCHEMA_VERSION, "kind": "model_stability_window",
        "model_id": baseline["model_id"], "artifact_sha256": baseline["artifact_sha256"],
        "baseline_sha256": baseline["baseline_sha256"],
        "policy_version": config["version"], "reference_scope": "development",
        "reference_rows": baseline["reference_rows"], "current_rows": current["rows"],
        "current_inputs_sha256": file_hash(current_inputs),
        "current_scores_sha256": file_hash(current_scores),
        "current_row_ids_sha256": current["row_ids_sha256"],
        "status": "NOT_ASSESSABLE" if band == "not_assessable" else
                  "PASS" if band == "green" else "REVIEW",
        "band": band, "score_band": score_band, "feature_band": feature_band,
        "revalidation_required": band != "not_assessable" and
                                 (score_band == "red" or bool(red_features)),
        "red_features": red_features,
        "score": score, "features": per_feature,
        "limitations": ["Marginal shift only: no outcomes, performance, calibration or concept drift assessed",
                        "Thresholds are project heuristics, not regulatory approval criteria",
                        "Never refit the frozen bins on the current window"],
    }
