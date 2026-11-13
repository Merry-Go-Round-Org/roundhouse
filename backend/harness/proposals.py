"""Proposals: actions an agent suggests and an authorised person approves.

The backend owns the proposals table, the API, approval and execution. The harness owns what an
agent may propose. This module holds the agreed action catalogue, the `create_proposal` tool spec
that the backend's gateway offers, and a check the loop runs before a call reaches the backend, so
an agent cannot propose an action its profile does not allow or send malformed arguments.

The target of an action, such as the model version or the monitoring run, is never an argument.
The backend takes it from the run's trusted context, so an agent cannot point an action at
another model.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from harness.schemas import ToolSpec

PROPOSAL_TOOL_NAME = "create_proposal"
MAX_RATIONALE_CHARS = 2_000
MAX_EVIDENCE_ITEMS = 20

Severity = Literal["low", "medium", "high"]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarkDriftBenign(_Args):
    """Records the monitoring result as reviewed and benign."""

    reason: str = Field(min_length=1, max_length=1_000)


class OpenFinding(_Args):
    """Creates a finding. Owner and due date are set by the person who accepts it."""

    title: str = Field(min_length=1, max_length=200)
    severity: Severity
    severity_rationale: str = Field(min_length=1, max_length=1_000)
    impact: str = Field(min_length=1, max_length=1_000)
    remediation: str = Field(min_length=1, max_length=1_000)
    check_ids: list[str] = Field(default_factory=list, max_length=20)
    report_section: str | None = Field(default=None, max_length=100)


class StartRevalidation(_Args):
    """A validation run that includes the monitoring batch."""

    reason: str = Field(min_length=1, max_length=1_000)


class StartRecalibration(_Args):
    """A rebuild run whose goal is recalibration on the development partition (data_scope is always "development")."""

    reason: str = Field(min_length=1, max_length=1_000)
    data_scope: Literal["development"]


class RestrictUse(_Args):
    """Adds a use restriction to the inventory record, pending the approver."""

    restriction: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=1_000)


class ReportDataIncident(_Args):
    """Records a data quality incident against the batch and leaves the model unchanged."""

    description: str = Field(min_length=1, max_length=1_000)
    affected_features: list[str] = Field(default_factory=list, max_length=50)


ACTIONS: dict[str, type[_Args]] = {
    "mark_drift_benign": MarkDriftBenign,
    "open_finding": OpenFinding,
    "start_revalidation": StartRevalidation,
    "start_recalibration": StartRecalibration,
    "restrict_use": RestrictUse,
    "report_data_incident": ReportDataIncident,
}

# The role that may approve each action, as proposed. The backend has the final say.
REQUIRED_ROLES: dict[str, str] = {
    "mark_drift_benign": "validator",
    "open_finding": "validator",
    "start_revalidation": "validator",
    "start_recalibration": "developer",
    "restrict_use": "approver",
    "report_data_incident": "developer",
}

MONITORING_ACTIONS: tuple[str, ...] = (
    "mark_drift_benign",
    "open_finding",
    "start_revalidation",
    "start_recalibration",
    "restrict_use",
    "report_data_incident",
)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["file", "check", "report_section", "monitoring_run", "artifact"]
    ref: str = Field(min_length=1, max_length=500)


class ProposalDraft(BaseModel):
    """What an agent sends. The backend adds the ID, source run, role, preconditions and status."""

    model_config = ConfigDict(extra="forbid")

    type: str
    args: dict[str, Any]
    rationale: str = Field(min_length=1, max_length=MAX_RATIONALE_CHARS)
    evidence: list[Evidence] = Field(min_length=1, max_length=MAX_EVIDENCE_ITEMS)


def proposal_tool_spec(types: tuple[str, ...] = tuple(ACTIONS)) -> ToolSpec:
    """The spec the backend's gateway offers. Each action's arguments are listed in the
    description, because a JSON schema with one branch per action is hard for small models.
    """
    lines = []
    for name in types:
        fields = ACTIONS[name].model_json_schema()
        required = set(fields.get("required", []))
        params = ", ".join(
            f"{field}{'' if field in required else ' (optional)'}" for field in fields["properties"]
        )
        lines.append(f"- {name}: {ACTIONS[name].__doc__.strip()} args: {params}.")
    return ToolSpec(
        name=PROPOSAL_TOOL_NAME,
        description=(
            "Propose an action for a person to approve. Nothing runs until they approve it. "
            "The target model and monitoring run come from this task, so never pass IDs for "
            "them. Actions:\n" + "\n".join(lines)
        ),
        parameters={
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": list(types)},
                "args": {"type": "object"},
                "rationale": {"type": "string", "maxLength": MAX_RATIONALE_CHARS},
                "evidence": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_EVIDENCE_ITEMS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": [
                                    "file",
                                    "check",
                                    "report_section",
                                    "monitoring_run",
                                    "artifact",
                                ],
                            },
                            "ref": {"type": "string"},
                        },
                        "required": ["kind", "ref"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["type", "args", "rationale", "evidence"],
            "additionalProperties": False,
        },
        risk="write",
    )


def proposal_problems(allowed_types: tuple[str, ...], args: dict[str, Any]) -> list[str]:
    """What stops a `create_proposal` call from reaching the backend. Empty means it may go.

    The messages go back to the agent, so they name the problem without quoting its input.
    """
    try:
        draft = ProposalDraft.model_validate(args)
    except ValidationError as exc:
        return [_describe(error) for error in exc.errors()]
    if draft.type not in ACTIONS:
        return [f"unknown action type; use one of: {', '.join(allowed_types) or 'none'}"]
    if draft.type not in allowed_types:
        allowed = ", ".join(allowed_types) or "none"
        return [f"this task may not propose {draft.type}; allowed: {allowed}"]
    try:
        ACTIONS[draft.type].model_validate(draft.args)
    except ValidationError as exc:
        return [f"args: {_describe(error)}" for error in exc.errors()]
    return []


def _describe(error: Any) -> str:
    location = ".".join(str(part) for part in error.get("loc", ())) or "input"
    return f"{location}: {error.get('msg', 'invalid')}"
