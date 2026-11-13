"""What the backend needs to start, check and correct each AI-native kind.

One `KindSpec` per kind: the trusted context IDs the request must carry, the sandbox inputs the
resolver may mount, the files the agent exports, the proposals it may make, and the failures a
follow-up round can correct. `check_exports` runs every structural check the backend must run
before it uses an export, and `correction_prompt` is the prompt of the follow-up round.

The examples at the end are the handoff examples: one request, export and terminal result per
kind. `tests/agent_harness/test_handoff.py` checks that each example request is a valid
`HarnessRequest`, each example export passes `check_exports`, and each example result is a valid
`HarnessResult`, so the examples cannot drift from the code.

How a follow-up round works:

1. The task ends `completed`. The backend reads the exports it saved for the run and calls
   `check_exports`. An empty list means it may use them.
2. Otherwise, while `round < MAX_CORRECTION_ROUNDS`, the backend sends a new `HarnessRequest` with
   a new `request_id` (such as `<run_id>:fix-1`), the same kind, actor and context,
   `resume_from` set to the result's `task_id`, and `prompt=correction_prompt(kind, problems)`.
   The task continues the same thread and workspace, so the agent fixes the file in place.
3. After the last round, or for a failure that is not correctable, the run fails with the
   problems as its reason. Nothing from a rejected export is used.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal

from harness.outputs import (
    CHALLENGER_DIR,
    INTAKE_FILE,
    INVESTIGATION_REPORT_FILE,
    SCORING_ENTRY_POINT,
    SCORING_WRAPPER_DIR,
    SURVEY_FILE,
    TRIAGE_FILE,
    TRIAGE_REPORT_FILE,
    intake_problems,
    investigation_report_problems,
    survey_problems,
    triage_problems,
)
from harness.proposals import MONITORING_ACTIONS
from harness.validation_report import NARRATIVE_FILE, narrative_problems

AiNativeKind = Literal["survey", "intake", "validation", "triage", "investigation"]
AI_NATIVE_KINDS: tuple[AiNativeKind, ...] = ("survey", "intake", "validation", "triage", "investigation")

MAX_CORRECTION_ROUNDS = 2
MAX_PROBLEM_CHARS = 300
MAX_PROBLEMS_IN_PROMPT = 20

# Every task needs these; the per-kind list adds the inputs.
COMMON_CONTEXT: tuple[str, ...] = ("policy_version", "run_id")


@dataclass(frozen=True)
class Export:
    path: str  # what the agent passes to export_artifact
    kind: Literal["file", "dir"]
    required: bool = True
    media_type: str = "application/json"


@dataclass(frozen=True)
class KindSpec:
    kind: AiNativeKind
    context: tuple[str, ...]  # RequestContext fields that must be set, besides COMMON_CONTEXT
    inputs: tuple[str, ...]  # sandbox inputs, the same as the profile's
    exports: tuple[Export, ...]
    proposals: tuple[str, ...]
    prompt_required: bool  # True when the profile has no default prompt
    # Failures a follow-up round can fix. Everything else fails the run.
    correctable: tuple[str, ...]
    not_correctable: tuple[str, ...]
    next_step: str  # what the backend does with an accepted export
    extra: dict[str, Any] = field(default_factory=dict)


_NOT_CORRECTABLE_COMMON = (
    "a failed task: budget_exhausted, turn_limit_exceeded, context_too_large, sandbox_unavailable, "
    "llm_* errors and internal_error. Start a new run if the cause is fixed.",
    "a cancelled or interrupted task: the backend's recovery restarts or fails the run.",
)

KIND_SPECS: dict[AiNativeKind, KindSpec] = {
    "survey": KindSpec(
        kind="survey",
        context=("archive_id",),
        inputs=("archive",),
        exports=(Export(SURVEY_FILE, "file"),),
        proposals=(),
        prompt_required=False,
        correctable=(
            "survey.json is missing, is not valid JSON or breaks the schema",
            "survey.json lists files that are not in the archive, or leaves archive files out",
            "a candidate's files or versions are inconsistent",
        ),
        not_correctable=(
            "personal_data_suspected is true with evidence: a valid outcome, not a failure. "
            "The backend quarantines the archive and starts no intake.",
            *_NOT_CORRECTABLE_COMMON,
        ),
        next_step="Start one intake run per candidate, with the candidate's ID and files in the prompt.",
    ),
    "intake": KindSpec(
        kind="intake",
        context=("archive_id", "candidate_id", "development_data_id"),
        inputs=("archive", "data"),
        exports=(
            Export(INTAKE_FILE, "file"),
            Export(SCORING_WRAPPER_DIR, "dir", required=False, media_type="application/zip"),
        ),
        proposals=(),
        prompt_required=True,
        correctable=(
            "intake.json is missing, is not valid JSON or breaks the schema",
            "manifest fields are missing, unknown, or lack a source, evidence or reason",
            "a person-only field (owner, actual_use, materiality, approval_history) is not proposed or unknown",
            "the scoring entry point or evidence is not one of the candidate's files",
            "intake.json says a wrapper was written but scoring_wrapper/score.py was not exported",
            "the dataset mapping names a dataset that is not on the platform",
        ),
        not_correctable=(
            "executable false with an execution_error: a valid outcome. Gate 0 shows it, and the "
            "model can be documented but not scored.",
            *_NOT_CORRECTABLE_COMMON,
        ),
        next_step=(
            "Show the plan at Gate 0. After approval, build the bundle at the archive's paths and "
            "run ScoringService.reproduction_check (with pass_model_dir for a wrapper)."
        ),
    ),
    "validation": KindSpec(
        kind="validation",
        context=("model_id", "model_package_id", "development_data_id", "document_id", "metrics_bundle_id"),
        inputs=("model", "document", "metrics", "data"),
        exports=(
            Export(NARRATIVE_FILE, "file", media_type="text/markdown"),
            Export(CHALLENGER_DIR, "dir", required=False, media_type="application/zip"),
        ),
        proposals=("open_finding",),
        prompt_required=False,
        correctable=(
            "validation_narrative.md is missing, or its sections, title or Recommendation line are wrong",
            "the narrative writes a number that is not in the metrics bundle",
            "the narrative still has template guide lines or HTML",
            "challenger/ was exported without score.py at its root",
        ),
        not_correctable=(
            "a structurally valid narrative with a wrong judgement: only the Gate 3 reviewer can "
            "catch it. Passing check_exports is not a correct validation.",
            *_NOT_CORRECTABLE_COMMON,
        ),
        next_step=(
            "Score challenger/ on validation and final-test data with the scoring service, add its "
            "aggregates to the bundle, and open the Gate 3 review with the narrative and the "
            "open_finding proposals."
        ),
    ),
    "triage": KindSpec(
        kind="triage",
        context=("model_id", "monitoring_run_id", "development_data_id"),
        inputs=("monitoring", "data"),
        exports=(
            Export(TRIAGE_FILE, "file"),
            Export(TRIAGE_REPORT_FILE, "file", media_type="text/markdown"),
        ),
        proposals=("mark_drift_benign",),
        prompt_required=False,
        correctable=(
            "triage.json or triage_report.md is missing, or triage.json breaks the schema",
            "a problem outcome has no drivers or data quality issues",
            "a benign outcome without a mark_drift_benign proposal",
            "a problem outcome with a proposal (triage proposes nothing for a problem)",
        ),
        not_correctable=_NOT_CORRECTABLE_COMMON,
        next_step=(
            "benign: show the mark_drift_benign card. problem: start an investigation run with "
            "triage_report.md in the monitoring input."
        ),
    ),
    "investigation": KindSpec(
        kind="investigation",
        context=("model_id", "monitoring_run_id", "development_data_id", "document_id", "validation_report_id"),
        inputs=("monitoring", "data", "document", "reports"),
        exports=(Export(INVESTIGATION_REPORT_FILE, "file", media_type="text/markdown"),),
        proposals=MONITORING_ACTIONS,
        prompt_required=False,
        correctable=(
            "investigation_report.md is missing, or its title or sections are wrong",
            "the report still has template guide lines or HTML",
            "no proposal was made (an investigation always proposes at least one action)",
        ),
        not_correctable=_NOT_CORRECTABLE_COMMON,
        next_step="Show the report and one card per proposal to the roles in REQUIRED_ROLES.",
    ),
}


# ---------------------------------------------------------------- the request
def request_problems(kind: str, context: dict[str, Any], prompt: str | None) -> list[str]:
    """What stops the backend from sending this request. Run it before `Harness.run`."""
    spec = KIND_SPECS.get(kind)  # type: ignore[arg-type]
    if spec is None:
        return [f"{kind} is not an AI-native kind"]
    missing = [name for name in (*COMMON_CONTEXT, *spec.context) if not context.get(name)]
    problems = ["context is missing: " + ", ".join(missing)] if missing else []
    if spec.prompt_required and not (prompt and prompt.strip()):
        problems.append(f"{kind} has no default prompt: send the candidate as the prompt")
    return problems


def intake_prompt(candidate: dict[str, Any]) -> str:
    """The intake prompt from one survey candidate. The backend sends it as `prompt`."""
    files = "\n".join(f"- {path}" for path in candidate["files"])
    lines = [
        f"Run intake for candidate {candidate['candidate_id']} ({candidate['name']}, "
        f"{candidate['language']}, fitted_model {candidate['fitted_model']}).",
        f"Its files, relative to the archive root:\n{files}",
    ]
    if candidate.get("current_version"):
        lines.append(f"The survey judged {candidate['current_version']} the current version.")
    return "\n".join(lines)


# ---------------------------------------------------------------- the exports
@dataclass
class ExportCheck:
    problems: list[str]
    used: dict[str, str]  # export path -> what it is, for the record

    @property
    def ok(self) -> bool:
        return not self.problems


def check_exports(
    kind: str,
    files: dict[str, bytes],
    *,
    proposals: Iterable[str] = (),
    archive_files: Iterable[str] | None = None,
    candidate_id: str | None = None,
    candidate_files: Iterable[str] | None = None,
    dataset_ids: Iterable[str] | None = None,
    metrics_bundle: Any = None,
) -> ExportCheck:
    """Every check the backend runs before it uses a run's exports.

    `files` maps each exported file to its bytes. A folder export appears as its files, such as
    `scoring_wrapper/score.py`. `proposals` lists the types of the proposals the run created.
    """
    spec = KIND_SPECS.get(kind)  # type: ignore[arg-type]
    if spec is None:
        return ExportCheck([f"{kind} is not an AI-native kind"], {})
    problems: list[str] = []
    used: dict[str, str] = {}
    for export in spec.exports:
        present = (
            export.path in files
            if export.kind == "file"
            else any(p.startswith(export.path + "/") for p in files)
        )
        if present:
            used[export.path] = export.media_type
        elif export.required:
            problems.append(f"{export.path} was not exported: call export_artifact with path {export.path}")
    proposals = list(proposals)
    disallowed = sorted({p for p in proposals if p not in spec.proposals})
    if disallowed:
        problems.append(f"{kind} may not propose: {', '.join(disallowed)}")
    if spec.kind == "intake" and SCORING_WRAPPER_DIR in used and not _claims_wrapper(files):
        # An export cannot be withdrawn, so a wrapper the agent no longer claims is ignored.
        del used[SCORING_WRAPPER_DIR]
    check = _CHECKS[spec.kind]
    problems += check(
        files,
        proposals=proposals,
        archive_files=archive_files,
        candidate_id=candidate_id,
        candidate_files=candidate_files,
        dataset_ids=dataset_ids,
        metrics_bundle=metrics_bundle,
    )
    return ExportCheck(problems, used)


def _text(files: dict[str, bytes], path: str) -> str | None:
    raw = files.get(path)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _claims_wrapper(files: dict[str, bytes]) -> bool:
    try:
        return bool(json.loads(files[INTAKE_FILE]).get("scoring", {}).get("wrapper"))
    except (KeyError, ValueError, AttributeError):
        return False


def _check_survey(files: dict[str, bytes], *, archive_files: Any, **_: Any) -> list[str]:
    text = _text(files, SURVEY_FILE)
    if text is None:
        return []
    return [f"{SURVEY_FILE}: {p}" for p in survey_problems(text, archive_files)]


def _check_intake(
    files: dict[str, bytes], *, candidate_id: Any, candidate_files: Any, dataset_ids: Any, **_: Any
) -> list[str]:
    text = _text(files, INTAKE_FILE)
    if text is None:
        return []
    problems = [
        f"{INTAKE_FILE}: {p}"
        for p in intake_problems(
            text, candidate_id=candidate_id, candidate_files=candidate_files, dataset_ids=dataset_ids
        )
    ]
    if not problems:
        wrapper = bool(json.loads(text).get("scoring", {}).get("wrapper"))
        entry = f"{SCORING_WRAPPER_DIR}/{SCORING_ENTRY_POINT}"
        if wrapper and entry not in files:
            problems.append(f"{INTAKE_FILE} says a wrapper was written, but {entry} was not exported")
    return problems


def _check_validation(files: dict[str, bytes], *, metrics_bundle: Any, **_: Any) -> list[str]:
    problems: list[str] = []
    text = _text(files, NARRATIVE_FILE)
    if text is not None:
        if metrics_bundle is None:
            problems.append("the backend must pass the metrics bundle the validator saw")
        else:
            problems += [f"{NARRATIVE_FILE}: {p}" for p in narrative_problems(text, metrics_bundle)]
    challenger = [p for p in files if p.startswith(CHALLENGER_DIR + "/")]
    if challenger and f"{CHALLENGER_DIR}/{SCORING_ENTRY_POINT}" not in files:
        problems.append(f"{CHALLENGER_DIR}/ has no {SCORING_ENTRY_POINT} at its root")
    return problems


def _check_triage(files: dict[str, bytes], *, proposals: list[str], **_: Any) -> list[str]:
    text = _text(files, TRIAGE_FILE)
    if text is None:
        return []
    problems = [f"{TRIAGE_FILE}: {p}" for p in triage_problems(text)]
    if problems:
        return problems
    report = _text(files, TRIAGE_REPORT_FILE)
    if report is not None and not report.strip():
        problems.append(f"{TRIAGE_REPORT_FILE} is empty")
    outcome = json.loads(text)["outcome"]
    if outcome == "benign" and "mark_drift_benign" not in proposals:
        problems.append("a benign outcome needs a mark_drift_benign proposal: call create_proposal")
    if outcome == "problem" and proposals:
        problems.append("a problem outcome proposes nothing: the platform starts an investigation")
    return problems


def _check_investigation(files: dict[str, bytes], *, proposals: list[str], **_: Any) -> list[str]:
    text = _text(files, INVESTIGATION_REPORT_FILE)
    problems: list[str] = []
    if text is not None:
        problems += [f"{INVESTIGATION_REPORT_FILE}: {p}" for p in investigation_report_problems(text)]
    if not proposals:
        problems.append("propose at least one action with create_proposal")
    return problems


_CHECKS: dict[AiNativeKind, Callable[..., list[str]]] = {
    "survey": _check_survey,
    "intake": _check_intake,
    "validation": _check_validation,
    "triage": _check_triage,
    "investigation": _check_investigation,
}


def correction_prompt(kind: str, problems: Iterable[str]) -> str:
    """The prompt of a follow-up round. Problems come from the checks above, never from the agent."""
    spec = KIND_SPECS[kind]  # type: ignore[index]
    items = [p[:MAX_PROBLEM_CHARS] for p in problems][:MAX_PROBLEMS_IN_PROMPT]
    paths = ", ".join(e.path for e in spec.exports if e.required)
    return (
        "The platform checked your exports and could not accept them. Fix these problems in your "
        f"workspace, export again with the same paths ({paths}), and finish with a one-sentence "
        "summary. Do not start over.\n" + "\n".join(f"- {p}" for p in items)
    )


# ---------------------------------------------------------------- examples
def _context(**ids: str) -> dict[str, Any]:
    return {"policy_version": "1.0.0", "run_id": "run-0001", **ids}


EXAMPLE_REQUESTS: dict[AiNativeKind, dict[str, Any]] = {
    "survey": {
        "request_id": "run-0001",
        "actor": {"id": "user-7", "role": "developer"},
        "kind": "survey",
        "context": _context(archive_id="archive-42"),
    },
    "intake": {
        "request_id": "run-0002",
        "actor": {"id": "user-7", "role": "developer"},
        "kind": "intake",
        "prompt": "Run intake for candidate credit_glm (credit default GLM, R, fitted_model saved_model).\n"
        "Its files, relative to the archive root:\n- credit/model.rds\n- credit/score.R\n- credit/notes.md",
        "context": _context(
            run_id="run-0002", archive_id="archive-42", candidate_id="credit_glm",
            development_data_id="dev-data-all",
        ),
    },
    "validation": {
        "request_id": "run-0003",
        "actor": {"id": "user-9", "role": "validator"},
        "kind": "validation",
        "context": _context(
            run_id="run-0003", model_id="model-5", model_package_id="package-5",
            development_data_id="dev-5", document_id="report-doc-5",
            metrics_bundle_id="report-val-5",
        ),
    },
    "triage": {
        "request_id": "run-0004",
        "actor": {"id": "system-monitor", "role": "validator"},
        "kind": "triage",
        "context": _context(
            run_id="run-0004", model_id="model-5", monitoring_run_id="monitor-run-8",
            development_data_id="dev-5",
        ),
    },
    "investigation": {
        "request_id": "run-0005",
        "actor": {"id": "system-monitor", "role": "validator"},
        "kind": "investigation",
        "context": _context(
            run_id="run-0005", model_id="model-5", monitoring_run_id="monitor-run-8",
            development_data_id="dev-5", document_id="report-doc-5",
            validation_report_id="report-val-5",
        ),
    },
}

EXAMPLE_ARCHIVE_FILES = ("credit/model.rds", "credit/score.R", "credit/notes.md", "README.txt")

_SURVEY_EXAMPLE = {
    "personal_data_suspected": False,
    "personal_data_evidence": [],
    "files": [
        {"path": "credit/model.rds", "category": "model_artifact"},
        {"path": "credit/score.R", "category": "scoring_code"},
        {"path": "credit/notes.md", "category": "document"},
        {"path": "README.txt", "category": "other"},
    ],
    "candidates": [
        {
            "candidate_id": "credit_glm",
            "name": "credit default GLM",
            "language": "R",
            "fitted_model": "saved_model",
            "files": ["credit/model.rds", "credit/score.R", "credit/notes.md"],
            "current_version": None,
            "current_version_reason": None,
            "superseded_versions": [],
            "rationale": "One saved R model with its scorer and notes.",
        }
    ],
    "unassigned_files": ["README.txt"],
}


def _field(value: Any, source: str, evidence: str | None = None, reason: str | None = None) -> dict[str, Any]:
    return {"value": value, "source": source, "evidence": evidence, "reason": reason}


_INTAKE_EXAMPLE = {
    "candidate_id": "credit_glm",
    "fitted_model": "saved_model",
    "executable": True,
    "execution_error": None,
    "manifest": {
        "name": _field("credit_glm", "code", "credit/score.R:3"),
        "version": _field("1.0", "document", "credit/notes.md"),
        "language": _field("R", "code", "credit/score.R"),
        "algorithm": _field("logistic regression", "artifact", "Rscript -e 'class(readRDS(\"model.rds\"))'"),
        "features": _field(["LIMIT_BAL", "PAY_0"], "artifact", "Rscript -e 'names(coef(m))'"),
        "target": _field("default next month", "document", "credit/notes.md"),
        "positive_class": _field(1, "code", "credit/score.R:20"),
        "output": _field("probability of default", "code", "credit/score.R:24"),
        "preprocessing": _field("none", "code", "credit/score.R"),
        "purpose": _field("credit default risk", "document", "credit/notes.md"),
        "intended_use": _field("application scoring", "proposed", reason="the notes describe new applicants"),
        "risk_tier": _field("high", "proposed", reason="it informs credit decisions"),
        "owner": _field(None, "unknown", reason="no file names an owner"),
        "actual_use": _field(None, "unknown", reason="only a person can say"),
        "materiality": _field("high", "proposed", reason="the notes say it scores every applicant"),
        "approval_history": _field(None, "unknown", reason="no approval record in the archive"),
    },
    "dataset_mapping": {
        "dataset_id": "uci_credit",
        "feature_map": {"LIMIT_BAL": "LIMIT_BAL", "PAY_0": "PAY_0"},
        "unmatched_features": [],
        "rationale": "Every model feature is a column of uci_credit.",
    },
    "scoring": {"entry_point": "credit/score.R", "wrapper": False, "self_check": "scored 200 development rows"},
    "problems": [],
}

EXAMPLE_METRICS_BUNDLE = {
    "policy_version": "1.0.0",
    "created_at": "2026-11-08T00:00:00Z",
    "artifact_sha256": "a" * 64,
    "data_sha256": {"validation": "b" * 64, "test": "c" * 64},
    "checks": [
        {"check_id": "performance.validation", "status": "REVIEW", "section": 6, "partition": "validation",
         "values": {"roc_auc": 0.7213, "gini": 0.4426}},
        {"check_id": "performance.test", "status": "REVIEW", "section": 6, "partition": "test",
         "values": {"roc_auc": 0.7158, "gini": 0.4316}},
    ],
}

_NARRATIVE_EXAMPLE = """# Validation narrative: credit_glm

