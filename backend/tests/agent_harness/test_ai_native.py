"""The AI-native kinds: profiles, proposals, inputs, budgets, concurrency and output checks."""
from __future__ import annotations

import asyncio
import json
from typing import get_args

import pytest

from contracts import harness as contract
from harness.config import ARCHIVE_KINDS, HarnessSettings
from harness.dev.gateway import FakeGatewayFactory, ScriptedLlm, text_turn, tool_turn
from harness.outputs import (
    MANIFEST_FIELDS,
    PERSON_ONLY_FIELDS,
    fields_needing_confirmation,
    intake_problems,
    investigation_report_problems,
    survey_problems,
    triage_problems,
    Intake,
)
from harness.profiles import DEFAULT_PROFILES, INPUT_DIRS, Profile
from harness.proposals import (
    ACTIONS,
    MONITORING_ACTIONS,
    PROPOSAL_TOOL_NAME,
    proposal_problems,
    proposal_tool_spec,
)
from harness.sandbox import (
    DockerSandbox,
    SandboxConfig,
    SandboxGatewayFactory,
    SandboxMounts,
    SandboxSlots,
    auto_sandbox_limit,
    parse_memory,
)
from harness.schemas import (
    EventType,
    LlmUsage,
    SandboxUnavailable,
    TaskContext,
    TaskInfo,
    TaskKind,
    ToolResult,
)
from harness.validation_report import (
    NARRATIVE_SECTIONS,
    claimed_numbers,
    narrative_problems,
    unsupported_numbers,
)
from harness_helpers import Harness, fast_settings, make_request
from test_sandbox import FakeRunner

NEW_KINDS = ("survey", "intake", "validation", "triage", "investigation")


def play(database, scenario, llm, gateways=None, settings=None):
    async def main():
        async with database as sessions:
            return await scenario(Harness(sessions, llm, settings=settings, gateways=gateways))

    return asyncio.run(main())


# ---------------------------------------------------------------- kinds and contract
def test_every_kind_has_a_profile_and_the_contract_agrees():
    kinds = get_args(TaskKind)
    assert set(NEW_KINDS) <= set(kinds)
    assert set(DEFAULT_PROFILES) == set(kinds)
    assert set(get_args(contract.HarnessRequest.model_fields["kind"].annotation)) == set(kinds)
    HarnessSettings()  # validates a profile per kind


def test_contract_context_and_core_context_have_the_same_fields():
    assert set(contract.RequestContext.model_fields) == set(TaskContext.model_fields)


def test_every_public_event_type_is_in_the_contract():
    public = set(get_args(EventType)) - {"llm_turn"}
    contract_types = set(get_args(contract.HarnessEvent.model_fields["type"].annotation))
    assert public == contract_types


def test_new_ids_reach_the_gateway_through_task_info(database):
    gateways = FakeGatewayFactory()

    async def scenario(h):
        await h.start(
            make_request(
                kind="validation",
                model_package_id="pkg-1",
                metrics_bundle_id="mb-1",
                document_id="doc-1",
            )
        )

    play(database, scenario, ScriptedLlm(lambda _r: text_turn("ok")), gateways)
    info = gateways.opened[0]
    assert (info.metrics_bundle_id, info.document_id, info.model_package_id) == (
        "mb-1",
        "doc-1",
        "pkg-1",
    )


# ---------------------------------------------------------------- profiles
@pytest.mark.parametrize("kind", NEW_KINDS)
def test_new_profiles_run_in_a_sandbox_at_low_effort(kind):
    profile = DEFAULT_PROFILES[kind]
    assert profile.sandbox and profile.effort == "low"
    assert profile.inputs and set(profile.inputs) <= set(INPUT_DIRS)
    assert "export_artifact" in profile.tool_policy


