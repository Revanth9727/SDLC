"""Validated implementation intent, independent of retry/error evidence."""
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


AUTHORITATIVE_SOURCES = frozenset({'ticket_requirement', 'human_approval_note'})


class ExecutionConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: Literal["ticket_requirement", "human_approval_note", "critic_correction", "prior_attempt"]
    text: str = Field(min_length=1)
    scope_type: Literal["ticket", "subtask", "step", "file", "symbol"]
    scope_value: str = Field(min_length=1)
    provenance: str = Field(min_length=1)

    @property
    def constraint_id(self) -> str:
        """Stable across old checkpoints; never assigned by an LLM."""
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()



class ConstraintBehavior(BaseModel):
    subject: str
    outcome: Literal['PASS', 'FAIL']


class ConstraintRelationship(BaseModel):
    constraint_ids: list[str]
    status: Literal['duplicate', 'compatible', 'non_overlapping', 'unknown', 'conflict']
    reason: str


class ConstraintConflict(BaseModel):
    constraint_ids: list[str]
    constraints: list[ExecutionConstraint]
    normalized_behaviors: list[ConstraintBehavior]
    reason: str
    affected_targets: list[dict] = Field(default_factory=list)
    status: Literal['active', 'resolved'] = 'active'
    resolution_provenance: str | None = None


class ConstraintChange(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    constraint_id: str = Field(min_length=1)
    # None explicitly withdraws; non-empty text explicitly replaces, retaining scope.
    replacement_text: str | None = Field(default=None, min_length=1, max_length=4000)


class ConstraintResolution(BaseModel):
    changes: list[ConstraintChange]
    note: str
    provenance: str
