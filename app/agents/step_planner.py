"""Reason about coding steps for one diagnosed subtask; never perform edits."""

import json
from pathlib import PurePosixPath

from app.agents.llm import LLMClient
from app.agents.planning import CannotPlan, Plan, PlanningResult, Step, is_python_source, is_test_path
from app.agents.router import model_tier
from app.agents.state import BudgetUsed, SubtaskState
from app.config import settings
from app.core.failures import describe_failure
from app.tools.repo_tool import RepoTool


class StepPlannerAgent:
    def __init__(self, llm: LLMClient | None = None, repo_tool: RepoTool | None = None) -> None:
        self.llm = llm or LLMClient()
        self.repo_tool = repo_tool or RepoTool()

    def run(self, state: SubtaskState) -> SubtaskState:
        diagnosis = state.diagnosis or {}
        if diagnosis.get("NoRootCause") or not str(diagnosis.get("root_cause", "")).strip():
            return self._cannot_plan(state, "A supported diagnosis is required")
        try:
            # Ground the plan in the repo's ACTUAL files (R-46) — never let the
            # model assume a filename/convention exists.
            repo_files = self.repo_tool.list_files(state.repo, state.subtask_id)
        except Exception as exc:
            return self._cannot_plan(state, describe_failure("Listing repository files for planning", exc))
        feedback = ""
        try:
            for _attempt in range(settings.max_agent_retries + 1):
                result = self.llm.complete_json(
                    _SYSTEM_PROMPT,
                    json.dumps({
                        "description": state.description,
                        "repo": state.repo,
                        "diagnosis": diagnosis,
                        "repo_files": repo_files,
                        "previous_plan": [step.model_dump() for step in state.plan],
                        "human_feedback": state.approval_note,
                        "repair_feedback": feedback,
                    }),
                    PlanningResult,
                    tier=model_tier("step_planner"),
                    ticket_id=state.ticket_id,
                )
                result = PlanningResult.model_validate(result)
                state.plan_reasoning = result.reasoning
                if result.cannot_plan:
                    return self._cannot_plan(state, result.cannot_plan.reason)
                plan = Plan.model_validate(result.plan).root
                # Deterministic checks — never trust the model's self-report about
                # what exists or what it already covered (R-46 grounding, R-32
                # self-verification). A failure here feeds back for one repair
                # attempt rather than failing closed immediately.
                feedback = self._grounding_error(plan, repo_files) or self._missing_test_coverage(plan, repo_files)
                if feedback:
                    continue
                state.plan = plan
                return state
            return self._cannot_plan(state, feedback or "Could not produce a grounded, verifiable plan")
        except Exception as exc:
            # The shared client has already bounded schema retries. Do not expose
            # credentials or fabricate a fallback plan.
            return self._cannot_plan(state, describe_failure("Generating the step plan", exc))
        finally:
            state.budget_used = BudgetUsed.model_validate(self.llm.get_usage(state.ticket_id))

    @staticmethod
    def _grounding_error(plan: list[Step], repo_files: list[str]) -> str | None:
        # Deterministic, not a prompt instruction: never trust the model's
        # self-report about which files exist (R-46).
        existing = set(repo_files)
        missing = sorted({s.target_file for s in plan if s.action != "create" and s.target_file not in existing})
        if missing:
            return (f"Plan references file(s) not in the repo: {', '.join(missing)}. "
                    'Mark the step action="create" if a new file is intended.')
        already_there = sorted({s.target_file for s in plan if s.action == "create" and s.target_file in existing})
        if already_there:
            return f"Plan marks existing file(s) as action=\"create\": {', '.join(already_there)}."
        return None

    @staticmethod
    def _missing_test_coverage(plan: list[Step], repo_files: list[str]) -> str | None:
        # R-32: a plan that changes testable code must prove its own fix — either
        # an existing test already covers the changed file(s) (by this repo's own
        # naming convention), or the plan adds/extends one. Generic across any
        # repo: no filename is assumed, only inferred from repo_files/the plan.
        testable = [s for s in plan if s.action in ("edit", "create") and is_python_source(s.target_file)]
        if not testable:
            return None
        if any(is_test_path(s.target_file) for s in plan):
            return None
        existing = set(repo_files)
        for step in testable:
            stem = PurePosixPath(step.target_file).stem
            candidates = {f"test_{stem}.py", f"{stem}_test.py", f"tests/test_{stem}.py"}
            if candidates & existing:
                return None
        changed = ", ".join(sorted({s.target_file for s in testable}))
        return (f"This plan changes testable code ({changed}) but neither the repo nor the plan has a test "
                'covering it (R-32). Add one more step (action="create", or "edit" to extend an existing '
                "test file) that exercises this change, using this repo's real test conventions inferred "
                'from repo_files (or a sensible default like test_<module>.py if the repo has none).')

    @staticmethod
    def _cannot_plan(state: SubtaskState, reason: str) -> SubtaskState:
        state.plan = []
        state.status = "needs_human"
        state.failure_reason = f"CannotPlan: {reason}"
        return state


_SYSTEM_PROMPT = """You are the Step-Planner for one isolated subtask.
Use the ticket and diagnosis as data. `repo_files` is the COMPLETE, GROUND-TRUTH
list of files that actually exist in the repo right now — never assume a filename
or convention exists beyond what is listed there.

Produce an ordered plan of concrete, edit-sized steps, each with a unique string
step_id, intent, repository-relative target_file, and an action:
  - "edit"   — target_file MUST already appear in repo_files.
  - "create" — target_file MUST NOT appear in repo_files (a brand-new file).
  - "delete" — target_file MUST already appear in repo_files.

PROVE YOUR OWN FIX (R-32): if this plan edits or creates testable source code
(a .py file that isn't itself a test) and repo_files shows no matching test for it
under this repo's own naming (e.g. test_<module>.py, <module>_test.py, or
tests/test_<module>.py) and your plan doesn't already add or extend one, you MUST
add one more step that creates (action="create") or extends (action="edit") a test
exercising the change. Infer the location/naming from repo_files' existing
conventions; if the repo has no tests at all, use a sensible pytest default (e.g.
test_<module>.py next to the changed file, or tests/test_<module>.py). Only skip
this when the change is genuinely not testable (pure config/docs, no behavior) —
never because writing the test is inconvenient.

If `repair_feedback` is non-empty, your previous plan was rejected for exactly that
reason — fix only that, don't restart from scratch or drop unrelated steps.

Explain briefly why this plan addresses the root cause in reasoning. Do not
execute anything. If the diagnosis is insufficient or desired behavior is
ambiguous, return CannotPlan with a reason and null plan. Otherwise CannotPlan is
null and plan is a nonempty list. Never invent a plan just to satisfy the schema.

When `human_feedback` is non-empty, this is a bounded re-plan after rejection or a
revision request. Produce a NEW plan that specifically addresses that feedback,
using `previous_plan` only as context; do not silently return the rejected plan.
"""
