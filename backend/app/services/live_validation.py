"""Score uploads in isolation, then calculate evidence in a separate trusted runtime."""

import asyncio
import csv
import io
import json
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4
from app.config import settings, BACKEND_DIR
from app.services.legacy_scoring import (
    docker,
    create_command,
    validate_predictions,
    ScoringError,
)
from app.services.package_service import (
    digest,
    read_inputs,
    verify_snapshot,
    PackageError,
)


def check(identity, status, required, summary, kind="fresh_execution", details=None):
    return dict(
        check_id=identity,
        status=status,
        required=required,
        summary=summary,
        evidence_kind=kind,
        details=details or {},
    )


async def parity_check(package, inventory, manifest):
    """Cite the official parity run on this exact candidate; never rerun or infer it."""
    if manifest.get("derived_from", {}).get("kind") == "recalibration":
        return check("migration_parity", "NOT_APPLICABLE", False,
                     "Recalibration changes probabilities intentionally; independent validation is required")
    if not manifest.get("derived_from"):
        return check(
            "migration_parity",
            "NOT_ASSESSABLE" if package.language == "R" else "NOT_APPLICABLE",
            package.language == "R",
            "Official parity applies to the Python rebuild of this model, not to the model itself"
            if package.language == "R"
            else "Not a rebuild candidate; reproduction is a separate check",
            "applicability",
        )
    from sqlalchemy import select
    from app.db.session import SessionFactory
    from app.services.parity_service import ParityRunRecord, passed_parity

    async with SessionFactory() as session:
        run = await passed_parity(session, inventory.model_id) or await session.scalar(
            select(ParityRunRecord)
            .where(
                ParityRunRecord.candidate_model_id == inventory.model_id,
                ParityRunRecord.candidate_bundle_sha256 == inventory.bundle_sha256,
            )
            .order_by(ParityRunRecord.created_at.desc())
        )
    if run is None:
        return check(
            "migration_parity",
            "NOT_ASSESSABLE",
            True,
            "No official parity run on this candidate yet",
            "recorded_evidence",
        )
    result = run.result or {}
    return check(
        "migration_parity",
        "PASS" if run.passed else "FAIL",
        True,
        f"Official parity {result.get('method', '')} by {run.initiator}: "
        f"max absolute difference {result.get('max_absolute_difference')}, "
        f"tolerance {result.get('tolerance_absolute')}",
        "recorded_evidence",
        {
            "parity_run_id": run.id,
            "initiator": run.initiator,
            "status": run.status,
            "candidate_bundle_sha256": run.candidate_bundle_sha256,
            **{
                k: result.get(k)
                for k in (
                    "n_rows",
                    "n_above_tolerance",
                    "max_absolute_difference",
                    "tolerance_absolute",
                    "reference_predictions_sha256",
                    "candidate_predictions_sha256",
                    "runtime_image",
                )
            },
        },
    )