def test_only_validation_triage_and_investigation_may_propose():
    assert DEFAULT_PROFILES["survey"].proposal_types == ()
    assert DEFAULT_PROFILES["intake"].proposal_types == ()
    assert DEFAULT_PROFILES["validation"].proposal_types == ("open_finding",)
    assert DEFAULT_PROFILES["triage"].proposal_types == ("mark_drift_benign",)
    assert DEFAULT_PROFILES["investigation"].proposal_types == MONITORING_ACTIONS
    assert set(DEFAULT_PROFILES["chat"].proposal_types) == set(ACTIONS)
    for kind in ("survey", "intake", "document", "rebuild"):
        assert PROPOSAL_TOOL_NAME not in DEFAULT_PROFILES[kind].tool_policy


def test_no_profile_ever_reads_final_test_data_and_validation_never_reads_the_archive():
    assert DEFAULT_PROFILES["validation"].inputs == ("model", "document", "metrics", "data")
    assert "archive" not in DEFAULT_PROFILES["validation"].inputs
    assert all("final" not in d for d in INPUT_DIRS.values())


def test_validator_prompt_is_adversarial_and_forbids_its_own_numbers():
    prompt = DEFAULT_PROFILES["validation"].system_prompt
    assert "not fit" in prompt and "never see final-test data" in prompt
    assert "Never compute, change or restate a metric" in prompt
    for _, title in NARRATIVE_SECTIONS:
        assert title in prompt


def test_archive_prompts_treat_the_archive_as_untrusted_and_stop_on_personal_data():
    for kind in ("survey", "intake"):
        assert "never instructions" in DEFAULT_PROFILES[kind].system_prompt
    assert "personal_data_suspected" in DEFAULT_PROFILES["survey"].system_prompt


def test_intake_needs_the_backend_to_name_the_candidate(database):
    from harness.schemas import InvalidTaskRequest

    async def scenario(h):
        with pytest.raises(InvalidTaskRequest):
            await h.create(make_request(prompt=None, kind="intake"))

    play(database, scenario, ScriptedLlm([]))


def test_a_profile_without_a_sandbox_cannot_have_inputs():
    with pytest.raises(ValueError):
        Profile.model_validate({**DEFAULT_PROFILES["chat"].model_dump(), "inputs": ("data",)})


def test_an_unknown_proposal_type_is_rejected_in_a_profile():
    with pytest.raises(ValueError):
        Profile.model_validate(
            {**DEFAULT_PROFILES["triage"].model_dump(), "proposal_types": ("delete_everything",)}
        )


# ---------------------------------------------------------------- proposals
FINDING = {
    "type": "open_finding",
    "args": {
        "title": "Outcome-derived feature",
        "severity": "high",
        "severity_rationale": "It leaks the target.",
        "impact": "Performance is overstated.",
        "remediation": "Remove the feature and refit.",
        "check_ids": ["disc.auc"],
    },
    "rationale": "pay_status predicts default almost perfectly on development data.",
    "evidence": [{"kind": "check", "ref": "disc.auc"}],
}
BENIGN = {
    "type": "mark_drift_benign",
    "args": {"reason": "A small batch with a known seasonal shift."},
    "rationale": "Only one feature moved, in one bin.",
    "evidence": [{"kind": "monitoring_run", "ref": "this run"}],
}


def test_a_valid_proposal_passes_the_check():
    assert proposal_problems(("open_finding",), FINDING) == []


def test_a_proposal_outside_the_profile_is_refused():
    problems = proposal_problems(("mark_drift_benign",), FINDING)
    assert problems and "may not propose open_finding" in problems[0]


def test_malformed_proposals_are_refused():
    assert proposal_problems(ACTIONS, {**FINDING, "evidence": []})
    assert proposal_problems(ACTIONS, {**FINDING, "type": "drop_table"})
    assert proposal_problems(ACTIONS, {**FINDING, "args": {**FINDING["args"], "severity": "dire"}})
    # A target ID is never an argument: the backend takes it from the run.
    assert proposal_problems(ACTIONS, {**BENIGN, "args": {**BENIGN["args"], "model_id": "m-2"}})
    assert proposal_problems(ACTIONS, {**FINDING, "approved": True})