Status: Validator agent draft, pending human decision at Gate 3

Recommendation: approve with conditions

## 1. Executive assessment

The model ranks risk moderately: AUC 0.72 on validation and 0.716 on final test (Gini 0.443 and 0.432). One medium finding is proposed.

## 2. Scope and independence

Conceptual, data and document review on development data; this agent did not see the rebuild work or any final-test rows.

## 3. Conceptual review

A logistic model suits a binary default target. [source: notes/conceptual.md]

## 4. Data review

No leakage probe found a feature that predicts the target almost perfectly. [source: probes/leakage.csv]

## 9. Findings and remediation

- Missing calibration monitoring, medium, check `performance.test`.

## 10. Conclusion and human decisions

The reviewer decides on the finding, its owner and due date, and on approval.
"""

_TRIAGE_EXAMPLE = {
    "outcome": "benign",
    "data_quality_issues": [],
    "drivers": [{"feature": "LIMIT_BAL", "bins": ["(50000, 80000]"], "note": "seasonal mix"}],
    "decision_impact": "The score distribution barely moves around the decision threshold.",
    "reason": "Amber PSI comes from one feature's seasonal mix, not from data quality or the score.",
}

_INVESTIGATION_EXAMPLE = """# Drift investigation: credit_glm, monitoring run monitor-run-8

