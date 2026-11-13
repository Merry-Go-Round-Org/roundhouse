"""The handoff examples stay valid, and the export checks and correction rounds behave."""
from __future__ import annotations

import json

import pytest

from contracts.harness import HarnessRequest, HarnessResult
from harness.handoff import (
    AI_NATIVE_KINDS,
    EXAMPLE_CHECK_ARGS,
    EXAMPLE_EXPORTS,
    EXAMPLE_PROPOSALS,
    EXAMPLE_REQUESTS,
    EXAMPLE_RESULTS,
    KIND_SPECS,
    MAX_PROBLEMS_IN_PROMPT,
    check_exports,
    correction_prompt,
    intake_prompt,
    request_problems,
)
from harness.metrics_bundle import bundle_from_validation_report
from harness.profiles import DEFAULT_PROFILES


@pytest.mark.parametrize("kind", AI_NATIVE_KINDS)
def test_each_example_request_is_a_valid_contract_request(kind):
    request = HarnessRequest.model_validate(EXAMPLE_REQUESTS[kind])
    assert request.kind == kind
    assert request_problems(kind, request.context.model_dump(), request.prompt) == []


@pytest.mark.parametrize("kind", AI_NATIVE_KINDS)
def test_each_example_export_passes_the_backend_checks(kind):
    check = check_exports(
        kind, EXAMPLE_EXPORTS[kind], proposals=EXAMPLE_PROPOSALS[kind], **EXAMPLE_CHECK_ARGS[kind]
    )
    assert check.problems == []
    assert set(check.used) == set(EXAMPLE_EXPORTS[kind])


@pytest.mark.parametrize("kind", AI_NATIVE_KINDS)
def test_each_example_result_is_a_valid_contract_result(kind):
    result = HarnessResult.model_validate(EXAMPLE_RESULTS[kind])
    assert result.status == "completed"
    assert len(result.proposal_ids) == len(EXAMPLE_PROPOSALS[kind])


@pytest.mark.parametrize("kind", AI_NATIVE_KINDS)
def test_specs_match_the_profiles(kind):
    spec, profile = KIND_SPECS[kind], DEFAULT_PROFILES[kind]
    assert spec.inputs == profile.inputs
    assert set(spec.proposals) == set(profile.proposal_types)
    assert spec.prompt_required == (profile.default_prompt is None)
    assert spec.correctable and spec.not_correctable


def test_a_request_without_its_inputs_is_refused_before_it_starts():
    assert request_problems("validation", {"policy_version": "1", "run_id": "r"}, None) == [
        "context is missing: model_id, model_package_id, development_data_id, document_id, "
        "metrics_bundle_id"
    ]
    context = EXAMPLE_REQUESTS["intake"]["context"]
    assert "intake has no default prompt" in request_problems("intake", context, " ")[0]
    assert request_problems("chat", {}, "hi") == ["chat is not an AI-native kind"]


def test_intake_prompt_names_the_candidate_and_its_files():
    survey = json.loads(EXAMPLE_EXPORTS["survey"]["survey.json"])
    prompt = intake_prompt(survey["candidates"][0])
    assert "credit_glm" in prompt and "- credit/score.R" in prompt


def test_missing_exports_and_disallowed_proposals_are_problems():
    check = check_exports("triage", {}, proposals=["restrict_use"])
    assert any("triage.json was not exported" in p for p in check.problems)
    assert any("triage_report.md was not exported" in p for p in check.problems)
    assert "triage may not propose: restrict_use" in check.problems


def test_survey_must_cover_the_backend_file_list():
    check = check_exports(
        "survey", EXAMPLE_EXPORTS["survey"], archive_files=("credit/model.rds", "other.csv")
    )
    assert any("files not in the archive" in p for p in check.problems)
    assert any("archive files not classified: other.csv" in p for p in check.problems)


def test_an_intake_wrapper_must_be_exported_when_claimed():
    intake = json.loads(EXAMPLE_EXPORTS["intake"]["intake.json"])
    intake["scoring"] = {"entry_point": None, "wrapper": True, "self_check": "ok"}
    files = {"intake.json": json.dumps(intake).encode()}
    args = EXAMPLE_CHECK_ARGS["intake"]
    assert any("scoring_wrapper/score.py was not exported" in p for p in check_exports("intake", files, **args).problems)
    files["scoring_wrapper/score.py"] = b"print()"
    assert check_exports("intake", files, **args).problems == []
    # A wrapper exported earlier and no longer claimed is ignored: an export cannot be withdrawn.
    intake["scoring"] = {"entry_point": "credit/score.R", "wrapper": False, "self_check": "ok"}
    files["intake.json"] = json.dumps(intake).encode()
    check = check_exports("intake", files, **args)
    assert check.problems == [] and "scoring_wrapper" not in check.used


def test_a_narrative_number_missing_from_the_bundle_is_correctable():
    text = EXAMPLE_EXPORTS["validation"]["validation_narrative.md"].decode().replace("0.716", "0.81")
    check = check_exports(
        "validation", {"validation_narrative.md": text.encode()}, **EXAMPLE_CHECK_ARGS["validation"]
    )
    assert any("0.81" in p for p in check.problems)
    assert check_exports("validation", {"validation_narrative.md": text.encode()}).problems[-1] == (
        "the backend must pass the metrics bundle the validator saw"
    )


def test_triage_outcome_and_proposals_must_agree():
    files = dict(EXAMPLE_EXPORTS["triage"])
    assert any("mark_drift_benign" in p for p in check_exports("triage", files).problems)
    problem = json.loads(files["triage.json"]) | {"outcome": "problem"}
    files["triage.json"] = json.dumps(problem).encode()
    assert check_exports("triage", files).problems == []
    assert any("proposes nothing" in p for p in check_exports("triage", files, proposals=["mark_drift_benign"]).problems)


def test_an_investigation_must_propose_something():
    check = check_exports("investigation", EXAMPLE_EXPORTS["investigation"])
    assert check.problems == ["propose at least one action with create_proposal"]


def test_correction_prompt_lists_problems_and_paths_and_is_bounded():
    prompt = correction_prompt("triage", [f"problem {i} " + "x" * 1000 for i in range(50)])
    assert "triage.json, triage_report.md" in prompt
    assert prompt.count("\n- ") == MAX_PROBLEMS_IN_PROMPT
    assert max(len(line) for line in prompt.splitlines()) < 400


def test_the_example_bundle_matches_a_bundle_built_from_a_report():
    report = {
        "report_kind": "legacy_model_validation",
        "artifact_sha256": "a" * 64,
        "policy_version": "1.0.0",
        "checks": [],
        "saved_metrics": {
            "fresh_evaluation": {
                "validation": {"roc_auc": 0.7213, "gini": 0.4426},
                "test": {"roc_auc": 0.7158, "gini": 0.4316},
            }
        },
    }
    bundle = bundle_from_validation_report(report)
    text = EXAMPLE_EXPORTS["validation"]["validation_narrative.md"].decode()
    assert check_exports("validation", {"validation_narrative.md": text.encode()}, metrics_bundle=bundle.model_dump(mode="json")).problems == []
