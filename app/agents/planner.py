"""Planner: split a ticket into isolated sub-tasks and assign each its repo.

Runs at the very front of the per-subtask graph, before Diagnosis (architecture.md
§4, §7a). Only the Planner sees the whole ticket; everything downstream sees one
sub-task (R-4). Repo assignment is the Planner's own job (R-26) — never a separate
matching agent, and never a guess when it's ambiguous (R-10).
"""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.llm import LLMClient
from app.agents.router import model_tier
from app.agents.state import BudgetUsed, SubtaskState, SubtaskType
from app.agents.constraints import ExecutionConstraint
from app.core.execution_constraints import append_constraints, scoped_constraints, constraint_description


class SubtaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    spec_id: str = Field(min_length=1)
    type: SubtaskType
    description: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)


class CannotDecompose(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1)


class DecompositionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    subtasks: list[SubtaskSpec] | None = None
    reasoning: str = Field(min_length=1)
    cannot_decompose: CannotDecompose | None = Field(None, alias="CannotDecompose")

    @model_validator(mode="after")
    def exclusive_outcome(self):
        if (self.subtasks is None) == (self.cannot_decompose is None):
            raise ValueError("provide either subtasks or CannotDecompose")
        if self.subtasks is not None:
            if not self.subtasks:
                raise ValueError("subtasks must be a nonempty list")
            if len({s.spec_id for s in self.subtasks}) != len(self.subtasks):
                raise ValueError("spec_id must be unique within a decomposition")
        return self


class PlannerAgent:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or LLMClient()

    def run(self, state: SubtaskState) -> SubtaskState:
        if not state.ticket_requirement:
            state.ticket_requirement = state.description
        for raw in (state.prior_attempt or {}).get("execution_constraints", []):
            item = ExecutionConstraint.model_validate(raw)
            if item.constraint_id in (state.prior_attempt or {}).get("withdrawn_constraint_ids", []):
                continue
            if item.scope_type == "ticket" and item.scope_value == state.ticket_id:
                append_constraints(state.execution_constraints, [item])
        if not state.confirmed_repos:
            return self._cannot_decompose(state, "No confirmed repo(s) for this ticket")
        try:
            result = self.llm.complete_json(
                _SYSTEM_PROMPT,
                json.dumps({
                    "ticket_description": constraint_description(state),
                    "execution_constraints": scoped_constraints(state),
                    "constraint_resolutions": [item.model_dump(mode="json") for item in state.constraint_resolutions],
                    "confirmed_repos": state.confirmed_repos,
                    "repository_overview": state.repo_overview,
                    "prior_attempt_failures": state.attempt_history,
                    "previous_attempt_advisory": state.prior_attempt,
                }),
                DecompositionResult,
                tier=model_tier("planner", ambiguous=len(state.confirmed_repos) > 1),
                ticket_id=state.ticket_id,
            )
            result = DecompositionResult.model_validate(result)
            state.decomposition_reasoning = result.reasoning
            if result.cannot_decompose:
                return self._cannot_decompose(state, result.cannot_decompose.reason)
            specs = result.subtasks
            if len(state.confirmed_repos) == 1:
                # Nothing to decide — never let the model introduce noise here.
                for spec in specs:
                    spec.repo = state.confirmed_repos[0]
            else:
                # Deterministic grounding (R-46 style): never trust the model's
                # self-report about which repos exist. Any repo outside the
                # confirmed list means it guessed instead of flagging ambiguity.
                stray = sorted({s.repo for s in specs if s.repo not in state.confirmed_repos})
                if stray:
                    return self._cannot_decompose(
                        state,
                        f"Assigned repo(s) not in the confirmed list: {', '.join(stray)}. "
                        "A sub-task's repo must be one of the confirmed repos, or flagged ambiguous.",
                    )
            state.subtask_specs = [spec.model_dump() for spec in specs]
            first = specs[0]
            state.subtask_type = first.type
            state.description = first.description
            state.repo = first.repo
            state.depends_on = first.depends_on
            return state
        except Exception as exc:
            from app.core.failures import describe_failure
            return self._cannot_decompose(state, describe_failure("Decomposing the ticket", exc))
        finally:
            state.budget_used = BudgetUsed.model_validate(self.llm.get_usage(state.ticket_id))

    @staticmethod
    def _cannot_decompose(state: SubtaskState, reason: str) -> SubtaskState:
        state.status = "needs_human"
        state.failure_reason = f"CannotDecompose: {reason}"
        return state


_SYSTEM_PROMPT = """
Only ticket_requirement and human_approval_note execution_constraints are authoritative.
critic_correction and prior_attempt records are advisory until explicit human approval.
constraint_resolutions records the human's explicit withdrawals/replacements. Do not restore
withdrawn intent from historical descriptions. Never decide authoritative intent precedence.
You are the Planner. You see the WHOLE ticket — the only agent
that does (sub-tasks are isolated from each other after this point).
The authoritative execution_constraints records carry approved intent with explicit scope; preserve it
when decomposing the ticket. Prior failure text alone never authorizes new intent.

Split ticket_description into an ordered list of isolated sub-tasks, each with a
unique string spec_id, a type (bug|feature|ci|design), a self-contained
description (specific enough that an agent given ONLY this description, with no
other sub-task's context, can act on it), and depends_on (spec_ids of sub-tasks
that must finish first; empty if independent). A ticket describing one clear
change is one sub-task — do not invent extra sub-tasks to look thorough.

REPO ASSIGNMENT (R-26): confirmed_repos is the CONFIRMED, ground-truth list of
repos this ticket may touch — assign each sub-task exactly one repo FROM THIS
LIST. If confirmed_repos has exactly one entry, every sub-task uses it — there is
nothing to decide. If it has several and it is genuinely unclear which repo a
sub-task belongs to, do NOT guess: return CannotDecompose explaining the
ambiguity (which sub-task, which candidate repos) instead of a decomposition. A
sub-task's repo must always be one of confirmed_repos, never invented.

repository_overview contains deterministic file inventory and folder/module
structure for the confirmed repos. Use this lightweight map to recognize real
module boundaries while splitting. It is structural context, not proof of code
behavior; do not invent implementation details from filenames alone.

previous_attempt_advisory may contain same-ticket integration_evidence and an
integration_issue from a failed decomposition. Treat it as advisory evidence that
must be re-verified, but use it to identify which previous sub-tasks, files, symbols,
and combined tests interacted. If integration_interaction=true, do NOT blindly repeat
the same independent decomposition: explain in reasoning how the new dependency graph,
scope boundaries, or grouping addresses the recorded interaction. If the evidence does
not establish one safe split, return CannotDecompose rather than repeating it.

Explain your split briefly in reasoning. If the ticket's intent is too unclear to
decompose at all, return CannotDecompose with a reason and null subtasks.
Otherwise CannotDecompose is null and subtasks is a nonempty list. Never invent a
decomposition just to satisfy the schema."""
