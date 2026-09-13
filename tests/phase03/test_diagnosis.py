from types import SimpleNamespace

import pytest

from app.agents.diagnosis import Diagnosis, DiagnosisAgent
from app.agents.state import SubtaskState
from app.orchestrator import graph as graph_module


class _FakeRepoTool:
    def __init__(self) -> None:
        self.reads: list[str] = []

    def revision(self, repo, subtask_id):
        return "a" * 40

    def list_files(self, repo: str, subtask_id: str) -> list[str]:
        return ["README.md", "app.py", "tests/test_app.py", "image.png"]

    def read_file(self, repo: str, subtask_id: str, path: str) -> str:
        self.reads.append(path)
        return "def divide(a, b):\n    return a / b\n"

    def clone_or_pull(self, repo, subtask_id):
        return None

    def _validate_full_name(self, repo):
        return None

    def _workspace_id(self, subtask_id):
        return subtask_id

    def _checkout_path(self, repo, subtask_id):
        from pathlib import Path
        return Path("/missing-test-checkout")

    def file_fingerprints(self, repo, subtask_id, paths):
        return {path: "fingerprint" for path in paths}


class _FakeLLM:
    def __init__(self, diagnosis: Diagnosis) -> None:
        self.diagnosis = diagnosis
        self.calls: list[dict] = []

    def complete_json(self, system, user, schema, tier, ticket_id):
        self.calls.append(
            {
                "system": system,
                "user": user,
                "schema": schema,
                "tier": tier,
                "ticket_id": ticket_id,
            }
        )
        return self.diagnosis

    def get_usage(self, ticket_id: str) -> dict:
        return {"calls": 1, "tokens": 123, "est_cost_usd": 0.001}


class _FakeSearch:
    def search_exact(self, repo, subtask_id, query, limit=20):
        return ([{"path": "app.py", "line": 1, "column": 5, "text": "def divide(a, b):"}]
                if query == "divide" else [])

    def find_symbol(self, repo, subtask_id, symbol, limit=10):
        return self.search_exact(repo, subtask_id, symbol, limit) if symbol == "divide" else []

    def get_callers(self, *args, **kwargs):
        return []

    def get_callees(self, *args, **kwargs):
        return []

    def get_references(self, *args, **kwargs):
        return []

    def get_file(self, repo, subtask_id, path, start, end):
        return {"path": path, "start_line": start, "end_line": end,
                "content": "def divide(a, b):\n    return a / b"}


def _state() -> SubtaskState:
    state = SubtaskState(
        ticket_id="11111111-1111-1111-1111-111111111111",
        subtask_id="22222222-2222-2222-2222-222222222222",
        subtask_type="bug",
        description="The app crashes with divide by zero in app.py",
        repo="owner/repo",
        confirmed_repos=["owner/repo"],
    )
    state.code_context = {
        "relevant_files": ["app.py"],
        "relevant_functions": ["divide"],
        "execution_path": ["divide"],
        "confidence": .9,
        "hypothesis": "divide lacks a zero guard",
        "verified_evidence": [{"verified": True, "path": "app.py"}],
        "relevant_chunks": [{"path": "app.py", "start_line": 1, "end_line": 2}],
        "repo_snapshot_id": "snapshot",
        "commit_sha": "a" * 40,
    }
    return state


def test_diagnosis_agent_writes_successful_diagnosis() -> None:
    llm = _FakeLLM(
        Diagnosis(
            root_cause="divide() does not guard b == 0",
            files=["app.py"],
            reasoning="The code returns a / b directly.",
        )
    )
    agent = DiagnosisAgent(llm=llm, repo_tool=_FakeRepoTool(), search_tool=_FakeSearch())

    result = agent.run(_state())

    assert result.status == "running"
    assert result.diagnosis["root_cause"] == "divide() does not guard b == 0"
    assert result.budget_used.calls == 1
    assert llm.calls[0]["tier"] == "strong"
    assert "app.py" in llm.calls[0]["user"]


def test_diagnosis_agent_has_no_root_cause_exit() -> None:
    agent = DiagnosisAgent(
        llm=_FakeLLM(
            Diagnosis(
                root_cause="",
                files=[],
                reasoning="Insufficient files.",
                NoRootCause=True,
            )
        ),
        repo_tool=_FakeRepoTool(),
        search_tool=_FakeSearch(),
    )

    result = agent.run(_state())

    assert result.status == "needs_human"
    assert result.failure_reason == "Diagnosis could not determine a root cause: Insufficient files."


class _FakeAgent:
    def run(self, state: SubtaskState) -> SubtaskState:
        state.diagnosis = {
            "root_cause": "divide() does not guard b == 0",
            "files": ["app.py"],
            "reasoning": "direct division",
            "NoRootCause": False,
        }
        return state


@pytest.mark.asyncio
async def test_minimal_graph_runs_diagnosis_node(monkeypatch) -> None:
    events: list[dict] = []

    async def fake_log_event(**kwargs):
        events.append(kwargs)
        return kwargs

    monkeypatch.setattr(graph_module, "log_event", fake_log_event)

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command
    from app.agents.planning import ApprovalDecision

    class CannotPlan:
        def run(self, state):
            state.status, state.failure_reason = "needs_human", "CannotPlan"
            return state

    class Decomposer:
        def run(self, state):
            state.subtask_specs = [{"spec_id": "1", "type": "bug", "description": state.description,
                                    "repo": state.repo, "depends_on": []}]
            state.decomposition_reasoning = "One clear bug fix."
            return state

    app = graph_module.build_graph(agent=_FakeAgent(), planner=CannotPlan(), decomposer=Decomposer(),
                                   memory_search=lambda *a, **k: [], checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "test-thread"}}
    await app.ainvoke(_state().model_dump(), config)
    # The Planner's decomposition pauses for confirmation before anything else runs.
    assert (await app.aget_state(config)).next == ("intent_gate",)
    result = await app.ainvoke(Command(resume=ApprovalDecision(approval_status="approved").model_dump()), config)

    assert result["diagnosis"]["root_cause"] == "divide() does not guard b == 0"
    assert [e["stage"] for e in events if e["agent"] == "diagnosis"] == ["started", "done"]
    assert result["failure_reason"] == "CannotPlan"
