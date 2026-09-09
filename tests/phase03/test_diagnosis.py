from types import SimpleNamespace

import pytest

from app.agents.diagnosis import Diagnosis, DiagnosisAgent
from app.agents.state import SubtaskState
from app.orchestrator import graph as graph_module


class _FakeRepoTool:
    def __init__(self) -> None:
        self.reads: list[str] = []

    def list_files(self, repo: str, subtask_id: str) -> list[str]:
        return ["README.md", "app.py", "tests/test_app.py", "image.png"]

    def read_file(self, repo: str, subtask_id: str, path: str) -> str:
        self.reads.append(path)
        return "def divide(a, b):\n    return a / b\n"


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


def _state() -> SubtaskState:
    return SubtaskState(
        ticket_id="11111111-1111-1111-1111-111111111111",
        subtask_id="22222222-2222-2222-2222-222222222222",
        subtask_type="bug",
        description="The app crashes with divide by zero in app.py",
        repo="owner/repo",
    )


def test_diagnosis_agent_writes_successful_diagnosis() -> None:
    llm = _FakeLLM(
        Diagnosis(
            root_cause="divide() does not guard b == 0",
            files=["app.py"],
            reasoning="The code returns a / b directly.",
        )
    )
    agent = DiagnosisAgent(llm=llm, repo_tool=_FakeRepoTool())

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
    )

    result = agent.run(_state())

    assert result.status == "needs_human"
    assert result.failure_reason == "NoRootCause"


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

    monkeypatch.setattr(graph_module, "_checkpointer", lambda: False)
    monkeypatch.setattr(graph_module, "log_event", fake_log_event)

    app = graph_module.build_graph(agent=_FakeAgent())
    result = await app.ainvoke(
        _state().model_dump(),
        config={"configurable": {"thread_id": "test-thread"}},
    )

    assert result["diagnosis"]["root_cause"] == "divide() does not guard b == 0"
    assert [event["stage"] for event in events] == ["started", "done"]