def test_the_tool_spec_lists_only_the_given_actions():
    spec = proposal_tool_spec(("mark_drift_benign",))
    assert spec.parameters["properties"]["type"]["enum"] == ["mark_drift_benign"]
    assert "mark_drift_benign" in spec.description and "open_finding" not in spec.description


def test_an_allowed_proposal_reaches_the_backend_and_is_announced(database):
    gateways = FakeGatewayFactory()
    llm = ScriptedLlm([tool_turn((PROPOSAL_TOOL_NAME, BENIGN)), text_turn("Benign.")])

    async def scenario(h):
        _, result = await h.start(make_request(kind="triage"))
        assert result.status == "completed"
        created = h.events_of("proposal_created")
        assert len(created) == 1 and created[0].data["type"] == "mark_drift_benign"
        assert created[0].data["proposal_id"].startswith("proposal-")

    play(database, scenario, llm, gateways)
    assert [c.name for c in gateways.calls] == [PROPOSAL_TOOL_NAME]


def test_a_disallowed_proposal_never_reaches_the_backend_and_can_be_fixed(database):
    gateways = FakeGatewayFactory()
    llm = ScriptedLlm(
        [
            tool_turn((PROPOSAL_TOOL_NAME, FINDING)),
            tool_turn((PROPOSAL_TOOL_NAME, BENIGN)),
            text_turn("Done."),
        ]
    )

    async def scenario(h):
        _, result = await h.start(make_request(kind="triage"))
        assert result.status == "completed"
        first = h.events_of("tool_result")[0]
        assert first.data["status"] == "error" and "may not propose" in first.data["content"]

    play(database, scenario, llm, gateways)
    assert len(gateways.calls) == 1 and gateways.calls[0].args["type"] == "mark_drift_benign"


def test_survey_and_intake_never_see_the_proposal_tool(database):
    llm = ScriptedLlm(lambda _r: text_turn("ok"))

    async def scenario(h):
        await h.start(make_request(kind="survey"))
        await h.start(make_request(kind="intake"))

    play(database, scenario, llm)
    for request in llm.requests:
        assert PROPOSAL_TOOL_NAME not in [t["function"]["name"] for t in request.tools]


def test_a_proposal_id_is_only_valid_on_an_ok_result():
    with pytest.raises(ValueError):
        ToolResult(status="error", content="x", proposal_id="p-1")


# ---------------------------------------------------------------- archive budget
def test_survey_and_intake_of_one_archive_share_a_budget(database):
    expensive = LlmUsage(prompt_tokens=100, completion_tokens=20, cost_usd=0.40)
    llm = ScriptedLlm(lambda _r: text_turn("ok", usage=expensive))
    settings = fast_settings(archive_budget_usd=0.75)

    async def scenario(h):
        _, survey = await h.start(make_request(kind="survey", archive_id="arch-1"))
        _, first = await h.start(make_request("c1", kind="intake", archive_id="arch-1"))
        _, second = await h.start(make_request("c2", kind="intake", archive_id="arch-1"))
        assert (survey.status, first.status) == ("completed", "completed")
        assert second.status == "failed" and second.error_code == "budget_exhausted"
        # Another archive has its own budget, and other kinds never count.
        _, other = await h.start(make_request("c3", kind="intake", archive_id="arch-10"))
        assert other.status == "completed"
        assert await h.store.archive_cost("arch-1", ARCHIVE_KINDS) == pytest.approx(0.80)
        assert await h.store.archive_cost("arch-1", ("validation",)) == 0.0

    play(database, scenario, llm, settings=settings)


def test_tasks_without_an_archive_ignore_the_archive_budget(database):
    llm = ScriptedLlm(lambda _r: text_turn("ok"))

    async def scenario(h):
        _, result = await h.start(make_request(kind="survey"))
        assert result.status == "completed"

    play(database, scenario, llm, settings=fast_settings(archive_budget_usd=0.0))


