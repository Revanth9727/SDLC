"""Critic: validate the Executor's completed change against the ticket, before PR.

Backstops the pipeline (architecture.md §7d) — approves, or sends the change back
to the Executor with concrete issues to fix. Never touches the world itself (R-6);
it only reasons about what the Executor already did and reports a verdict.
"""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.llm import LLMClient
from app.agents.router import model_tier
from app.agents.state import BudgetUsed, SubtaskState, UnresolvedCheck
from app.core.code_impact import SymbolImpact, inspect_symbols, merge_impacts
from app.core.execution_constraints import scoped_constraints, constraint_description
from app.tools.code_search import CodeSearchTool
from app.tools.repo_tool import RepoTool


class CriticVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool
    issues: list[str] = Field(default_factory=list)
    verifiability: Literal["ok", "no_tests", "uncovered_change"] = "ok"
    test_validity: Literal["valid", "invalid", "uncertain"]
    test_issues: list[str] = Field(default_factory=list)
    implementation_valid: bool
    confidence: Literal["high", "medium", "low"] | None = None
    unresolved_checks: list[UnresolvedCheck] = Field(default_factory=list)

    @model_validator(mode="after")
    def approval_requires_both_validities(self):
        if self.approved and (self.test_validity != "valid" or not self.implementation_valid):
            raise ValueError("Approval requires a valid test and valid implementation")
        if self.test_validity == "invalid" and not self.test_issues:
            raise ValueError("An invalid test requires a specific test_issues explanation")
        if self.unresolved_checks and self.confidence not in {"medium", "low"}:
            raise ValueError("A verdict with unresolved checks cannot claim high or unspecified confidence")
        return self

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
            current = inspect_symbols(
                state, prior.target_file, [prior.symbol], self.code_search, limit=25
            )
            for impact in current:
                impact.contract_changed = prior.contract_changed
                impact.contract_changes = prior.contract_changes
            refreshed.extend(current or [prior])
        if refreshed:
            merge_impacts(state, refreshed)
        last_tests = state.steps_done[-1]["tests"] if state.steps_done else {}
        payload = {
            "execution_constraints": scoped_constraints(state),
            "constraint_resolutions": [item.model_dump(mode="json") for item in state.constraint_resolutions],
            "ticket_description": constraint_description(state),
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
            "deterministic_test_validity": state.test_validity_checks,
            # R-32d: these checks did not pass. They could not be proven either
            # way and must remain explicit uncertainty in the final verdict.
            "checks_that_could_not_be_statically_verified": [
                item.model_dump(mode="json") for item in state.unresolved_checks
            ],
            "prior_critic_feedback": state.critic_feedback,
            "prior_attempt_failures": state.attempt_history,
            "graph_impact_checks": state.code_impacts,
            "integration_reconciliation_evidence": state.integration_review_evidence,
        }
        try:
            verdict = self.llm.complete_json(
                _SYSTEM_PROMPT, json.dumps(payload), CriticVerdict,
                tier=model_tier("critic"), ticket_id=state.ticket_id,
            )
            verdict = CriticVerdict.model_validate(verdict)
            if state.unresolved_checks:
                verdict.unresolved_checks = list(state.unresolved_checks)
                if verdict.confidence not in {"medium", "low"}:
                    verdict.confidence = "medium"
                verdict = CriticVerdict.model_validate(verdict)
            dumped = verdict.model_dump(mode="json")
            # Keep old checkpoint shape stable when there is no R-32d uncertainty.
            if not verdict.unresolved_checks:
                dumped.pop("unresolved_checks", None)
            if verdict.confidence is None:
                dumped.pop("confidence", None)
            state.critic_verdict = dumped
        finally:
            state.budget_used = BudgetUsed.model_validate(self.llm.get_usage(state.ticket_id))
        return state


_SYSTEM_PROMPT = """
Only ticket_requirement and human_approval_note execution_constraints are authoritative.
critic_correction and prior_attempt records are advisory until explicit human approval.
constraint_resolutions records the human's explicit withdrawals/replacements. Do not restore
withdrawn intent from historical descriptions. Never decide authoritative intent precedence.
You are the Critic: the last check before a pull request opens.
The authoritative execution_constraints records are approved intent with explicit scope; reject
code or tests contradicting applicable constraints. Do not invent new business intent.
Validate the COMPLETED change (all steps_done, changed_files) against
ticket_description and diagnosis. You do not edit anything — you only judge.
Evaluate two independent questions. First, TEST validity: do generated tests
faithfully represent the ticket, with internally consistent setup and assertions?
Set test_validity and test_issues explicitly. Deterministic_test_validity contains
the source-proven pre-run checks, but you must review subtler semantics yourself.
Second, IMPLEMENTATION validity: does the implementation satisfy the requirement
and every valid test? Set implementation_valid explicitly. Never treat code as wrong
merely because an invalid test fails. approved=true requires test_validity=valid and
implementation_valid=true.
checks_that_could_not_be_statically_verified contains deterministic checks that were
SKIPPED because their target could not be resolved. They are uncertainty, not passed
checks. Consider their stated impact when judging the change. You may still approve
when the available runtime evidence is sufficient, but name every unresolved check in
unresolved_checks and set confidence to medium or low; never silently turn a skip into
successful verification. If the list is empty, unresolved_checks must be empty.
integration_reconciliation_evidence, when present, describes the original combined-test
failure, every bounded reconciliation attempt and its explicit file delta, and the final
combined-test result. Judge the FINAL changed_files against that complete trajectory; a
passing final test does not erase the original interaction or an unexplained reconciliation.
Treat graph_impact_checks as verified navigation evidence and changed_files as the
actual completed diff. Caller/reference count is a risk signal, never a reason by
itself to reject. First inspect contract_changed and contract_changes: signature,
return type/shape, raised exceptions, or documented behavior. If the contract is
unchanged, do not reject because callers exist. If it changed, inspect the verified
caller/reference evidence and test results. Reject only when that evidence shows an
affected caller is broken or its changed behavior is unverified; name the concrete
path and incompatibility. Passing affected tests are evidence that callers remain
compatible, not proof to ignore a demonstrated breakage.

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
