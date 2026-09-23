"""Core per-subtask blackboard state (architecture.md §5)."""

from typing import Any, Literal

from pydantic import BaseModel, Field
from app.agents.planning import Step
from app.agents.constraints import (ExecutionConstraint, ConstraintConflict, ConstraintRelationship, ConstraintResolution)
from app.tools.edit_applier import EditBlock, MatchFailureEvidence

SubtaskType = Literal["bug", "feature", "ci", "design"]
ApprovalStatus = Literal["pending", "approved", "rejected"]
SubtaskStatus = Literal["running", "integration_pending", "needs_human", "failed", "done", "in_review"]


class BudgetUsed(BaseModel):
    calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    est_cost_usd: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    elapsed_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)


class FailedEditAttempt(BaseModel):
    """Compatibility view of the latest failed edit; see retry_attempts for history."""
    attempt: int = Field(ge=1)
    step_id: str
    operation: Literal["apply_edit"] = "apply_edit"
    target_file: str
    candidate_content: list[EditBlock]
    failure_type: Literal["NoMatch", "Ambiguous"]
    failure_message: str
    evidence: MatchFailureEvidence
    corrective_instruction: str


class RetryAttempt(BaseModel):
    """Bounded reasoning evidence, distinct from operational FailureContext."""

    attempt_number: int = Field(ge=1)
    operation: str
    target: str
    candidate_type: Literal["source_edit", "generated_test", "invalid_output"]
    candidate_fingerprint: str | None = None
    generated_test_fingerprint: str | None = None
    candidate_content: str = Field(max_length=8000)
    candidate_content_truncated: bool = False
    failure_type: str
    failure_reason: str
    failure_evidence: dict[str, Any] = Field(default_factory=dict)
    corrective_instruction: str


class FailureContext(BaseModel):
    """Sanitized, durable evidence for one failed operation (R-8b/R-17)."""

    classification: Literal["reasoning", "infrastructure"]
    component: str
    operation: str
    function: str
    file: str
    line: int = Field(ge=0)
    exception_type: str
    message: str
    reason: str
    stack: list[str] = Field(default_factory=list, max_length=12)
    identifiers: dict[str, str] = Field(default_factory=dict)


class UnresolvedCheck(BaseModel):
    """A deterministic verification that could not prove or disprove a claim."""

    owner: str
    attribute: str
    reason: str
    source: str
    impact: str


class SubtaskState(BaseModel):
    # identity / isolation
    ticket_id: str
    jira_key: str | None = None
    subtask_id: str
    subtask_type: SubtaskType
    description: str
    ticket_requirement: str = ""
    execution_constraints: list[ExecutionConstraint] = Field(default_factory=list)
    pending_execution_constraints: list[ExecutionConstraint] = Field(default_factory=list)
    withdrawn_constraint_ids: list[str] = Field(default_factory=list)
    constraint_conflicts: list[ConstraintConflict] = Field(default_factory=list)
    constraint_relationships: list[ConstraintRelationship] = Field(default_factory=list)
    constraint_resolutions: list[ConstraintResolution] = Field(default_factory=list)
    constraint_resolution_replan: bool = False
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
    # Same-ticket history only. It is evidence to re-check, never current
    # progress: diagnosis/plan/current_step/file_changes remain fresh for this run.
    prior_attempt: dict[str, Any] | None = None

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
    # Durable, compact context from failed attempts. Retry nodes consume this so
    # checkpoint resumes learn from prior failures instead of repeating them.
    attempt_history: list[str] = Field(default_factory=list)
    failure_contexts: list[FailureContext] = Field(default_factory=list)
    failed_edit_attempt: FailedEditAttempt | None = None
    retry_attempts: dict[str, list[RetryAttempt]] = Field(default_factory=dict)
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
    # The exact repository-level artifact that passed whole-ticket integration.
    # Kept separately from this sub-task's isolated changes so redo publication
    # can reproduce the tested tree without rewriting sub-task history (R-31b).
    integrated_file_changes: dict[str, str | None] = Field(default_factory=dict)
    integrated_base_commit: str | None = None
    integrated_subtask_ids: list[str] = Field(default_factory=list)
    integration_verified: bool = False
    # Whole-ticket R-31 evidence. This is advisory to future planning and the
    # human decision surface; it never authorizes publication by itself.
    integration_evidence: list[dict[str, Any]] = Field(default_factory=list)
    integration_issue: dict[str, Any] | None = None
    integration_review_evidence: dict[str, Any] | None = None
    # Deterministic repository-graph checks made around surgical edits. This is
    # the shared Code-Intelligence tool handoff to Critic and integration.
    code_impacts: list[dict[str, Any]] = Field(default_factory=list)
    execution_complete: bool = False
    # Set once execution finishes: "verified" ran real passing tests, "no_tests"
    # means pytest never collected any and the plan added none — escalated per
    # R-32 rather than silently assumed safe (ai_rules.md R-46).
    verifiability: Literal["verified", "no_tests", "unverifiable"] | None = None
    # Non-secret runtime facts only. Credential values are request-scoped and
    # must never enter this checkpointed blackboard.
    verification_summary: dict[str, Any] | None = None
    required_test_credentials: list[str] = Field(default_factory=list)
    # Generated tests are checked independently from implementation correctness.
    test_validity_checks: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Shared R-32d channel for every deterministic check that had to skip a claim.
    # These are uncertainties, never successful validations.
    unresolved_checks: list[UnresolvedCheck] = Field(default_factory=list)
    pending_valid_tests: dict[str, dict[str, str]] = Field(default_factory=dict)
    test_failure_repair_count: int = 0
    # R-32c: evidence from a valid test that exposed behavior outside the
    # approved ticket. Human resolution controls whether scope stays, expands,
    # or aborts; this record itself never authorizes an edit.
    discovered_defect: dict[str, Any] | None = None
    scope_resolution: Literal["stay_scope", "expand_scope"] | None = None
    # One-shot proof that Executor intentionally rewound from a failing valid
    # test to an earlier production step. Guard consumes and clears this marker.
    repair_rewind_from: int | None = Field(default=None, ge=0)

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
