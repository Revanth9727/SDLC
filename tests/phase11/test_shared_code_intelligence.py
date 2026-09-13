import uuid

from app.agents.critic import CriticAgent
from app.agents.planning import Step
from app.agents.state import SubtaskState
from app.core.code_impact import inspect_impacts, symbols_touched
from app.core.integration import cross_subtask_graph_risks


class Graph:
    def get_callers(self, repo, subtask_id, symbol, limit=25):
        return [{"path": "api.py", "line": 8, "text": f"{symbol}()", "verified": True}]

    def get_references(self, repo, subtask_id, symbol, limit=25):
        return [{"path": "tests/test_service.py", "line": 4, "text": symbol, "verified": True}]


class LLM:
    def complete_json(self, system, user, schema, **kwargs):
        return schema(approved=True, issues=[], verifiability="ok")

    def get_usage(self, ticket_id):
        return {"calls": 1, "tokens": 10, "est_cost_usd": 0.0}


def state(**changes):
    values = dict(
        ticket_id=str(uuid.uuid4()), subtask_id=str(uuid.uuid4()), subtask_type="bug",
        description="Change calculate", repo="org/repo", approval_status="approved",
        plan=[Step(step_id="1", intent="Change calculate", target_file="service.py")],
        current_step=1, execution_complete=True, verifiability="verified",
    )
    values.update(changes)
    return SubtaskState(**values)


def test_executor_impact_check_finds_symbol_and_unplanned_dependents():
    source = "def calculate():\n    return 1\n"
    current = state(current_step=0, execution_complete=False)
    assert symbols_touched("service.py", source, ["    return 1"], {}) == ["calculate"]
    impacts = inspect_impacts(current, "service.py", source, ["    return 1"], Graph())
    assert impacts[0].uncovered_paths == ["api.py", "tests/test_service.py"]


def test_critic_rechecks_graph_and_rejects_missed_callers():
    current = state(code_impacts=[{
        "symbol": "calculate", "target_file": "service.py", "callers": [],
        "references": [], "uncovered_paths": [], "checked": True,
    }])
    result = CriticAgent(LLM(), code_search=Graph()).run(current)
    assert result.critic_verdict["approved"] is False
    assert result.critic_verdict["verifiability"] == "uncovered_change"
    assert "api.py" in result.critic_verdict["issues"][0]


def test_integration_attributes_graph_dependency_across_subtasks():
    first = state(spec_id="A", code_impacts=[{
        "symbol": "calculate", "target_file": "service.py",
        "callers": [{"path": "api.py", "verified": True}], "references": [],
        "uncovered_paths": ["api.py"], "checked": True,
    }])
    second = state(spec_id="B", code_context={"relevant_files": ["api.py"]})
    risks = cross_subtask_graph_risks([first, second])
    assert len(risks) == 1
    assert "A changes calculate" in risks[0] and "B relies" in risks[0]