# ---------------------------------------------------------------- sandbox inputs
def info(kind, workspace="ws1"):
    return TaskInfo(
        task_id="task1",
        thread_id="task1",
        workspace_id=workspace,
        kind=kind,
        actor_id="alice",
        actor_role="validator",
        policy_version="p1",
    )


def test_each_input_is_mounted_read_only_at_its_place():
    mounts = SandboxMounts(
        model_package_path="/h/bundle",
        development_data_path="/h/dev",
        document_path="/h/doc",
        metrics_path="/h/metrics",
    )
    command = DockerSandbox(info("validation"), mounts, SandboxConfig(), FakeRunner()).run_command()
    joined = " ".join(command)
    for src, dst in (
        ("/h/bundle", "/inputs/model"),
        ("/h/dev", "/inputs/data"),
        ("/h/doc", "/inputs/document"),
        ("/h/metrics", "/inputs/metrics"),
    ):
        assert f"type=bind,src={src},dst={dst},readonly" in joined


@pytest.mark.parametrize(
    "kind, mounts",
    [
        ("validation", SandboxMounts(archive_path="/h/archive")),
        ("survey", SandboxMounts(development_data_path="/h/dev")),
        ("rebuild", SandboxMounts(metrics_path="/h/metrics")),
        ("triage", SandboxMounts(model_package_path="/h/bundle")),
    ],
)
def test_an_input_outside_the_profile_fails_the_sandbox(kind, mounts):
    with pytest.raises(SandboxUnavailable, match="may not mount"):
        DockerSandbox(info(kind), mounts, SandboxConfig(), FakeRunner())


def test_a_mount_path_that_could_inject_options_is_refused():
    mounts = SandboxMounts(archive_path="/h/a,dst=/etc")
    with pytest.raises(SandboxUnavailable):
        DockerSandbox(info("survey"), mounts, SandboxConfig(), FakeRunner()).run_command()


def test_the_sandbox_limit_holds_across_tasks():
    runner = FakeRunner()
    slots = SandboxSlots(1)

    async def resolve(_task):
        return SandboxMounts(archive_path="/h/archive")

    factory = SandboxGatewayFactory(
        FakeGatewayFactory(), resolve, runner=runner, slots=slots
    )
    peak = 0

    async def use(task_id):
        nonlocal peak
        task = info("survey", workspace=f"ws-{task_id}").model_copy(update={"task_id": task_id})
        async with factory.open(task):
            peak = max(peak, slots.in_use)
            await asyncio.sleep(0.02)

    async def main():
        await asyncio.gather(*(use(f"t{i}") for i in range(3)))

    asyncio.run(main())
    assert peak == 1 and slots.in_use == 0


def test_the_automatic_limit_divides_the_machine_by_the_container():
    config = SandboxConfig(cpus="2", memory="4g")
    assert auto_sandbox_limit(config, cpus=16, memory=64 * 1024**3) == 8
    assert auto_sandbox_limit(config, cpus=16, memory=8 * 1024**3) == 2
    assert auto_sandbox_limit(config, cpus=1, memory=1024**3) == 1
    assert parse_memory("512m") == 512 * 1024**2


def test_limits_come_from_the_environment():
    settings = HarnessSettings.from_env(
        {
            "HARNESS_MAX_CONCURRENT_SANDBOXES": "3",
            "HARNESS_MAX_CONCURRENT_LLM_REQUESTS": "5",
            "HARNESS_ARCHIVE_BUDGET_USD": "2.5",
            "HARNESS_BACKGROUND_CONCURRENCY": "",
        }
    )
    assert (settings.max_concurrent_sandboxes, settings.llm_concurrency) == (3, 5)
    assert settings.archive_budget_usd == 2.5 and settings.background_concurrency == 8
    config = SandboxConfig.from_env({"HARNESS_SANDBOX_CPUS": "1", "HARNESS_SANDBOX_MEMORY": "2g"})
    assert (config.cpus, config.memory) == ("1", "2g")
    with pytest.raises(ValueError):
        HarnessSettings.from_env({"HARNESS_MAX_CONCURRENT_SANDBOXES": "0"})


