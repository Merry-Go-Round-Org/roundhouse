"""Export a bounded candidate directory, ingest it through normal upload checks, retain lineage."""

import asyncio
import io
import json
import hashlib
import tempfile
import zipfile
import shutil
from pathlib import Path
from uuid import uuid4
from contracts.harness import ToolResult
from app.db.tables import AuditRecord
from app.services.legacy_scoring import docker
from app.services.artifact_service import save_artifact
from app.services.upload_service import (
    UploadRecord,
    InventoryRecord,
    upload_root,
    process_upload,
    unpack_bundle,
    verify_uploaded_bundle,
)

GUARDED_SCORER = """\
import argparse
import csv
import json
import runpy
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    package = Path(__file__).resolve().parent
    manifest = json.loads((package / "manifest.json").read_text())
    expected = {"row_id", *manifest["feature_order"]}
    with args.input.open(newline="", encoding="utf-8") as stream:
        rows = csv.DictReader(stream)
        columns = rows.fieldnames or []
        if len(columns) != len(expected) or set(columns) != expected:
            parser.error("Input must contain exactly row_id and the approved features")
        seen = set()
        for row in rows:
            row_id = row["row_id"]
            if None in row or any(value is None for value in row.values()):
                parser.error("Input row does not match the approved columns")
            if not row_id.strip() or row_id in seen:
                parser.error("Input row IDs must be nonempty and unique")
            seen.add(row_id)
        if not seen:
            parser.error("Input must contain at least one row")
    runpy.run_path(str(package / "_candidate_scorer.py"), run_name="__main__")


if __name__ == "__main__":
    main()
"""


def guard_candidate_scorer(bundle):
    scorer = bundle / "score.py"
    implementation = bundle / "_candidate_scorer.py"
    if implementation.exists():
        raise ValueError("Candidate uses reserved path _candidate_scorer.py")
    scorer.rename(implementation)
    scorer.write_text(GUARDED_SCORER, encoding="utf-8")
    return implementation


async def export_candidate(task, run, package, relative, sessions):
    if relative != "candidate":
        return ToolResult(status="denied", content="Export the candidate directory")
    script = """import pathlib,io,sys,zipfile
root=pathlib.Path('/workspace/candidate')
assert root.is_dir() and not root.is_symlink()
files=list(root.rglob('*')); assert len(files)<=512
buf=io.BytesIO(); total=0
with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
 for p in files:
  assert not p.is_symlink() and p.resolve().is_relative_to(root)
  if p.is_file():
   assert p.suffix in ('.py','.json','.csv','.txt','.md')
   total+=p.stat().st_size; assert total<=64000000
   z.writestr(str(p.relative_to(root)),p.read_bytes())
assert (root/'score.py').is_file()
sys.stdout.buffer.write(buf.getvalue())
"""
    content = await docker(
        "exec", "mgr-task-" + task.task_id, "python", "-c", script, limit=64_000_000
    )
    async with sessions() as session:
        inventory = await session.get(InventoryRecord, run.model_id)
        from app.services.native_analysis import AnalysisRecord
        goal = await session.get(AnalysisRecord, run.id)
        recalibration = goal is not None and goal.kind == "recalibration"
    if inventory is None:
        return ToolResult(
            status="denied", content="Rebuild export requires an uploaded parent model"
        )
    source = await asyncio.to_thread(verify_uploaded_bundle, inventory)
    with tempfile.TemporaryDirectory(prefix="candidate-export-") as tmp:
        root = Path(tmp)
        archive = root / "raw.zip"
        archive.write_bytes(content)
        bundle = root / "bundle"
        unpack_bundle(archive, bundle)
        try:
            implementation = guard_candidate_scorer(bundle)
        except ValueError as exc:
            return ToolResult(status="denied", content=str(exc))
        files = [p for p in bundle.rglob("*") if p.is_file()]
        # Prefer the extracted parameter payload; a parameter-free scorer is itself the immutable artifact.
        artifact = next(
            (
                p
                for p in files
                if p.suffix == ".json"
                and p.name
                not in ("manifest.json", "schema.json", "bundle_provenance.json")
            ),
            implementation,
        )
        manifest = {
            k: v
            for k, v in inventory.manifest.items()
            if k
            not in (
                "artifact_paths",
                "artifact_sha256",
                "shared_pipeline_sha256",
                "model_code_sha256",
                "scoring_code_sha256",
                "metrics_reference",
                "dataset_sha256_expected",
            )
        }
        manifest.update(
            ai_native=False,
            pass_model_dir=False,
            dataset_sha256=inventory.manifest.get("dataset_sha256")
            or inventory.manifest.get("dataset_sha256_expected"),
            name=inventory.name[:90] + "_rebuilt",
            model_version=inventory.model_version[:20]
            + "-r"
            + run.id[:8]
            + "-"
            + hashlib.sha256(content).hexdigest()[:8],
            language="Python",
            entry_point="score.py",
            artifact_paths=[str(artifact.relative_to(bundle))],
            artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
            derived_from={
                "kind": "recalibration" if recalibration else "migration",
                "model_id": run.model_id,
                "model_version": run.model_version,
                "package_sha256": package.sha256,
                "run_id": run.id,
            },
            limitations=[
                "AI rebuilt candidate; official parity and independent review pending"
            ],
            synthetic_demo=False,
        )
        (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2))
        shutil.copyfile(source / "schema.json", bundle / "schema.json")
        (bundle / "fixtures").mkdir(exist_ok=True)
        for name in ("development_inputs.csv", "reference_predictions.csv"):
            shutil.copyfile(source / "fixtures" / name, bundle / "fixtures" / name)
        checksums = {
            str(p.relative_to(bundle)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in bundle.rglob("*")
            if p.is_file() and p.name != "bundle_provenance.json"
        }
        (bundle / "bundle_provenance.json").write_text(
            json.dumps(
                {
                    "file_sha256": checksums,
                    "bundled_scoring_code_sha256": checksums["score.py"],
                    "source": "agent_candidate",
                    "scoring_input_guard": "exact_columns_and_unique_row_ids",
                    "official_parity": False,
                }
            )
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in bundle.rglob("*"):
                if p.is_file():
                    z.write(p, str(p.relative_to(bundle)))
        raw = buf.getvalue()
    identity = str(uuid4())
    folder = upload_root(identity)
    folder.mkdir(parents=True)
    (folder / "bundle.zip").write_bytes(raw)
    async with sessions() as session:
        session.add(
            UploadRecord(
                id=identity,
                actor_id=task.actor_id,
                status="queued",
                archive_sha256=hashlib.sha256(raw).hexdigest(),
                checks=[],
                files=[],
            )
        )
        await session.commit()
    await process_upload(identity, sessions)
    async with sessions() as session:
        uploaded = await session.get(UploadRecord, identity)
        if uploaded.status != "completed":
            return ToolResult(
                status="error",
                content="Candidate bundle checks failed: " + str(uploaded.error),
            )
        artifact = await save_artifact(
            session, run, raw, "candidate.zip", "application/zip"
        )
        session.add(
            AuditRecord(
                actor_id=task.actor_id,
                resource_id=run.id,
                action="candidate_exported",
                payload={
                    "artifact_id": artifact.id,
                    "candidate_model_id": uploaded.model_id,
                    "upload_id": identity,
                    "official_parity": "pending",
                },
            )
        )
        await session.commit()
        return ToolResult(
            status="ok",
            content=json.dumps(
                {
                    "artifact_id": artifact.id,
                    "candidate_model_id": uploaded.model_id,
                    "sha256": artifact.sha256,
                    "official_parity": "pending",
                }
            ),
        )
