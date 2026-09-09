"""Core per-subtask blackboard state (architecture.md §5)."""

from typing import Any, Literal

from pydantic import BaseModel, Field

SubtaskType = Literal["bug", "feature", "ci", "design"]
ApprovalStatus = Literal["pending", "approved", "rejected"]
SubtaskStatus = Literal["running", "needs_human", "failed", "done"]


class BudgetUsed(BaseModel):
    calls: int = 0
    tokens: int = 0
    est_cost_usd: float = 0.0


class SubtaskState(BaseModel):
    # identity / isolation
    ticket_id: str
    subtask_id: str
    subtask_type: SubtaskType
    description: str
    repo: str
    depends_on: list[str] = Field(default_factory=list)

    # produced by agents (structured, validated)
    diagnosis: dict[str, Any] | None = None
    plan: list[Any] = Field(default_factory=list)
    current_step: int = 0
    steps_done: list[dict[str, Any]] = Field(default_factory=list)

    # human interaction
    approval_status: ApprovalStatus = "pending"
    approval_payload: dict[str, Any] | None = None

    # control fields
    retry_count: int = 0
    budget_used: BudgetUsed = Field(default_factory=BudgetUsed)
    status: SubtaskStatus = "running"
    failure_reason: str | None = None

    # outputs
    pr_url: str | None = None
    memory_refs: list[Any] = Field(default_factory=list)
