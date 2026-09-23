import uuid

import pytest

from app.agents.critic import CriticAgent
from app.agents.planning import Step
from app.agents.state import SubtaskState
from app.core.code_impact import (
    CodeNavigationContractError,
    contract_changes,
    inspect_impacts,
    symbols_touched,
)
from app.core.integration import cross_subtask_graph_risks


class Graph:
    def get_callers(self, repo, subtask_id, symbol, limit=25):
        return [{"path": "api.py", "line": 8, "text": f"{symbol}()", "verified": True}]

    def get_references(self, repo, subtask_id, symbol, limit=25):
        return [{"path": "tests/test_service.py", "line": 4, "text": symbol, "verified": True}]


class LLM:
    def complete_json(self, system, user, schema, **kwargs):
        return schema(approved=True, issues=[], verifiability="ok", test_validity="valid",
                      test_issues=[], implementation_valid=True)

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


def test_impact_navigation_accepts_empty_results():
    class EmptyGraph:
        def get_callers(self, repo, subtask_id, symbol, limit=25):
            return []

        def get_references(self, repo, subtask_id, symbol, limit=25):
            return []

    impacts = inspect_impacts(
        state(current_step=0, execution_complete=False),
        "service.py", "def calculate():\n    return 1\n", ["return 1"], EmptyGraph(),
    )
    assert impacts[0].callers == []
    assert impacts[0].references == []


def test_impact_navigation_none_is_a_controlled_contract_failure():
    class MissingGraph(Graph):
        def get_callers(self, repo, subtask_id, symbol, limit=25):
            return None

    with pytest.raises(CodeNavigationContractError, match="get_callers returned no result"):
        inspect_impacts(
            state(current_step=0, execution_complete=False),
            "service.py", "def calculate():\n    return 1\n", ["return 1"], MissingGraph(),
        )


@pytest.mark.parametrize("bad_result", [[None], [{}], "not-a-list"])
def test_impact_navigation_malformed_result_is_a_controlled_contract_failure(bad_result):
    class MalformedGraph(Graph):
        def get_references(self, repo, subtask_id, symbol, limit=25):
            return bad_result

    with pytest.raises(CodeNavigationContractError, match="get_references returned a malformed result"):
        inspect_impacts(
            state(current_step=0, execution_complete=False),
            "service.py", "def calculate():\n    return 1\n", ["return 1"], MalformedGraph(),
        )


def test_critic_does_not_reject_unchanged_contract_with_many_callers():
    class PopularGraph(Graph):
        def get_callers(self, repo, subtask_id, symbol, limit=25):
            return [
                {"path": f"consumer_{index}.py", "line": 8, "text": f"{symbol}()", "verified": True}
                for index in range(12)
            ]

        def get_references(self, repo, subtask_id, symbol, limit=25):
            return []

    before = "def calculate(value: int) -> int:\n    return value + 1\n"
    after = "def calculate(value: int) -> int:\n    return value + 2\n"
    current = state()
    impacts = inspect_impacts(
        current, "service.py", before, ["return value + 1"], PopularGraph(), updated_source=after,
    )
    assert impacts[0].contract_changed is False
    assert len(impacts[0].uncovered_paths) == 12
    current.code_impacts = [impact.model_dump() for impact in impacts]

    result = CriticAgent(LLM(), code_search=PopularGraph()).run(current)
    assert result.critic_verdict["approved"] is True
    assert result.critic_verdict["issues"] == []
    assert len(result.code_impacts[0]["uncovered_paths"]) == 12
    assert result.code_impacts[0]["contract_changed"] is False


def test_contract_change_is_flagged_and_given_to_critic_for_verification():
    before = "def calculate(value: int) -> int:\n    return value\n"
    after = "def calculate(value: int) -> str:\n    return str(value)\n"
    changes = contract_changes(before, after, "calculate")
    assert changes == ["return type annotation changed"]

    current = state()
    impacts = inspect_impacts(
        current, "service.py", before, ["return value"], Graph(), updated_source=after,
    )
    assert impacts[0].contract_changed is True
    assert impacts[0].contract_changes == changes

    class ContractReviewLLM(LLM):
        def complete_json(self, system, user, schema, **kwargs):
            import json
            impact = json.loads(user)["graph_impact_checks"][0]
            assert impact["contract_changed"] is True
            assert impact["contract_changes"] == ["return type annotation changed"]
            assert impact["uncovered_paths"] == ["api.py", "tests/test_service.py"]
            return schema(
                approved=False,
                issues=["api.py expects calculate() to return int, but it now returns str"],
                verifiability="uncovered_change",
                test_validity="valid",
                test_issues=[],
                implementation_valid=False,
            )

    current.code_impacts = [impact.model_dump() for impact in impacts]
    result = CriticAgent(ContractReviewLLM(), code_search=Graph()).run(current)
    assert result.critic_verdict["approved"] is False
    assert "returns str" in result.critic_verdict["issues"][0]


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