def test_the_runner_shares_one_sandbox_limit_with_the_scoring_service(database):
    from harness.runner import HarnessRunner

    async def main():
        async with database as sessions:
            runner = HarnessRunner(
                sessions, ScriptedLlm([]), settings=fast_settings(max_concurrent_sandboxes=2)
            )
            assert runner.sandbox_slots.limit == 2
            assert runner.scoring_service().slots is runner.sandbox_slots
            await runner.shutdown()

    asyncio.run(main())


# ---------------------------------------------------------------- survey output
SURVEY = {
    "personal_data_suspected": False,
    "files": [
        {"path": "pd/model_final.R", "category": "training_code"},
        {"path": "pd/model_final_v2.R", "category": "training_code"},
        {"path": "pd/model.rds", "category": "model_artifact"},
        {"path": "notes.txt", "category": "other"},
    ],
    "candidates": [
        {
            "candidate_id": "c1",
            "name": "PD model",
            "language": "R",
            "fitted_model": "saved_model",
            "files": ["pd/model_final.R", "pd/model_final_v2.R", "pd/model.rds"],
            "current_version": "pd/model_final_v2.R",
            "current_version_reason": "Newer and matches the saved model's features.",
            "superseded_versions": [{"path": "pd/model_final.R", "reason": "Older copy."}],
            "rationale": "One fitted R model with its training code.",
        }
    ],
    "unassigned_files": ["notes.txt"],
}
ARCHIVE = ["pd/model_final.R", "pd/model_final_v2.R", "pd/model.rds", "notes.txt"]


def test_a_good_survey_passes():
    assert survey_problems(json.dumps(SURVEY), ARCHIVE) == []


def test_survey_problems_are_found():
    assert survey_problems("not json") == ["the file is not valid JSON"]
    assert any("not classified" in p for p in survey_problems(json.dumps(SURVEY), [*ARCHIVE, "x.py"]))
    bad = json.loads(json.dumps(SURVEY))
    bad["unassigned_files"] = []
    assert any("in no candidate" in p for p in survey_problems(json.dumps(bad), ARCHIVE))
    bad = json.loads(json.dumps(SURVEY))
    bad["candidates"][0]["current_version"] = None
    assert any("no current version" in p for p in survey_problems(json.dumps(bad), ARCHIVE))
    bad = json.loads(json.dumps(SURVEY))
    bad["files"][0]["path"] = "../etc/passwd"
    assert survey_problems(json.dumps(bad), ARCHIVE)


def test_suspected_personal_data_stops_every_candidate():
    flagged = {**SURVEY, "personal_data_suspected": True, "personal_data_evidence": ["notes.txt: names"]}
    assert any("no candidate may go further" in p for p in survey_problems(json.dumps(flagged), ARCHIVE))
    stopped = {**flagged, "candidates": [], "unassigned_files": []}
    assert survey_problems(json.dumps(stopped), ARCHIVE) == []


# ---------------------------------------------------------------- intake output
def intake(**changes):
    manifest = {
        name: {"value": None, "source": "unknown", "reason": "Not found."} for name in MANIFEST_FIELDS
    }
    manifest["algorithm"] = {"value": "glm", "source": "artifact", "evidence": "Rscript -e 'class(m)'"}
    manifest["features"] = {"value": ["age", "limit"], "source": "artifact", "evidence": "names(coef(m))"}
    manifest["target"] = {"value": "default", "source": "code", "evidence": "pd/model_final_v2.R:12"}
    manifest["risk_tier"] = {"value": "tier 2", "source": "proposed", "reason": "Retail credit use."}
    manifest["owner"] = {"value": "Credit risk", "source": "proposed", "reason": "Old notes name them."}
    data = {
        "candidate_id": "c1",
        "fitted_model": "saved_model",
        "executable": True,
        "manifest": manifest,
        "dataset_mapping": {
            "dataset_id": "uci-credit",
            "feature_map": {"age": "AGE", "limit": "LIMIT_BAL"},
            "rationale": "Same columns.",
        },
        "scoring": {"wrapper": True, "self_check": "Scored 100 development rows."},
    }
    data.update(changes)
    return data


