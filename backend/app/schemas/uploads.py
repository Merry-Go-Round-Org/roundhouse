"""Upload contracts agreed with frontend issue #10."""

from typing import Literal
from pydantic import BaseModel, Field, ConfigDict


class UploadCheck(BaseModel):
    name: str
    status: Literal["pending", "running", "passed", "failed", "not_applicable"]
    blocking: bool
    reason: str


class UploadState(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    status: Literal["queued", "running", "completed", "failed"]
    checks: list[UploadCheck]
    files: list[str]
    model_id: str | None
    error: str | None


class DatasetDescriptor(BaseModel):
    model_config = ConfigDict(extra="allow")
    dataset_id: Literal["uci_credit", "ulb_fraud"]
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=120)
    source_url: str = Field(min_length=1, max_length=2000)
    license: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    split_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_column: str = Field(min_length=1, max_length=100)
    positive_class: int | str
    row_id_rule: str = Field(min_length=1, max_length=1000)
    row_count: int = Field(gt=0, le=2_000_000)
    partition_counts: dict[str, int]
    audit_only_columns: list[str] = Field(default_factory=list)
    time_column: str | None = None
    source_filename: str = Field(min_length=1, max_length=200)
