"""Review requests must eventually bind an authenticated person to a version."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str
    artifact_version: str
    decision: Literal["accepted", "changes_requested", "accepted_with_conditions"]
    rationale: str = Field(min_length=1)
    conditions: list[str] = Field(default_factory=list)
    # Reviewer identity must come from the server-side session, not this body.
