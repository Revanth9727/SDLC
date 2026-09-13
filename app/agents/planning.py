"""Validated planning handoffs shared through the blackboard."""

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    step_id: str = Field(min_length=1)
    intent: str = Field(min_length=1)
    target_file: str = Field(min_length=1)
    # Grounds the Executor: "edit"/"delete" require the file to already exist in
    # the repo, "create" requires it NOT to. Never inferred from file presence
    # alone — the plan states its intent explicitly (ai_rules.md R-46).
    action: Literal["edit", "create", "delete"] = "edit"

    @field_validator("target_file")
    @classmethod
    def relative_file(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value or value in {".", ""}:
            raise ValueError("target_file must be a repository-relative file path")
        return value


def is_test_path(path: str) -> bool:
    """Shared, generic pytest-discovery heuristic (R-32/R-46) — not tied to any
    one repo's layout. Used by the Step-Planner to require test coverage and by
    the Executor to classify pytest's exit code."""
    parts = PurePosixPath(path)
    name = parts.name
    return (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py") or "tests" in parts.parts[:-1]


def is_python_source(path: str) -> bool:
    return path.endswith(".py") and not is_test_path(path)


class Plan(RootModel[list[Step]]):
    @model_validator(mode="after")
    def valid_steps(self):
        if not self.root or len(self.root) > 30:
            raise ValueError("a plan must contain 1–30 steps")
        if len({step.step_id for step in self.root}) != len(self.root):
            raise ValueError("step_id must be unique within a plan")
        return self


class CannotPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1)


class PlanningResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    plan: Plan | None = None
    reasoning: str = Field(min_length=1)
    cannot_plan: CannotPlan | None = Field(None, alias="CannotPlan")

    @model_validator(mode="after")
    def exclusive_outcome(self):
        if (self.plan is None) == (self.cannot_plan is None):
            raise ValueError("provide either plan or CannotPlan")
        return self


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    approval_status: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=4000)
