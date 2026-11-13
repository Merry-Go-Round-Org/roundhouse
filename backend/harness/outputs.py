"""The files the AI-native agents export, and checks the backend runs before it uses them.

Each agent writes one machine-readable file and exports it with `export_artifact`. The backend
reads it only after the matching `*_problems` function returns an empty list. Like
`document_problems`, these check structure and the platform's rules, not truth: that is the
human review at Gate 0, Gate 3 or the proposal card.
"""
from __future__ import annotations

import json
import posixpath
import re
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SURVEY_FILE = "survey.json"
INTAKE_FILE = "intake.json"
TRIAGE_FILE = "triage.json"
TRIAGE_REPORT_FILE = "triage_report.md"
INVESTIGATION_REPORT_FILE = "investigation_report.md"
SCORING_WRAPPER_DIR = "scoring_wrapper"
CHALLENGER_DIR = "challenger"
SCORING_ENTRY_POINT = "score.py"

FileCategory = Literal[
    "model_artifact", "scoring_code", "training_code", "document", "data", "other"
]
FittedModel = Literal["saved_model", "parameters_in_code", "training_code_only"]
FieldSource = Literal["file", "artifact", "code", "document", "proposed", "unknown"]

# Every inventory field intake fills. Each one has a value and a source.
MANIFEST_FIELDS: tuple[str, ...] = (
    "name",
    "version",
    "language",
    "algorithm",
    "features",
    "target",
    "positive_class",
    "output",
    "preprocessing",
    "purpose",
    "intended_use",
    "risk_tier",
    "owner",
    "actual_use",
    "materiality",
    "approval_history",
)
# Facts about the business that the model files can never prove.
PERSON_ONLY_FIELDS: tuple[str, ...] = ("owner", "actual_use", "materiality", "approval_history")
CONFIRMATION_SOURCES: tuple[str, ...] = ("code", "document", "proposed")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _relative(path: str) -> str:
    if not path or path.startswith(("/", "~")) or "\\" in path or "\x00" in path:
        raise ValueError(f"not a relative archive path: {path!r}")
    if ".." in path.split("/"):
        raise ValueError(f"archive paths must not contain '..': {path!r}")
    return posixpath.normpath(path)


# ---------------------------------------------------------------- survey
class SurveyFile(_Strict):
    path: str
    category: FileCategory

    @model_validator(mode="after")
    def _path(self) -> SurveyFile:
        self.path = _relative(self.path)
        return self


class SupersededVersion(_Strict):
    path: str
    reason: str = Field(min_length=1)


class Candidate(_Strict):
    candidate_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str = Field(min_length=1)
    language: str = Field(min_length=1)
    fitted_model: FittedModel
    files: list[str] = Field(min_length=1)
    current_version: str | None = None
    current_version_reason: str | None = None
    superseded_versions: list[SupersededVersion] = Field(default_factory=list)
    rationale: str = Field(min_length=1)


class Survey(_Strict):
    personal_data_suspected: bool
    personal_data_evidence: list[str] = Field(default_factory=list)
    files: list[SurveyFile]
    candidates: list[Candidate]
    unassigned_files: list[str] = Field(default_factory=list)


def survey_problems(text: str, archive_files: Iterable[str] | None = None) -> list[str]:
    """Problems with `survey.json`. `archive_files` is the backend's list from safe unpacking."""
    survey, problems = _load(Survey, text)
    if survey is None:
        return problems
    listed = {f.path for f in survey.files}
    known = {_relative(p) for p in archive_files} if archive_files is not None else None
    if known is not None:
        unknown = sorted(listed - known)
        if unknown:
            problems.append("files not in the archive: " + ", ".join(unknown[:10]))
        missing = sorted(known - listed)
        if missing:
            problems.append("archive files not classified: " + ", ".join(missing[:10]))
    if survey.personal_data_suspected:
        if not survey.personal_data_evidence:
            problems.append("personal data is suspected but no evidence is given")
        if survey.candidates:
            problems.append("no candidate may go further when personal data is suspected")
        return problems
    ids = [c.candidate_id for c in survey.candidates]
    if len(set(ids)) != len(ids):
        problems.append("candidate IDs are repeated")
    grouped: set[str] = set()
    for candidate in survey.candidates:
        files = {_safe(p, problems) for p in candidate.files}
        grouped |= files
        if not files <= listed:
            problems.append(f"{candidate.candidate_id}: lists files that are not classified")
        versions = [candidate.current_version, *(v.path for v in candidate.superseded_versions)]
        for path in filter(None, versions):
            if _safe(path, problems) not in files:
                problems.append(f"{candidate.candidate_id}: version {path} is not in its files")
        if candidate.superseded_versions and not (
            candidate.current_version and candidate.current_version_reason
        ):
            problems.append(
                f"{candidate.candidate_id}: has several versions but no current version and reason"
            )
    unassigned = {_safe(p, problems) for p in survey.unassigned_files}
    if unassigned & grouped:
        problems.append("a file is both in a candidate and unassigned")
    left_over = sorted(listed - grouped - unassigned)
    if left_over:
        problems.append("files in no candidate and not unassigned: " + ", ".join(left_over[:10]))
    return problems


