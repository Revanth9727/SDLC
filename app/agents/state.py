"""Core per-subtask blackboard state (architecture.md §5)."""

from typing import Any, Literal

from pydantic import BaseModel, Field
from app.agents.planning import Step

SubtaskType = Literal["bug", "feature", "ci", "design"]
ApprovalStatus = Literal["pending", "approved", "rejected"]
SubtaskStatus = Literal["running", "integration_pending", "needs_human", "failed", "done", "in_review"]


class BudgetUsed(BaseModel):
    calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    est_cost_usd: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    elapsed_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)


class SubtaskState(BaseModel):
    # identity / isolation
    ticket_id: str
    jira_key: str | None = None
    subtask_id: str
    subtask_type: SubtaskType
    description: str
    repo: str
    depends_on: list[str] = Field(default_factory=list)
    # Phase 8 orchestration metadata. A coordinator owns only the ticket-level
    # decomposition/intent gate; every work state owns exactly one specification.
    orchestration_role: Literal["legacy", "coordinator", "work"] = "legacy"
    spec_id: str | None = None
    orchestration_index: int | None = Field(default=None, ge=0)
    parent_subtask_id: str | None = None
    # The ticket's full confirmed repo list (§5c) — the Planner's input; it assigns
    # each sub-task exactly one of these (R-26), never a repo outside this list.
    confirmed_repos: list[str] = Field(default_factory=list)
    # Ticket-level, deterministic repository shape supplied to the Planner.
    # Deep code evidence remains isolated in each work state's code_context.
    repo_overview: list[dict[str, Any]] = Field(default_factory=list)

    # Planner (architecture.md §7a, ai_rules.md R-26/R-30/R-10): the full
    # decomposition it recorded, even though only the first sub-task (this state)
    # runs for now. `description`/`repo`/`subtask_type`/`depends_on` above are
    # overwritten with that first sub-task's assignment once the Planner runs.
    subtask_specs: list[dict[str, Any]] = Field(default_factory=list)
    decomposition_reasoning: str = ""

    # produced by agents (structured, validated)
    # Sole Code-Intelligence -> Diagnosis handoff. No agent-side repository channel.
    code_context: dict[str, Any] = Field(default_factory=dict)
    repo_snapshot_id: str | None = None
    diagnosis: dict[str, Any] | None = None
    plan: list[Step] = Field(default_factory=list)
    plan_reasoning: str = ""
    current_step: int = 0
    steps_done: list[dict[str, Any]] = Field(default_factory=list)

    # human interaction
    approval_status: ApprovalStatus = "pending"
    approval_payload: dict[str, Any] | None = None
    approval_note: str = ""
    last_rejection_note: str = ""
    replan_count: int = 0
    execution_previewed: bool = False

    # control fields
    guard_node: str = "planner"
    guard_error: str | None = None
    guard_retry: bool = False
    escalation_id: str | None = None
    resolution: str | None = None
    retry_count: int = 0
    budget_used: BudgetUsed = Field(default_factory=BudgetUsed)
    status: SubtaskStatus = "running"
    failure_reason: str | None = None

    # Durable execution artifacts; the checkout itself is disposable.
    base_commit: str | None = None
    diagnosed_file_hashes: dict[str, str | None] = Field(default_factory=dict)
    freshness_recorded: bool = False
    execution_branch: str | None = None
    # None marks a path for deletion (a "delete" step); a string is its new content.
    file_changes: dict[str, str | None] = Field(default_factory=dict)
    # Deterministic repository-graph checks made around surgical edits. This is
    # the shared Code-Intelligence tool handoff to Critic and integration.
    code_impacts: list[dict[str, Any]] = Field(default_factory=list)
    execution_complete: bool = False
    # Set once execution finishes: "verified" ran real passing tests, "no_tests"
    # means pytest never collected any and the plan added none — escalated per
    # R-32 rather than silently assumed safe (ai_rules.md R-46).
    verifiability: Literal["verified", "no_tests"] | None = None

    # Critic (architecture.md §7d, ai_rules.md R-31/R-32): the last verdict on the
    # completed change, and the counter/feedback for the Critic<->Executor loop.
    # A separate counter from retry_count (per-node exception retries) and
    # replan_count (pre-execution human-gate loop) — this one is specifically the
    # post-execution review loop, capped by the same MAX_AGENT_RETRIES.
    critic_verdict: dict[str, Any] | None = None
    critic_feedback: list[str] = Field(default_factory=list)
    critic_retry_count: int = 0

    # Solution reuse (memory.md §8a, ai_rules.md R-29): set only on a strong
    # (>= threshold) fresh match, which skips Diagnosis + Step-Planner — the
    # synthesized `diagnosis`/`plan`/`plan_reasoning` still go through the SAME
    # human gate, Executor, and Critic as a freshly-diagnosed fix (never
    # blind-applied).
    reuse_source: str | None = None
    reuse_similarity: float | None = None

    # outputs
    pr_url: str | None = None
    memory_refs: list[Any] = Field(default_factory=list)
    # Compact, advisory summaries recalled for this sub-task. Never raw context.
    prior_resolutions: list[dict[str, Any]] = Field(default_factory=list)
