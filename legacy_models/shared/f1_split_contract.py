"""F1 split-policy *reference* utilities.

NOT wired into the upload endpoint. Review the cross-team API/descriptor contract
before production integration. Does not rewrite frozen evidence or model artifacts.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping
from uuid import uuid4

ALGORITHM = "f1-stratified-hash-v1"
SEED = 20261110
PARTITIONS = ("train", "validation", "test")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", re.ASCII)

# Hashes are trusted literals, not populated from an uploaded descriptor.
CANONICAL = {
    "uci_credit": {
        "source_sha256": "30c6be3abd8dcfd3e6096c828bad8c2f011238620f5369220bd60cfc82700933",
        "split_sha256": "e8feb21a0c57c5d6c8344af11aaaab079064f259e4861faad8f4aef40fe6d6cb",
        "counts": {"train": 17981, "validation": 6037, "test": 5982},
        "row_count": 30000,
    },
    "ulb_fraud": {
        "source_sha256": "76274b691b16a6c49d3f159c883398e03ccd6d1ee12d9d8ee38f4b4b98551a89",
        "split_sha256": "5d9edf4df20e866d3198e74893fd2e1cf75f5fc3c5b4e175efd5a4a478b827fa",
        "counts": {"train": 162992, "validation": 61675, "test": 60140},
        "row_count": 284807,
    },
}


class SplitPolicyError(ValueError):
    """Input cannot be accepted without changing or weakening the policy."""


@dataclass(frozen=True)
class GeneratedSplit:
    content: bytes
    sha256: str
    partition_counts: dict[str, int]
    algorithm: str = ALGORITHM
    seed: int = SEED


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_upload_mode(
    dataset_id: str, source_sha256: str, supplied_split: bytes | None = None
) -> str:
    """Return canonical_reused or uploaded after checking *trusted* fingerprints.

    The caller must copy the exact immutable split bytes and validate all source
    membership separately. Do not trust the descriptor to define these digests.
    """
    spec = CANONICAL.get(dataset_id)
    if spec is None:
        raise SplitPolicyError("Unknown canonical dataset family")
    if source_sha256 != spec["source_sha256"]:
        raise SplitPolicyError("Source is not the frozen canonical source")
    if supplied_split is None:
        return "canonical_reused"
    if sha256(supplied_split) != spec["split_sha256"]:
        raise SplitPolicyError("Uploaded split does not match frozen canonical bytes")
    return "uploaded"


def validate_assignments(
    source_ids: Iterable[str], split_csv: bytes
) -> dict[str, int]:
    """Validate exact one-to-one source/split membership for explicit CSVs.

    This function checks IDs, not target labels, source integrity or frozen hashes.
    """
    ids = list(source_ids)
    if not ids or any(not isinstance(x, str) or not x for x in ids):
        raise SplitPolicyError("Source row IDs must be non-empty strings")
    if len(ids) != len(set(ids)):
        raise SplitPolicyError("Duplicate source row ID")
    try:
        text = split_csv.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if reader.fieldnames not in (["row_id", "split"], ["row_id", "partition"]):
            raise SplitPolicyError("Split must have row_id,split or row_id,partition header")
        field = reader.fieldnames[1]
        assignments: dict[str, str] = {}
        for row in reader:
            rid = row.get("row_id")
            part = row.get(field)
            if (not rid or rid in assignments or part not in PARTITIONS or
                    None in row or any(value is None for value in row.values())):
                raise SplitPolicyError("Duplicate, malformed or invalid split assignment")
            assignments[rid] = part
    except (UnicodeError, csv.Error) as exc:
        raise SplitPolicyError("Split CSV is not valid UTF-8 CSV") from exc
    expected = set(ids)
    if set(assignments) != expected:
        raise SplitPolicyError("Missing or unknown row ID assignment")
    counts = dict(Counter(assignments.values()))
    if any(counts.get(p, 0) == 0 for p in PARTITIONS):
        raise SplitPolicyError("All three partitions must be populated")
    return {p: counts[p] for p in PARTITIONS}


def _quotas(size: int) -> tuple[int, int, int]:
    """Largest-remainder 60/20/20 quota; ties train, validation, test."""
    numerators = (6, 2, 2)
    counts = [size * n // 10 for n in numerators]
    remainders = [size * n % 10 for n in numerators]
    for i in sorted(range(3), key=lambda k: (-remainders[k], k))[:size - sum(counts)]:
        counts[i] += 1
    return counts[0], counts[1], counts[2]


def generate_binary_iid(
    rows: Iterable[Mapping[str, object]],
    *,
    dataset_id: str | None = None,
    seed: int = SEED,
    time_column: str | None = None,
    group_column: str | None = None,
) -> GeneratedSplit:
    """Deterministic split of *new* independent 0/1-labeled, stable-ID rows.

    Input records contain explicit row_id and target keys. Row position, Python
    hash randomization, library RNG, and the uploader's filesystem are irrelevant.
    Production ingestion needs a reviewed descriptor mapping for these keys.
    """
    if dataset_id in CANONICAL:
        raise SplitPolicyError("Never generate a replacement for a frozen legacy split")
    if time_column or group_column:
        raise SplitPolicyError("Temporal/grouped datasets require a reviewed split")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise SplitPolicyError("Seed must be a 32-bit nonnegative integer")
    classes: dict[int, list[str]] = {0: [], 1: []}
    seen: set[str] = set()
    for row in rows:
        rid, label = row.get("row_id"), row.get("target")
        if not isinstance(rid, str) or not _ID.fullmatch(rid):
            raise SplitPolicyError("New datasets require stable ASCII row IDs")
        if rid in seen:
            raise SplitPolicyError("Duplicate source row ID")
        seen.add(rid)
        if isinstance(label, bool) or (label not in (0, 1, "0", "1")):
            raise SplitPolicyError("Only complete binary 0/1 classification is supported")
        cls = int(label)
        classes[cls].append(rid)
    if any(len(ids) < 5 for ids in classes.values()):
        raise SplitPolicyError("Require at least five examples of each class")
    assignments: dict[str, str] = {}
    for ids in classes.values():
        ranked = sorted(
            ids,
            key=lambda rid: (
                hashlib.sha256(
                    f"{ALGORITHM}\n{seed}\n{rid}".encode("utf-8")
                ).digest(),
                rid,
            ),
        )
        train, validation, _ = _quotas(len(ranked))
        for position, rid in enumerate(ranked):
            assignments[rid] = (
                "train" if position < train
                else "validation" if position < train + validation
                else "test"
            )
    # IDs are ASCII under this v1 policy; lexicographic order is bytewise order.
    content = ("row_id,split\n" + "".join(
        f"{rid},{assignments[rid]}\n" for rid in sorted(assignments)
    )).encode("utf-8")
    counts = validate_assignments(assignments.keys(), content)
    return GeneratedSplit(content=content, sha256=sha256(content), partition_counts=counts, seed=seed)


def server_dataset_id() -> str:
    """Allocate a new family ID. Persist it transactionally exactly once."""
    return "ds_" + uuid4().hex
