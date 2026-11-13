"""Report result shapes; no example values should be treated as evidence."""

from typing import Literal
from datetime import datetime
from pydantic import BaseModel, Field


class MetricResult(BaseModel):
    name: str
    value: float | None = None
    status: Literal[
        "green",
        "amber",
        "red",
        "not_assessable",
        "not_run",
        "not_applicable",
        "NOT_APPLICABLE",
    ]
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)


class ReportRead(BaseModel):
    id: str
    model_id: str
    run_id: str
    kind: Literal["documentation", "parity", "validation"]
    artifact_id: str
    metrics: list[MetricResult] = Field(default_factory=list)
    evidence: dict = Field(default_factory=dict)
    model_version: str | None = None
    artifact_version: str | None = None
    artifact_sha256: str | None = None
    created_at: datetime | None = None
    review_status: str | None = None
    review_id: str | None = None
    review_resolution: dict | None = None
    source_files: list[dict] = Field(default_factory=list)


class FindingUpdate(BaseModel):
    status: Literal["open", "in_progress", "resolved"] | None = None
    owner_id: str | None = None
    due_date: datetime | None = None
    comment: str = Field(min_length=1, max_length=4000)
