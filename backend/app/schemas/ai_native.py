"""Browser-facing AI Native contracts. Action argument schemas come from /proposals/actions."""

from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field
from harness.outputs import ManifestField, DatasetMapping


class ScoringPlan(BaseModel):
    entry_point: str | None = None
    wrapper: bool = False
    wrapper_sha256: str | None = None
    self_check: str | None = None


class CandidatePlan(BaseModel):
    candidate_id: str
    fitted_model: Literal["saved_model", "parameters_in_code", "training_code_only"]
    executable: bool
    execution_error: str | None = None
    manifest: dict[str, ManifestField]
    dataset_mapping: DatasetMapping | None = None
    scoring: ScoringPlan
    files: list[str]
    problems: list[str] = Field(default_factory=list)


class IntakePlan(BaseModel):
    candidates: list[CandidatePlan]
    unassigned_files: list[str]
    archive_sha256: str | None = None


class ArchiveFile(BaseModel):
    path: str
    sha256: str


class GeneratedModel(BaseModel):
    model_id: str
    name: str
    model_version: str
    document_run_id: str | None
    reproduction: dict | None


class ArchiveRead(BaseModel):
    id: str
    actor_id: str
    sha256: str
    status: Literal[
        "queued",
        "running",
        "awaiting_review",
        "confirmed",
        "building",
        "completed",
        "failed",
        "cancelled",
        "blocked",
    ]
    files: list[ArchiveFile]
    plan: IntakePlan | None
    plan_sha256: str | None
    decision: dict | None
    error: str | None
    created_at: datetime
    models: list[GeneratedModel]


class ProposalRead(BaseModel):
    id: str
    source_run_id: str | None
    source_task_id: str | None
    conversation_id: str | None
    model_id: str
    type: str
    args: dict[str, Any]
    rationale: str
    evidence: list[dict]
    required_role: str
    preconditions: dict
    status: Literal[
        "pending", "approved", "rejected", "superseded", "expired", "executed"
    ]
    decision: dict | None
    result: dict | None
    created_at: datetime