# ---------------------------------------------------------------- intake
class ManifestField(_Strict):
    value: Any = None
    source: FieldSource
    evidence: str | None = None  # a file path for file, document and code; a command for artifact
    reason: str | None = None  # why, for proposed and unknown


class DatasetMapping(_Strict):
    dataset_id: str = Field(min_length=1)
    feature_map: dict[str, str]
    unmatched_features: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)


class Scoring(_Strict):
    entry_point: str | None = None  # an existing scorer in the archive
    wrapper: bool = False  # True when the agent wrote scoring_wrapper/score.py
    self_check: str | None = None  # what the agent's own run showed. Not official


class Intake(_Strict):
    candidate_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    fitted_model: FittedModel
    executable: bool
    execution_error: str | None = None
    manifest: dict[str, ManifestField]
    dataset_mapping: DatasetMapping | None = None
    scoring: Scoring
    problems: list[str] = Field(default_factory=list)


def intake_problems(
    text: str,
    *,
    candidate_id: str | None = None,
    candidate_files: Iterable[str] | None = None,
    dataset_ids: Iterable[str] | None = None,
) -> list[str]:
    """Problems with `intake.json`, checked against what the backend knows about the candidate."""
    intake, problems = _load(Intake, text)
    if intake is None:
        return problems
    if candidate_id is not None and intake.candidate_id != candidate_id:
        problems.append("candidate_id does not match the task's candidate")
    missing = [f for f in MANIFEST_FIELDS if f not in intake.manifest]
    if missing:
        problems.append("manifest fields missing: " + ", ".join(missing))
    extra = [f for f in intake.manifest if f not in MANIFEST_FIELDS]
    if extra:
        problems.append("unknown manifest fields: " + ", ".join(extra))
    files = {_relative(p) for p in candidate_files} if candidate_files is not None else None
    for name, field in intake.manifest.items():
        if field.source == "unknown":
            if field.value not in (None, "", []):
                problems.append(f"{name}: an unknown field must have no value")
            if not field.reason:
                problems.append(f"{name}: an unknown field needs a reason")
            continue
        if field.source in ("proposed",) and not field.reason:
            problems.append(f"{name}: a proposed value needs a rationale in reason")
        if field.source in ("file", "code", "document", "artifact") and not field.evidence:
            problems.append(f"{name}: source {field.source} needs evidence")
        if field.source in ("file", "code", "document") and field.evidence and files is not None:
            if _evidence_path(field.evidence) not in files:
                problems.append(f"{name}: evidence is not one of the candidate's files")
        if field.source == "artifact" and not intake.executable:
            problems.append(f"{name}: source artifact needs a model that loads")
        if name in PERSON_ONLY_FIELDS and field.source != "proposed":
            # A claim in an old file goes in the reason of a proposed value.
            problems.append(f"{name}: only a person can confirm this, so use proposed or unknown")
    if intake.fitted_model == "training_code_only":
        if intake.scoring.wrapper or intake.scoring.entry_point:
            problems.append("training code only has no fitted model to score")
    elif intake.executable and not (intake.scoring.wrapper or intake.scoring.entry_point):
        problems.append("an executable model needs a scoring entry point or a wrapper")
    if not intake.executable and not intake.execution_error:
        problems.append("a model that does not load needs the error in execution_error")
    if not intake.executable and intake.scoring.wrapper:
        problems.append("a wrapper cannot be checked for a model that does not load")
    if intake.scoring.entry_point and files is not None:
        if _safe(intake.scoring.entry_point, problems) not in files:
            problems.append("the scoring entry point is not one of the candidate's files")
    if intake.dataset_mapping and dataset_ids is not None:
        if intake.dataset_mapping.dataset_id not in set(dataset_ids):
            problems.append("the dataset mapping names a dataset that is not on the platform")
    return problems


