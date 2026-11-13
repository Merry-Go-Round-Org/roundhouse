"""Shared frontend contract for metadata registration (not file upload)."""

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

DatasetId = Literal["uci_credit", "ulb_fraud"]


def canonical_language(value):
    if isinstance(value, str):
        return {"python": "Python", "r": "R"}.get(value.strip().casefold(), value)
    return value


class ModelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=40)
    language: Literal["R", "Python"]
    dataset_id: DatasetId
    purpose: str = Field(min_length=1, max_length=1000)
    owner: str | None = Field(default=None, max_length=120)


class ModelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    version: str
    language: str
    dataset_id: str | None
    purpose: str
    owner: str | None
    lifecycle_status: str
    created_at: datetime
    inventory: dict = Field(default_factory=dict)

    @field_validator("language", mode="before")
    @classmethod
    def normalize_language(cls, value):
        # Older intake records may contain the agent's lowercase label.
        return canonical_language(value)


class DatasetRead(BaseModel):
    id: DatasetId
    name: str
    filename: str
    target_description: str
    limitations: list[str]
