"""Core per-subtask blackboard state (architecture.md §5)."""

from typing import Any, Literal

from pydantic import BaseModel, Field
from app.agents.planning import Step

SubtaskType = Literal["bug", "feature", "ci", "design"]
ApprovalStatus = Literal["pending", "approved", "rejected"]
SubtaskStatus = Literal["running", "needs_human", "failed", "done", "in_review"]


class BudgetUsed(BaseModel):
    calls: int = 0
    tokens: int = 0
    est_cost_usd: float = 0.0


class SubtaskState(BaseModel):
    # identity / isolation
    ticket_id: str
    jira_key: str | None = None
    subtask_id: str
    subtask_type: SubtaskType
    description: str
    repo: str
    depends_on: list[str] = Field(default_factory=list)

    # produced by agents (structured, validated)
    diagnosis: dict[str, Any] | None = None
    plan: list[Step] = Field(default_factory=list)
    plan_reasoning: str = ""
    current_step: int = 0
    steps_done: list[dict[str, Any]] = Field(default_factory=list)

    # human interaction
    approval_status: ApprovalStatus = "pending"
    approval_payload: dict[str, Any] | None = None
    approval_note: str = ""
    execution_previewed: bool = False

    # control fields
    retry_count: int = 0
    budget_used: BudgetUsed = Field(default_factory=BudgetUsed)
    status: SubtaskStatus = "running"
    failure_reason: str | None = None

    # Durable execution artifacts; the checkout itself is disposable.
    base_commit: str | None = None
    execution_branch: str | None = None
    # None marks a path for deletion (a "delete" step); a string is its new content.
    file_changes: dict[str, str | None] = Field(default_factory=dict)
    execution_complete: bool = False
    # Set once execution finishes: "verified" ran real passing tests, "no_tests"
    # means pytest never collected any and the plan added none — escalated per
    # R-32 rather than silently assumed safe (ai_rules.md R-46).
    verifiability: Literal["verified", "no_tests"] | None = None

    # outputs
    pr_url: str | None = None
    memory_refs: list[Any] = Field(default_factory=list)