def check_intake(data, **kwargs):
    return intake_problems(
        json.dumps(data),
        candidate_id="c1",
        candidate_files=ARCHIVE,
        dataset_ids=["uci-credit"],
        **kwargs,
    )


def test_a_good_intake_passes_and_lists_fields_to_confirm():
    assert check_intake(intake()) == []
    parsed = Intake.model_validate(intake())
    assert set(fields_needing_confirmation(parsed)) == {"target", "risk_tier", "owner"}


def test_person_only_fields_can_never_be_stated_as_fact():
    data = intake()
    data["manifest"]["owner"] = {"value": "Credit risk", "source": "file", "evidence": "notes.txt"}
    assert any("only a person" in p for p in check_intake(data))
    assert set(PERSON_ONLY_FIELDS) <= set(MANIFEST_FIELDS)


def test_intake_rules_are_enforced():
    assert any("does not match" in p for p in check_intake(intake(candidate_id="c2")))
    data = intake()
    del data["manifest"]["purpose"]
    assert any("missing: purpose" in p for p in check_intake(data))
    data = intake()
    data["manifest"]["target"]["evidence"] = "elsewhere.R"
    assert any("not one of the candidate's files" in p for p in check_intake(data))
    not_running = intake(executable=False, execution_error="package 'glmnet' 2.0 not found")
    problems = check_intake(not_running)
    assert any("needs a model that loads" in p for p in problems)
    assert any("cannot be checked" in p for p in problems)
    assert any("needs the error" in p for p in check_intake(intake(executable=False)))
    training_only = intake(fitted_model="training_code_only")
    assert any("nothing to score" in p or "no fitted model" in p for p in check_intake(training_only))
    unknown_dataset = intake()
    unknown_dataset["dataset_mapping"]["dataset_id"] = "made-up"
    assert any("not on the platform" in p for p in check_intake(unknown_dataset))


# ---------------------------------------------------------------- triage and investigation
def test_triage_output_is_checked():
    good = {"outcome": "benign", "decision_impact": "None.", "reason": "Small batch."}
    assert triage_problems(json.dumps(good)) == []
    problem = {**good, "outcome": "problem"}
    assert triage_problems(json.dumps(problem)) == ["a problem needs drivers or data quality issues"]
    assert triage_problems(json.dumps({**good, "outcome": "fine"}))


def test_investigation_report_needs_its_sections():
    report = "# Investigation\n\n" + "".join(
        f"## {t}\n\nText.\n\n" for t in (
            "Drift attribution", "Population change or data quality", "Impact", "Proposed actions"
        )
    )
    assert investigation_report_problems(report) == []
    assert investigation_report_problems(report.replace("## Impact", "## Effects"))


# ---------------------------------------------------------------- the validator's numbers
BUNDLE = {
    "policy_version": "v1",
    "checks": [
        {"check_id": "disc.auc", "status": "PASS", "values": {"auc": 0.7213, "gini": 0.4426, "rows": 1234}},
        {"check_id": "cal.oe", "status": "REVIEW", "values": {"oe": 1.0731, "diff": -0.0213}},
    ],
}


def narrative(body="AUC is 0.721 (72.1%) on 1,234 rows, and Gini 44.3%."):
    sections = "".join(f"## {n}. {t}\n\n{body}\n\n" for n, t in NARRATIVE_SECTIONS)
    return f"# Validation narrative: PD model\n\nRecommendation: approve with conditions\n\n{sections}"


