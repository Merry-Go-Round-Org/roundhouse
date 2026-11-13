"""Input-contract probes for a Python candidate scorer.

The rebuild agent gets these results with every official parity run, so it can fix a scorer
that accepts malformed input before Gate 2. Each probe scores a few development rows in the
same fresh, network-less container official scoring uses.

A control run with the unchanged rows comes first. It must exit 0 and score every row in
order; only then does a non-zero exit on a malformed input count as a rejection. A scorer that
crashes for an unrelated reason therefore never passes a probe.
"""

import asyncio
import csv
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from app.config import settings
from app.services.legacy_scoring import (
    ScoringError,
    create_command,
    docker,
    validate_predictions,
)
from app.services.package_service import verify_snapshot

PROBES = ("missing_row_id", "duplicated_row_id", "target_leakage")
TARGETS = {"ulb_fraud": "Class", "uci_credit": "default payment next month"}
PROBE_ROWS = 3
# Exit codes from `timeout --signal=KILL` and the OOM killer: not evidence of a rejection.
KILLED = (124, 137)


def _rows(snapshot):
    with (snapshot / "data/development_inputs.csv").open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), [row for _, row in zip(range(PROBE_ROWS), reader)]


def probe_input(kind, columns, rows, target):
    """Return (columns, rows) for one probe. `control` is the input unchanged."""
    columns, rows = columns[:], [dict(r) for r in rows]
    if kind == "missing_row_id":
        columns.remove("row_id")
        for row in rows:
            row.pop("row_id")
    elif kind == "duplicated_row_id":
        rows[1]["row_id"] = rows[0]["row_id"]
    elif kind == "target_leakage":
        columns.append(target)
        for row in rows:
            row[target] = "0"
    return columns, rows


async def _score(root, columns, rows, language):
    """Score one input in a fresh container. Returns (exit_code, stdout)."""
    data = root / "data"
    data.mkdir(exist_ok=True)
    with (data / "development_inputs.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    for path in (root, data, data / "development_inputs.csv"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    name = "input-contract-" + uuid4().hex
    await docker(*create_command(name, settings.scoring_image, root, language))
    try:
        stdout = b""
        try:
            stdout = await docker(
                "start", "--attach", name, timeout=settings.scoring_timeout_seconds
            )
        except ScoringError:
            pass
        code = int((await docker("inspect", "--format={{.State.ExitCode}}", name)).strip())
        return code, stdout
    finally:
        await asyncio.shield(docker("rm", "--force", name))


async def input_contract_checks(package, dataset_id):
    """Run the control and the three probes on a registered package.

    Returns {"passed": bool, "control": {...}, "checks": [{check_id, status, exit_code,
    reason}]}. Status is PASS, FAIL, or NOT_RUN when the control did not score cleanly.
    """
    snapshot = await asyncio.to_thread(verify_snapshot, package.sha256, package.file_hashes)
    columns, rows = await asyncio.to_thread(_rows, snapshot)
    target = TARGETS.get(dataset_id, "target")
    with tempfile.TemporaryDirectory(prefix="input-contract-") as temp:
        root = Path(temp)
        shutil.copytree(snapshot / "package", root / "package")
        code, stdout = await _score(root, columns, rows, package.language)
        control_ok = code == 0
        reason = "Unchanged rows scored" if control_ok else f"Exited {code} on unchanged rows"
        if control_ok:
            try:
                validate_predictions(stdout, [r["row_id"] for r in rows])
            except ScoringError as exc:
                control_ok, reason = False, str(exc)
        control = {"status": "PASS" if control_ok else "FAIL", "exit_code": code, "reason": reason}
        checks = []
        for kind in PROBES:
            check_id = "reject_" + kind
            if not control_ok:
                checks.append(dict(check_id=check_id, status="NOT_RUN", exit_code=None,
                                   reason="The unchanged rows did not score cleanly, so a "
                                          "rejection here would prove nothing"))
                continue
            code, _ = await _score(root, *probe_input(kind, columns, rows, target),
                                   package.language)
            rejected = code not in (0, *KILLED)
            checks.append(dict(
                check_id=check_id,
                status="PASS" if rejected else "FAIL",
                exit_code=code,
                reason=(f"Rejected with exit code {code}" if rejected else
                        "Scored the malformed input instead of exiting non-zero"
                        if code == 0 else "Killed or timed out; not a rejection"),
            ))
    return {
        "passed": control_ok and all(c["status"] == "PASS" for c in checks),
        "control": control,
        "checks": checks,
    }


async def save_evidence(sessions, parity_run_id, contract):
    """Keep the probe result on the agent parity run it was produced with.

    Gate 2 shows it from there, so opening a review never runs Docker again. A person's parity
    rerun does not run the probes and leaves this evidence unchanged.
    """
    from datetime import datetime, timezone
    from app.services.parity_service import ParityRunRecord

    async with sessions() as session:
        record = await session.get(ParityRunRecord, parity_run_id)
        if record is None:
            return
        record.result = {
            **(record.result or {}),
            "input_contract": {
                **contract,
                "executed_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        await session.commit()


async def gate_2_evidence(session, candidate_model_id):
    """The saved probe result for a Gate 2 candidate, bound to its exact bundle, or None.

    None means no saved result: a candidate exported before the probes existed, or one whose
    agent never ran parity. It is never inferred from parity passing.
    """
    from sqlalchemy import select
    from app.services.parity_service import ParityRunRecord
    from app.services.upload_service import InventoryRecord

    inventory = await session.get(InventoryRecord, candidate_model_id) if candidate_model_id else None
    if inventory is None:
        return None
    records = await session.scalars(
        select(ParityRunRecord)
        .where(
            ParityRunRecord.candidate_model_id == candidate_model_id,
            ParityRunRecord.candidate_bundle_sha256 == inventory.bundle_sha256,
            ParityRunRecord.initiator == "agent",
        )
        .order_by(ParityRunRecord.created_at.desc())
    )
    for record in records:
        contract = (record.result or {}).get("input_contract")
        if contract:
            return {
                **contract,
                "parity_run_id": record.id,
                "rebuild_run_id": record.rebuild_run_id,
                "candidate_model_id": record.candidate_model_id,
                "candidate_version": record.candidate_version,
                "candidate_bundle_sha256": record.candidate_bundle_sha256,
            }
    return None
