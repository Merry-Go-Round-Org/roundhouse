"""Executable F1 contract regression tests; no DB, raw data or model mutation.

These exercise the stand-alone proposed deterministic policy, not backend API
integration. Canonical SHA fixtures are from immutable legacy evidence.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from legacy_models.shared.f1_split_contract import (  # noqa: E402
    ALGORITHM,
    CANONICAL,
    SEED,
    SplitPolicyError,
    canonical_upload_mode,
    generate_binary_iid,
    server_dataset_id,
    sha256,
    validate_assignments,
)
from legacy_models.shared.restore_ulb_split import restore  # noqa: E402


def parse(content: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content.decode("utf-8"))))


def test_uci_frozen_exact_sha_row_order_and_partition_counts():
    evidence = ROOT / "legacy_models" / "evidence" / "uci_split_ids.csv"
    content = evidence.read_bytes()
    spec = CANONICAL["uci_credit"]
    assert sha256(content) == spec["split_sha256"]
    rows = parse(content)
    assert [r["row_id"] for r in rows] == [
        f"uci-{i:06d}" for i in range(1, 30001)
    ]
    assert dict(Counter(r["split"] for r in rows)) == spec["counts"]
    assert validate_assignments([r["row_id"] for r in rows], content) == spec["counts"]
    assert canonical_upload_mode("uci_credit", spec["source_sha256"]) == "canonical_reused"
    assert canonical_upload_mode("uci_credit", spec["source_sha256"], content) == "uploaded"


def test_ulb_restored_exact_sha_row_order_partition_counts(tmp_path):
    target = tmp_path / "ulb_split_ids.csv"
    assert restore(target) == target
    content = target.read_bytes()
    spec = CANONICAL["ulb_fraud"]
    assert sha256(content) == spec["split_sha256"]
    rows = parse(content)
    assert [r["row_id"] for r in rows] == [
        f"ulb-{i:06d}" for i in range(284807)
    ]
    assert dict(Counter(r["split"] for r in rows)) == spec["counts"]
    assert validate_assignments([r["row_id"] for r in rows], content) == spec["counts"]
    # Repeating restoration must not change canonical bytes.
    restore(target)
    assert target.read_bytes() == content
    assert canonical_upload_mode("ulb_fraud", spec["source_sha256"]) == "canonical_reused"
    assert canonical_upload_mode("ulb_fraud", spec["source_sha256"], content) == "uploaded"
    target.write_bytes(content + b"\n")
    with pytest.raises(ValueError):
        restore(target)


@pytest.mark.parametrize("dataset_id", ["uci_credit", "ulb_fraud"])
def test_canonical_source_and_supplied_split_are_trusted_not_self_declared(dataset_id):
    spec = CANONICAL[dataset_id]
    with pytest.raises(SplitPolicyError, match="canonical source"):
        canonical_upload_mode(dataset_id, "0" * 64)
    with pytest.raises(SplitPolicyError, match="frozen canonical bytes"):
        canonical_upload_mode(dataset_id, spec["source_sha256"], b"row_id,split\n")
    with pytest.raises(SplitPolicyError):
        canonical_upload_mode(dataset_id, hashlib.sha256(b"modified source").hexdigest())


def test_unknown_canonical_id_is_not_implicitly_reused():
    with pytest.raises(SplitPolicyError, match="Unknown"):
        canonical_upload_mode("ds_fake", "0" * 64)


def test_explicit_three_file_membership_reference_fixture():
    fixtures = json.loads(
        (Path(__file__).parent / "fixtures" / "f1_invalid_assignments.json").read_text()
    )
    assert validate_assignments(
        fixtures["source_ids"], fixtures["valid_split_csv"].encode()
    ) == {"train": 2, "validation": 2, "test": 2}


@pytest.mark.parametrize("case_name", [
    "duplicate_split_id", "overlapping_partitions", "missing_assignment",
    "unknown_assignment", "invalid_partition", "empty_row_id",
    "malformed_extra_column", "wrong_header", "missing_partition",
])
def test_invalid_explicit_assignments_fail_closed(case_name):
    fixtures = json.loads(
        (Path(__file__).parent / "fixtures" / "f1_invalid_assignments.json").read_text()
    )
    case = next(c for c in fixtures["invalid"] if c["name"] == case_name)
    with pytest.raises(SplitPolicyError):
        validate_assignments(fixtures["source_ids"], case["split_csv"].encode())


def test_duplicate_source_ids_fail_closed():
    with pytest.raises(SplitPolicyError, match="Duplicate source"):
        validate_assignments(
            ["acct-001", "acct-001"],
            b"row_id,split\nacct-001,train\nacct-002,validation\nacct-003,test\n",
        )


def independent_rows(count_per_class=40):
    return [
        {"row_id": f"record-{cls}-{i:03d}", "target": cls, "feature": str(i)}
        for cls in (0, 1)
        for i in range(count_per_class)
    ]


def test_generation_deterministic_reorder_independent_and_stratified():
    rows = independent_rows()
    original = tuple(r["row_id"] for r in rows)
    first = generate_binary_iid(rows, seed=SEED)
    same = generate_binary_iid(reversed(rows), seed=SEED)
    assert first == same
    assert first.algorithm == ALGORITHM
    assert first.sha256 == sha256(first.content)
    assert first.content.startswith(b"row_id,split\n")
    assert first.partition_counts == {"train": 48, "validation": 16, "test": 16}
    assert tuple(r["row_id"] for r in rows) == original  # Does not reorder input.
    labels = {r["row_id"]: r["target"] for r in rows}
    by_class = {
        cls: Counter(r["split"] for r in parse(first.content) if labels[r["row_id"]] == cls)
        for cls in (0, 1)
    }
    for cls in (0, 1):
        assert by_class[cls] == {"train": 24, "validation": 8, "test": 8}
    assert generate_binary_iid(rows, seed=SEED + 1).content != first.content


@pytest.mark.parametrize("bad_rows, kwargs", [
    ([{"row_id": "a", "target": 0}], {}),
    (independent_rows(4), {}),
    (independent_rows(5) + [{"row_id": "record-0-001", "target": 1}], {}),
    (independent_rows(5) + [{"row_id": "", "target": 1}], {}),
    (independent_rows(5) + [{"row_id": "has,comma", "target": 1}], {}),
    (independent_rows(5) + [{"row_id": "extra-000", "target": None}], {}),
    (independent_rows(5) + [{"row_id": "extra-000", "target": 2}], {}),
    (independent_rows(5), {"group_column": "patient_id"}),
    (independent_rows(5), {"time_column": "timestamp"}),
    (independent_rows(5), {"dataset_id": "uci_credit"}),
    (independent_rows(5), {"dataset_id": "ulb_fraud"}),
    (independent_rows(5), {"seed": -1}),
])
def test_generation_rejects_unsupported_or_unsafe_input(bad_rows, kwargs):
    with pytest.raises(SplitPolicyError):
        generate_binary_iid(bad_rows, **kwargs)


def test_server_ids_are_unique_and_do_not_alias_frozen_families():
    ids = [server_dataset_id() for _ in range(16)]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"ds_[a-f0-9]{32}", value) for value in ids)
    assert not (set(ids) & set(CANONICAL))


def test_generation_minimum_per_class_populates_all_three_partitions():
    generated = generate_binary_iid(independent_rows(5))
    assert generated.partition_counts == {"train": 6, "validation": 2, "test": 2}