async def fixture_checks(package, bundle):
    from app.services.monitoring_service import score_window

    manifest = json.loads((bundle / "manifest.json").read_text())
    fixture = bundle / "fixtures/development_inputs.csv"
    ids = read_inputs(fixture, manifest["feature_order"])
    expected = validate_predictions(
        (bundle / "fixtures/reference_predictions.csv").read_bytes(), ids
    )
    policy = json.loads(
        (settings.legacy_models_root / "validation_policy.json").read_text()
    )
    tolerance = policy["scoring"]["absolute_tolerance"]
    results = []
    with tempfile.TemporaryDirectory(prefix="fixture-evaluation-") as temp:
        root = Path(temp)
        root.chmod(0o755)
        output = root / "scores.csv"
        await score_window(package, fixture, output)
        predicted = list(csv.DictReader(io.StringIO(output.read_text())))
        reference = list(csv.DictReader(io.StringIO(expected.decode())))
        delta = max(
            abs(float(a["positive_probability"]) - float(b["positive_probability"]))
            for a, b in zip(predicted, reference, strict=True)
        )
        results.append(
            check(
                "fixture_scoring",
                "PASS" if delta <= tolerance else "FAIL",
                True,
                "Development fixture reproduction, not official migration parity",
                details={
                    "max_abs_difference": delta,
                    "tolerance": tolerance,
                    "rows": len(ids),
                },
            )
        )
        snapshot = verify_snapshot(package.sha256, package.file_hashes)
        shutil.copytree(snapshot / "package", root / "package")
        (root / "data").mkdir()
        rows = list(csv.DictReader(fixture.open()))
        fields = list(rows[0])
        control_ids = [row["row_id"] for row in rows[:3]]
        with (root / "data/development_inputs.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows[:3])
        control_name = "negative-control-" + uuid4().hex
        try:
            await docker(*create_command(control_name, settings.scoring_image, root, package.language))
            try:
                control_output = await docker("start", "--attach", control_name,
                                              timeout=settings.scoring_timeout_seconds)
                control_exit = int((await docker("inspect", "--format={{.State.ExitCode}}", control_name)).strip())
                validate_predictions(control_output, control_ids)
                control_ok = control_exit == 0
            except (ScoringError, ValueError, KeyError):
                control_ok = False
                control_exit = None
        finally:
            await asyncio.shield(docker("rm", "--force", control_name))
        results.append(check(
            "negative_input_control", "PASS" if control_ok else "FAIL", True,
            "Unchanged input scores every control row" if control_ok else
            "Unchanged control input did not score reliably; malformed-input rejections cannot be credited",
            details={"exit_code": control_exit, "rows": len(control_ids)},
        ))
        if not control_ok:
            return results
        for kind in ("missing_row_id", "duplicated_row_id", "target_leakage"):
            modified = [dict(r) for r in rows[:3]]
            columns = fields[:]
            if kind == "missing_row_id":
                columns.remove("row_id")
                for row in modified:
                    row.pop("row_id")
            elif kind == "duplicated_row_id":
                modified[1]["row_id"] = modified[0]["row_id"]
            else:
                target = (
                    "Class"
                    if manifest["dataset_id"] == "ulb_fraud"
                    else "default payment next month"
                )
                columns.append(target)
                for row in modified:
                    row[target] = "0"
            with (root / "data/development_inputs.csv").open("w") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerows(modified)
            name = "negative-check-" + uuid4().hex
            try:
                await docker(
                    *create_command(
                        name, settings.scoring_image, root, package.language
                    )
                )
                try:
                    await docker(
                        "start",
                        "--attach",
                        name,
                        timeout=settings.scoring_timeout_seconds,
                    )
                except ScoringError:
                    pass
                exit_code = int(
                    (
                        await docker("inspect", "--format={{.State.ExitCode}}", name)
                    ).strip()
                )
                # Timeouts/kills are not evidence that a scorer correctly rejected a schema.
                status = "PASS" if exit_code not in (0, 124, 137) else "FAIL"
                results.append(
                    check(
                        "reject_" + kind,
                        status,
                        True,
                        "Malformed development input rejected"
                        if status == "PASS"
                        else "Scorer did not reliably reject malformed input",
                        details={"exit_code": exit_code},
                    )
                )
            finally:
                await asyncio.shield(docker("rm", "--force", name))
    return results


async def generate_uploaded_validation(package, inventory, dataset):
    from app.services.upload_service import verify_uploaded_bundle, dataset_root
    from app.services.monitoring_service import features_only, score_window, engine
    from app.services.report_service import check_report

    bundle = await asyncio.to_thread(verify_uploaded_bundle, inventory)
    manifest = inventory.manifest
    if manifest["task"] != "binary_classification":
        raise PackageError("NOT_APPLICABLE: documentation-only synthetic model")
    policy = json.loads(
        (settings.legacy_models_root / "validation_policy.json").read_text()
    )
    checks = [
        check(
            "artifact_integrity",
            "PASS",
            True,
            "Uploaded artifact and all package files match stored hashes",
        ),
        check(
            "schema_contract",
            "PASS",
            True,
            "Uploaded manifest and input/output schemas verified",
        ),
    ]
    if manifest.get("ai_native"):
        reproduction = inventory.provenance.get("reproduction", {})
        checks.append(check("fixture_scoring", reproduction.get("status", "NOT_ASSESSABLE"), True,
                            "Fresh-container development reproduction", details=reproduction))
    else:
        checks += await fixture_checks(package, bundle)
    with tempfile.TemporaryDirectory(prefix="live-validation-") as temp:
        root = Path(temp)
        root.chmod(0o755)
        source = dataset_root(dataset)
        hashes = {}
        for part in ("train", "validation", "test"):
            raw = source / f"partitions/{part}.csv"
            if digest(raw) != dataset.statistics["partition_sha256"][part]:
                raise PackageError("Dataset partition hash changed")
            features = root / f"{part}_inputs.csv"
            scores = root / f"{part}_scores.csv"
            await asyncio.to_thread(
                features_only, raw, features, manifest["feature_order"], None, 2_000_000
            )
            await score_window(package, features, scores, max_rows=2_000_000)
            if part != "train":
                shutil.copyfile(raw, root / f"{part}.csv")
            hashes[part] = {
                "partition_sha256": digest(raw),
                "input_sha256": digest(features),
                "prediction_sha256": digest(scores),
            }
        psi = engine()
        baseline = await asyncio.to_thread(
            psi.freeze_baseline,
            model_id=package.name,
            feature_order=manifest["feature_order"],
            reference_inputs=root / "train_inputs.csv",
            reference_scores=root / "train_scores.csv",
            reference_scope="development",
            artifact_sha256=manifest["artifact_sha256"],
            config=policy["stability"],
        )
        stability = await asyncio.to_thread(
            psi.evaluate_window,
            baseline=baseline,
            current_inputs=root / "validation_inputs.csv",
            current_scores=root / "validation_scores.csv",
            expected_model_id=package.name,
            expected_artifact_sha256=manifest["artifact_sha256"],
        )
        stability["scope"] = (
            "train_vs_validation_partition; not an independent production window"
        )
        for key in ("score", "features"):
            values = (
                [stability["score"]]
                if key == "score"
                else list(stability["features"].values())
            )
            statuses = {v["status"] for v in values}
            status = (
                "NOT_ASSESSABLE"
                if "NOT_ASSESSABLE" in statuses
                else "REVIEW"
                if "REVIEW" in statuses
                else "PASS"
            )
            checks.append(
                check(
                    "score_psi" if key == "score" else "feature_psi",
                    status,
                    False,
                    "Shared PSI engine, train-frozen bins and separate validation rows",
                    "fresh_partition_psi",
                    {"result": stability[key]},
                )
            )
        # Training provenance stays a human-review issue: upload hashes prove bytes, not fit history.
        checks.append(
            check(
                "split_provenance",
                "REVIEW",
                True,
                "Uploaded split is verified; original fit membership remains unverified",
                "governance_limitation",
                {
                    "split_sha256": dataset.split_sha256,
                    "original_fit_time_attestation": False,
                },
            )
        )
        if (bundle / "evaluation.json").is_file():
            saved = json.loads((bundle / "evaluation.json").read_text())
        else:
            saved = {}
        (root / "saved_evaluation.json").write_text(json.dumps(saved))
        checks.append(await parity_check(package, inventory, manifest))
        lineage = dict(
            dataset_version_id=dataset.id,
            dataset_sha256=dataset.sha256,
            split_sha256=dataset.split_sha256,
            partitions=hashes,
            package_sha256=package.sha256,
            runtime_image=settings.scoring_image,
        )
        (root / "context.json").write_text(
            json.dumps(
                dict(
                    target_column=dataset.descriptor["target_column"],
                    positive_class=int(dataset.descriptor["positive_class"]),
                    lineage=lineage,
                    checks=checks,
                    stability=stability,
                )
            )
        )
        (root / "manifest.json").write_text(json.dumps(manifest))
        # Copy trusted calculation code only. No uploaded script, serialized model, key or network here.
        for source_path, target in [
            (settings.legacy_models_root / "shared/pipeline.py", "pipeline.py"),
            (
                BACKEND_DIR.parent / "standardize_exl_models.py",
                "standardize_exl_models.py",
            ),
            (BACKEND_DIR.parent / "validate_all_models.py", "validate_all_models.py"),
            (Path(__file__).with_name("validation_metrics.py"), "calculate.py"),
            (
                settings.legacy_models_root / "stability.py",
                "legacy_models/stability.py",
            ),
        ]:
            dst = root / target
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, dst)
        name = "trusted-metrics-" + uuid4().hex
        command = create_command(name, settings.scoring_image, root, "Python")
        command[command.index("--workdir=/inputs/package")] = "--workdir=/inputs"
        at = command.index("timeout")
        command[at:] = [
            "timeout",
            "--signal=KILL",
            str(settings.run_timeout_seconds),
            "python",
            "/inputs/calculate.py",
        ]
        try:
            await docker(*command)
            raw = await docker(
                "start",
                "--attach",
                name,
                timeout=settings.run_timeout_seconds,
                limit=2_000_000,
            )
            if (
                await docker("inspect", "--format={{.State.ExitCode}}", name)
            ).strip() != b"0":
                raise ScoringError("Trusted validation calculation failed")
            report = json.loads(raw)
        finally:
            await asyncio.shield(docker("rm", "--force", name))
        await asyncio.to_thread(check_report, report)
        if (
            report["artifact_sha256"] != manifest["artifact_sha256"]
            or report["model_id"] != package.name
        ):
            raise PackageError("Report binding mismatch")
        return report, {
            "dataset": lineage,
            "validator_sha256": digest(
                Path(__file__).with_name("validation_metrics.py")
            ),
            "policy_sha256": digest(
                settings.legacy_models_root / "validation_policy.json"
            ),
        }
