"""Critic: validate the Executor's completed change against the ticket, before PR.

Backstops the pipeline (architecture.md §7d) — approves, or sends the change back
to the Executor with concrete issues to fix. Never touches the world itself (R-6);
it only reasons about what the Executor already did and reports a verdict.
"""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.llm import LLMClient
from app.agents.router import model_tier
from app.agents.state import BudgetUsed, SubtaskState
from app.core.code_impact import SymbolImpact, impact_warning, inspect_symbols, merge_impacts
from app.tools.code_search import CodeSearchTool
from app.tools.repo_tool import RepoTool


class CriticVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool
    issues: list[str] = Field(default_factory=list)
    verifiability: Literal["ok", "no_tests", "uncovered_change"] = "ok"

    @property
    def summary(self) -> str:
        if self.approved:
            return "Approved" + (f" (verifiability: {self.verifiability})" if self.verifiability != "ok" else "")
        return "Rejected: " + "; ".join(self.issues)


class CriticAgent:
    def __init__(self, llm: LLMClient | None = None, code_search=None, repo_tool=None) -> None:
        self.llm = llm or LLMClient()
        self.repo_tool = repo_tool or RepoTool()
        self.code_search = code_search or CodeSearchTool(repo_tool=self.repo_tool)

    def run(self, state: SubtaskState) -> SubtaskState:
        refreshed = []
        for raw in state.code_impacts[:8]:
            prior = SymbolImpact.model_validate(raw)
            refreshed.extend(inspect_symbols(
                state, prior.target_file, [prior.symbol], self.code_search, limit=25
            ) or [prior])
        if refreshed:
            merge_impacts(state, refreshed)
        graph_warning = impact_warning([SymbolImpact.model_validate(item) for item in state.code_impacts])
        last_tests = state.steps_done[-1]["tests"] if state.steps_done else {}
        payload = {
            "ticket_description": state.description,
            "diagnosis": state.diagnosis,
            "plan": [step.model_dump() for step in state.plan],
            "steps_done": [
                {"step_id": s["step_id"], "intent": s["intent"], "target_file": s["target_file"],
                 "test_outcome": s["tests"]["outcome"]}
                for s in state.steps_done
            ],
            "changed_files": state.file_changes,
            "last_test_result": last_tests,
            # What the Executor/test-runner already established (ai_rules.md R-46) —
            # the Critic reads this rather than re-deriving it from scratch.
            "executor_verifiability": state.verifiability,
            "prior_critic_feedback": state.critic_feedback,
            "graph_impact_checks": state.code_impacts,
        }
        try:
            verdict = self.llm.complete_json(
                _SYSTEM_PROMPT, json.dumps(payload), CriticVerdict,
                tier=model_tier("critic"), ticket_id=state.ticket_id,
            )
            verdict = CriticVerdict.model_validate(verdict)
            if graph_warning:
                verdict.approved = False
                verdict.verifiability = "uncovered_change"
                if graph_warning not in verdict.issues:
                    verdict.issues.append(graph_warning)
            state.critic_verdict = verdict.model_dump()
        finally:
            state.budget_used = BudgetUsed.model_validate(self.llm.get_usage(state.ticket_id))
        return state


_SYSTEM_PROMPT = """You are the Critic: the last check before a pull request opens.
Validate the COMPLETED change (all steps_done, changed_files) against
ticket_description and diagnosis. You do not edit anything — you only judge.
Treat graph_impact_checks as verified navigation evidence. Reject an edit when its
changed symbol has callers/references outside the approved plan; name those paths.

approved=true only if the change plausibly fixes the described problem, does not
contradict the diagnosis, and introduces no obvious new defect. Otherwise
approved=false with issues: a list of SPECIFIC, actionable problems (e.g. "the
guard only checks b==0 but the ticket also describes negative b" or "the new
branch in divide() for b<0 has no assertion exercising it") — an Executor must be
able to act on each one without further clarification. Never approve just because
it compiles and tests passed; also never invent issues that aren't real for the
sake of having something to say.

Verifiability (ai_rules.md R-32/R-46), reported for information — do not fail a
change SOLELY for verifiability, only for genuine correctness/coverage problems:
- "no_tests": executor_verifiability is "no_tests" (the repo has no tests and the
  plan added none) — the fix cannot be meaningfully verified. Report it; this alone
  is not grounds for rejection since the Executor cannot invent test infrastructure
  the plan already decided against.
- "uncovered_change": the change added new code/branches (compare changed_files
  against the tests actually present) that no test in steps_done/changed_files
  exercises. This IS actionable — if so, also add a specific issue naming the
  uncovered branch and set approved=false so the Executor adds coverage for it.
- "ok": otherwise.

If prior_critic_feedback is non-empty, this is a re-review after the Executor
redid the work for exactly that feedback — check whether it was actually
addressed, don't repeat feedback that's already resolved."""
