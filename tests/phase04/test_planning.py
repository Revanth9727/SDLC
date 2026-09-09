from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents.planning import Plan, PlanningResult
from app.agents.state import SubtaskState
from app.agents.step_planner import StepPlannerAgent


def state():
    return SubtaskState(ticket_id="ticket", subtask_id="subtask", subtask_type="bug",
                        description="Handle division by zero", repo="owner/repo",
                        diagnosis={"root_cause": "unguarded division", "files": ["app.py"]})


class LLM:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def complete_json(self, system, user, schema, **kwargs):
        self.calls.append((user, kwargs))
        return schema.model_validate(self.result)

    def get_usage(self, ticket_id):
        return {"calls": 1, "tokens": 100, "est_cost_usd": 0.001}


class FakeRepoTool:
    """Ground truth file listing (R-46) — no real clone in these unit tests."""
    def __init__(self, files=("app.py",)):
        self.files = list(files)

    def list_files(self, repo, subtask_id):
        return self.files


def test_planner_validates_and_uses_cheap_tier():
    # An existing test_app.py already covers app.py, so R-32 self-verification
    # is satisfied without the plan needing its own test step.
    llm = LLM({"plan": [{"step_id": "1", "intent": "Guard zero denominator", "target_file": "app.py"}],
               "reasoning": "Avoid unhandled division by zero"})
    result = StepPlannerAgent(llm, FakeRepoTool(["app.py", "test_app.py"])).run(state())
    assert result.plan[0].target_file == "app.py"
    assert result.budget_used.calls == 1
    assert llm.calls[0][1]["tier"] == "cheap"
    assert 'unguarded division' in llm.calls[0][0]
    assert 'app.py' in llm.calls[0][0]  # repo_files grounding is passed to the model


def test_planner_requires_a_test_step_for_untested_code_then_accepts_it():
    class TwoPassLLM(LLM):
        def __init__(self):
            super().__init__(None)
        def complete_json(self, system, user, schema, **kwargs):
            self.calls.append((user, kwargs))
            if len(self.calls) == 1:
                self.result = {"plan": [{"step_id": "1", "intent": "Guard zero denominator", "target_file": "app.py"}],
                               "reasoning": "Avoid unhandled division by zero"}
            else:
                assert "test" in user.lower() and "app.py" in user  # repair feedback reached the model
                self.result = {"plan": [
                    {"step_id": "1", "intent": "Guard zero denominator", "target_file": "app.py"},
                    {"step_id": "2", "intent": "Cover the guard", "target_file": "test_app.py", "action": "create"},
                ], "reasoning": "Avoid unhandled division by zero, with coverage"}
            return schema.model_validate(self.result)

    llm = TwoPassLLM()
    result = StepPlannerAgent(llm, FakeRepoTool(["app.py"])).run(state())
    assert len(result.plan) == 2
    assert result.plan[1].target_file == "test_app.py" and result.plan[1].action == "create"
    assert len(llm.calls) == 2


def test_planner_cannot_plan_when_untested_and_never_gets_a_test_step():
    llm = LLM({"plan": [{"step_id": "1", "intent": "Guard zero denominator", "target_file": "app.py"}],
               "reasoning": "Avoid unhandled division by zero"})
    output = StepPlannerAgent(llm, FakeRepoTool(["app.py"])).run(state())
    assert output.status == "needs_human"
    assert "neither the repo nor the plan has a test" in output.failure_reason
    assert len(llm.calls) == 3  # bounded by max_agent_retries, never an open-ended loop


def test_planner_skips_test_requirement_for_non_python_or_test_targeted_changes():
    llm = LLM({"plan": [{"step_id": "1", "intent": "Update the changelog", "target_file": "CHANGELOG.md"}],
               "reasoning": "Document the fix"})
    result = StepPlannerAgent(llm, FakeRepoTool(["app.py", "CHANGELOG.md"])).run(state())
    assert result.plan[0].target_file == "CHANGELOG.md"
    assert len(llm.calls) == 1


def test_planner_rejects_step_targeting_a_file_not_in_the_repo():
    llm = LLM({"plan": [{"step_id": "1", "intent": "Fix it", "target_file": "test_app.py"}],
               "reasoning": "Add a regression test"})
    output = StepPlannerAgent(llm, FakeRepoTool(["app.py"])).run(state())
    assert output.status == "needs_human"
    assert "not in the repo" in output.failure_reason
    assert "test_app.py" in output.failure_reason


def test_planner_rejects_create_step_for_an_existing_file():
    llm = LLM({"plan": [{"step_id": "1", "intent": "Fix it", "target_file": "app.py", "action": "create"}],
               "reasoning": "..."})
    output = StepPlannerAgent(llm, FakeRepoTool(["app.py"])).run(state())
    assert output.status == "needs_human"
    assert "existing file" in output.failure_reason


def test_planner_accepts_create_step_for_a_new_file():
    llm = LLM({"plan": [{"step_id": "1", "intent": "Add coverage", "target_file": "test_app.py", "action": "create"}],
               "reasoning": "..."})
    result = StepPlannerAgent(llm, FakeRepoTool(["app.py"])).run(state())
    assert result.plan[0].target_file == "test_app.py"
    assert result.plan[0].action == "create"


@pytest.mark.parametrize("result", [
    {"CannotPlan": {"reason": "Desired zero behavior is unclear"}, "reasoning": "Need clarification"},
    {"plan": [], "reasoning": "invalid"},
    {"plan": [{"step_id": "1", "intent": "", "target_file": "app.py"}], "reasoning": "invalid"},
])
def test_cannot_plan_exits_to_human(result):
    output = StepPlannerAgent(LLM(result), FakeRepoTool()).run(state())
    assert output.status == "needs_human"
    assert output.failure_reason.startswith("CannotPlan")
    assert output.plan == []


def test_missing_diagnosis_does_not_call_llm():
    s = state()
    s.diagnosis = None
    llm = LLM(None)
    assert StepPlannerAgent(llm, FakeRepoTool()).run(s).status == "needs_human"
    assert not llm.calls


def test_planner_preserves_underlying_provider_error():
    class FailedLLM(LLM):
        def complete_json(self, *args, **kwargs):
            raise TimeoutError("provider timed out after 30 seconds")

    output = StepPlannerAgent(FailedLLM(None), FakeRepoTool()).run(state())
    assert output.status == "needs_human"
    assert "Generating the step plan failed" in output.failure_reason
    assert "TimeoutError" in output.failure_reason
    assert "provider timed out after 30 seconds" in output.failure_reason


def test_planner_cannot_plan_when_repo_listing_fails():
    class BrokenRepoTool:
        def list_files(self, repo, subtask_id):
            raise RuntimeError("clone failed")

    output = StepPlannerAgent(LLM(None), BrokenRepoTool()).run(state())
    assert output.status == "needs_human"
    assert "Listing repository files" in output.failure_reason


@pytest.mark.parametrize("path", ["/etc/passwd", "../app.py", "src/../../app.py", "C:\\app.py", "."])
def test_plan_rejects_unsafe_paths(path):
    with pytest.raises(ValidationError):
        Plan.model_validate([{"step_id": "1", "intent": "fix", "target_file": path}])


def test_plan_rejects_duplicate_ids():
    with pytest.raises(ValidationError):
        Plan.model_validate([{"step_id": "1", "intent": "fix", "target_file": "app.py"}] * 2)