def test_a_narrative_quoting_only_the_bundle_passes():
    assert narrative_problems(narrative(), BUNDLE) == []


def test_a_number_the_bundle_does_not_have_is_rejected():
    problems = narrative_problems(narrative("AUC is 0.83 and the model is excellent."), BUNDLE)
    assert any("0.83" in p for p in problems)


def test_whole_numbers_must_match_exactly_and_signs_may_be_dropped():
    assert unsupported_numbers("about 1 in practice", BUNDLE) == ["1"]
    assert unsupported_numbers("O/E is off by 0.02", BUNDLE) == []
    assert unsupported_numbers("1234 rows", BUNDLE) == []


def test_references_dates_ids_and_code_are_not_claims():
    text = (
        "See section 4 and Principle 4.1(d) of SS1/23, check `disc.auc`, dated 2026-11-07, "
        "[source: notes/v2.md], model v2 and DISC-01.\n1. a list item"
    )
    assert claimed_numbers(text) == []


def test_enumeration_markers_and_class_labels_are_not_claims():
    # The failed Flow C validation run spent 17 turns guessing which "1, 2" the check meant.
    text = (
        "The assumptions that matter: (1) linearity in the log-odds, (2) independence; "
        "and (3) no leakage. The positive class 1 is a default."
    )
    assert claimed_numbers(text) == []
    # A number in parentheses mid-sentence is still a claim.
    assert claimed_numbers("AUC (2) was reported") == ["2"]


def test_a_rejected_number_is_reported_with_its_line_and_text():
    body = narrative().replace("PD model", "PD model fitted on 17,981 rows")
    problems = narrative_problems(body, BUNDLE)
    message = next(p for p in problems if "numbers not in the metrics bundle" in p)
    line = body[: body.index("17,981")].count("\n") + 1
    assert f"17,981 (line {line}:" in message
    assert "fitted on 17,981 rows" in message


def test_the_narrative_needs_its_sections_and_a_real_recommendation():
    assert any("missing sections" in p for p in narrative_problems("# T\n\nRecommendation: reject\n", BUNDLE))
    bad = narrative().replace("approve with conditions", "maybe")
    assert any("recommendation must be" in p for p in narrative_problems(bad, BUNDLE))
    placeholder = narrative().replace("PD model", "MODEL_NAME")
    assert any("MODEL_NAME" in p for p in narrative_problems(placeholder, BUNDLE))


LIMITATION_BUNDLE = {
    "policy_version": "v1",
    "checks": [
        {"check_id": "disc.auc", "status": "PASS", "values": {"auc": 0.7213, "gini": 0.4426, "rows": 1234}},
        {"check_id": "split_provenance", "status": "REVIEW", "settings": {"required": True},
         "note": "Uploaded split is verified; original fit membership remains unverified"},
        {"check_id": "bootstrap", "status": "REVIEW", "settings": {"required": True},
         "note": "100-resample descriptive bootstrap"},
        {"check_id": "out_of_time", "status": "NOT_ASSESSABLE", "settings": {"required": False},
         "note": "No independent post-deployment time cohort supplied"},
    ],
}


def test_a_required_limitation_must_be_quoted_word_for_word():
    # Flow C retest: the narrative said the original split membership was unverified and
    # dropped that the uploaded split is verified.
    paraphrase = narrative("Original split membership could not be confirmed.")
    problems = narrative_problems(paraphrase, LIMITATION_BUNDLE)
    assert any("split_provenance" in p and "bootstrap" in p for p in problems)
    assert not any("out_of_time" in p for p in problems)  # not required
    quoted = narrative(
        "The platform records: *uploaded split is verified; original fit membership remains "
        "unverified*. Calibration uses a `100-resample` \"100-resample descriptive bootstrap\"."
    )
    # Quoted notes pass, and the number inside a quoted note is not the writer's claim.
    assert narrative_problems(quoted, LIMITATION_BUNDLE) == []
