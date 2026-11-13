"""Persisted operations, lifecycle timestamps and public results."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from datetime import datetime, timezone


Operation = Literal["document", "rebuild", "validate", "monitor"]
RunStatus = Literal["queued", "running", "awaiting_review", "completed", "failed"]


class RunCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )
    operation: Operation
    model_version: str = Field(min_length=1)
    dataset_id: str | None = None


class RunRead(BaseModel):
    ## Fields the backend returns
    model_config = ConfigDict(from_attributes=True)
    id: str
    model_id: str
    model_version: str
    dataset_id: str | None = None
    operation: Operation
    status: RunStatus

    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    stage: str | None = None
    error: str | None = None

    @field_validator("dataset_id", mode="before")
    @classmethod
    def optional_dataset(cls, value):
        return value or None

    @field_serializer("created_at", "started_at", "finished_at")
    def utc_time(self, value):
        return (
            value.replace(tzinfo=timezone.utc).isoformat()
            if value and value.tzinfo is None
            else value.isoformat()
            if value
            else None
        )