## Drift attribution

PAY_0 drives the red band, in its two highest bins.

## Population change or data quality

Missing rates and categories are unchanged, so this is population change.

## Impact

The score distribution shifts upward. PSI alone does not show a loss of performance while labels are not yet available.

## Proposed actions

Revalidation with this batch, proposed for the validator.
"""

EXAMPLE_EXPORTS: dict[AiNativeKind, dict[str, bytes]] = {
    "survey": {SURVEY_FILE: json.dumps(_SURVEY_EXAMPLE).encode()},
    "intake": {INTAKE_FILE: json.dumps(_INTAKE_EXAMPLE).encode()},
    "validation": {NARRATIVE_FILE: _NARRATIVE_EXAMPLE.encode()},
    "triage": {
        TRIAGE_FILE: json.dumps(_TRIAGE_EXAMPLE).encode(),
        TRIAGE_REPORT_FILE: b"# Triage: monitor-run-8\n\nAmber PSI from LIMIT_BAL's seasonal mix. Benign.\n",
    },
    "investigation": {INVESTIGATION_REPORT_FILE: _INVESTIGATION_EXAMPLE.encode()},
}

EXAMPLE_PROPOSALS: dict[AiNativeKind, tuple[str, ...]] = {
    "survey": (),
    "intake": (),
    "validation": ("open_finding",),
    "triage": ("mark_drift_benign",),
    "investigation": ("start_revalidation",),
}

EXAMPLE_CHECK_ARGS: dict[AiNativeKind, dict[str, Any]] = {
    "survey": {"archive_files": EXAMPLE_ARCHIVE_FILES},
    "intake": {
        "candidate_id": "credit_glm",
        "candidate_files": ("credit/model.rds", "credit/score.R", "credit/notes.md"),
        "dataset_ids": ("uci_credit", "ulb_fraud"),
    },
    "validation": {"metrics_bundle": EXAMPLE_METRICS_BUNDLE},
    "triage": {},
    "investigation": {},
}

EXAMPLE_RESULTS: dict[AiNativeKind, dict[str, Any]] = {
    kind: {
        "status": "completed",
        "task_id": f"task-{index}",
        "text": "Done.",
        "artifact_ids": [f"artifact-{index}-{n}" for n, _ in enumerate(EXAMPLE_EXPORTS[kind])],
        "proposal_ids": [f"proposal-{index}-{n}" for n, _ in enumerate(EXAMPLE_PROPOSALS[kind])],
        "usage": {"input_tokens": 42_000, "output_tokens": 3_100, "cost_usd": 0.04},
    }
    for index, kind in enumerate(AI_NATIVE_KINDS, start=1)
}
