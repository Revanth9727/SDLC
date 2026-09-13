from types import SimpleNamespace

from app.agents.code_intelligence import CodeContext, CodeIntelligenceAgent, InvestigationPlan
from app.agents.state import SubtaskState
from app.core.guard import validate_output


class FakeLLM:
    def __init__(self):
        self.schemas = []

    def complete_json(self, _system, _user, schema, **_kwargs):
        self.schemas.append(schema.__name__)
        if schema is InvestigationPlan:
            return InvestigationPlan(
                investigation_type="data_flow",
                search_queries=["where customer balances are reconciled"],
                symbols=["reconcile_balance"],
            )
        return CodeContext(
            relevant_files=["oddly_named.py", "invented.py"],
            relevant_functions=["reconcile_balance", "invented"],
            execution_path=["reconcile_balance -> ledger.write", "invented -> nowhere"],
            confidence=.86,
            hypothesis="The reconciliation path writes an incorrect ledger balance.",
            repo_snapshot_id="ignored",
            commit_sha="ignored",
        )

    def get_usage(self, _ticket_id):
        return {"calls": 2, "tokens": 300, "est_cost_usd": .01}


class FakeRepo:
    def clone_or_pull(self, *_args):
        return None

    def revision(self, *_args):
        return "a" * 40


class FakeIndexer:
    def ensure(self, *_args):
        return SimpleNamespace(id="snapshot-1")


class FakeSearch:
    embedder = None

    def search_hybrid(self, *_args, **_kwargs):
        return [{
            "path": "oddly_named.py", "start_line": 20, "end_line": 28,
            "line": 20, "label": "function reconcile_balance", "score": .9,
        }]

    def get_file(self, *_args):
        return {
            "path": "oddly_named.py", "start_line": 20, "end_line": 28,
            "content": "def reconcile_balance():\n    ledger.write(total)",
        }

    def find_symbol(self, *_args, **_kwargs):
        return []

    def get_callers(self, *_args, **_kwargs):
        return []

    def get_callees(self, *_args, **_kwargs):
        return [{
            "path": "oddly_named.py", "line": 21, "start_line": 21, "end_line": 21,
            "source": "symbol:reconcile_balance", "target": "symbol:ledger.write",
            "relation": "CALLS", "text": "ledger.write(total)", "verified": True,
        }]

    def get_references(self, *_args, **_kwargs):
        return []


def state():
    return SubtaskState(
        ticket_id="11111111-1111-1111-1111-111111111111",
        subtask_id="22222222-2222-2222-2222-222222222222",
        subtask_type="bug", description="Customer balances drift after reconciliation",
        repo="owner/repo", confirmed_repos=["owner/repo"],
    )


def test_investigator_writes_only_verified_context_to_blackboard():
    original = state()
    result = CodeIntelligenceAgent(
        llm=FakeLLM(), repo_tool=FakeRepo(), search_tool=FakeSearch(),
        indexer=FakeIndexer(), max_steps=8,
    ).run(original.model_copy(deep=True))
    checked = validate_output(original, result, "code_intelligence")

    assert checked.code_context["relevant_files"] == ["oddly_named.py"]
    assert "invented" not in checked.code_context["relevant_functions"]
    assert checked.code_context["repo_snapshot_id"] == "snapshot-1"
    assert checked.code_context["commit_sha"] == "a" * 40
    assert all(item["verified"] for item in checked.code_context["verified_evidence"])


def test_investigation_cap_returns_an_honest_low_confidence_result():
    result = CodeIntelligenceAgent(
        llm=FakeLLM(), repo_tool=FakeRepo(), search_tool=FakeSearch(),
        indexer=FakeIndexer(), max_steps=1,
    ).run(state())

    assert result.code_context["limit_reached"] is True
    assert result.code_context["confidence"] <= .2
    assert result.code_context["verified_evidence"] == []


def test_ticket_ui_surfaces_investigator_confidence_and_evidence():
    template = open("app/web/templates/ticket.html", encoding="utf-8").read()
    assert 'id="code-intelligence-panel"' in template
    assert 'id="code-confidence"' in template
    assert "verified_evidence" in template