def fields_needing_confirmation(intake: Intake) -> list[str]:
    """The manifest fields a person must confirm at Gate 0."""
    return [n for n, f in intake.manifest.items() if f.source in CONFIRMATION_SOURCES]


# ---------------------------------------------------------------- triage
class Driver(_Strict):
    feature: str = Field(min_length=1)
    bins: list[str] = Field(default_factory=list)
    note: str | None = None


class Triage(_Strict):
    outcome: Literal["benign", "problem"]
    data_quality_issues: list[str] = Field(default_factory=list)
    drivers: list[Driver] = Field(default_factory=list)
    decision_impact: str = Field(min_length=1)
    reason: str = Field(min_length=1)


def triage_problems(text: str) -> list[str]:
    triage, problems = _load(Triage, text)
    if triage is None:
        return problems
    if triage.outcome == "problem" and not (triage.drivers or triage.data_quality_issues):
        problems.append("a problem needs drivers or data quality issues")
    return problems


# ---------------------------------------------------------------- investigation
INVESTIGATION_SECTIONS: tuple[str, ...] = (
    "Drift attribution",
    "Population change or data quality",
    "Impact",
    "Proposed actions",
)


def investigation_report_problems(markdown: str) -> list[str]:
    return _sections_problems(markdown, INVESTIGATION_SECTIONS, numbered=False)


# ---------------------------------------------------------------- shared
def _load(model: type[BaseModel], text: str) -> tuple[Any, list[str]]:
    try:
        data = json.loads(text)
    except ValueError:
        return None, ["the file is not valid JSON"]
    try:
        return model.model_validate(data), []
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            return None, [
                f"{'.'.join(str(p) for p in e['loc']) or 'file'}: {e['msg']}" for e in exc.errors()
            ][:20]
        return None, [str(exc)]


def _safe(path: str, problems: list[str]) -> str:
    try:
        return _relative(path)
    except ValueError as exc:
        problems.append(str(exc))
        return path


def _evidence_path(evidence: str) -> str:
    """Evidence may add a location, as in `model.R:12` or `notes.docx (page 2)`."""
    head = re.split(r"[\s(]", evidence.strip(), maxsplit=1)[0]
    head = re.sub(r":\d+(-\d+)?$", "", head)
    try:
        return _relative(head)
    except ValueError:
        return head


_HEADING = re.compile(r"^## (?:(\d+)\. )?(.+?)\s*$", re.MULTILINE)
_HTML_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9-]*(\s[^<>]*)?/?>")
GUIDE_PREFIX = "> Guide:"


def _sections_problems(
    markdown: str, sections: tuple[str, ...], *, numbered: bool, numbers: tuple[int, ...] = ()
) -> list[str]:
    if not markdown.strip():
        return ["the document is empty"]
    problems: list[str] = []
    if not markdown.lstrip().startswith("# "):
        problems.append("the document does not start with a level-1 title")
    found = [(int(n) if n else None, t) for n, t in _HEADING.findall(markdown)]
    expected = [(n if numbered else None, t) for n, t in zip(numbers or [None] * len(sections), sections)]
    if found != expected:
        missing = [t for n, t in expected if (n, t) not in found]
        extra = [t for n, t in found if (n, t) not in expected]
        if missing:
            problems.append("missing sections: " + "; ".join(missing))
        if extra:
            problems.append("unexpected sections: " + "; ".join(extra))
        if not missing and not extra:
            problems.append("sections are out of order or repeated")
    if any(line.lstrip().startswith(GUIDE_PREFIX) for line in markdown.splitlines()):
        problems.append("template guide lines are still present")
    stripped = re.sub(r"^```.*?^```", "", markdown, flags=re.MULTILINE | re.DOTALL)
    if _HTML_TAG.search(re.sub(r"`[^`\n]*`", "", stripped)):
        problems.append("the document contains HTML")
    return problems
