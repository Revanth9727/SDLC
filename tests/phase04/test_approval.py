import uuid

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.agents.planning import ApprovalDecision, Step
from app.agents.state import SubtaskState
from app.config import settings
from app.orchestrator import graph as module


class Diagnosis:
    calls = 0

    def run(self, state):
        self.calls += 1
        state.diagnosis = {"root_cause": "unguarded division", "files": ["app.py"], "reasoning": "direct a / b"}
        return state


class Planner:
    calls = 0

    def run(self, state):
        self.calls += 1
        state.plan = [Step(step_id="1", intent="Guard zero denominator", target_file="app.py")]
        state.plan_reasoning = "Return the error required by the ticket"
        return state


class Jira:
    def __init__(self):
        self.statuses, self.comments = [], []

    def set_status(self, key, stage):
        self.statuses.append(stage)
        return {"applied": True}

    def comment(self, key, message):
        self.comments.append(message)


@pytest.fixture
def setup(monkeypatch):
    events = []

    async def log_event(**kwargs):
        events.append(kwargs)

    monkeypatch.setattr(module, "log_event", log_event)
    from app.core import approvals, pr_sync
    async def register(state, jira, message):
        await module._jira_comment(state, message, jira)
    monkeypatch.setattr(approvals, 'register_plan_gate', register)
    monkeypatch.setattr(approvals, 'resolve_plan_gate', lambda state: None)
    monkeypatch.setattr(pr_sync, 'record_pr', lambda key, result: None)
    class Executor:
        async def run(self, state):
            state.current_step = len(state.plan)
            state.execution_complete = True
            return state
    class Publisher:
        def publish_changes(self, state):
            return {'url': 'https://github.com/owner/repo/pull/1'}
    monkeypatch.setattr(module, 'ExecutorAgent', Executor)
    monkeypatch.setattr(module, 'GitHubTool', Publisher)
    state = SubtaskState(ticket_id=str(uuid.uuid4()), subtask_id=str(uuid.uuid4()), jira_key="TEST-1",
                         subtask_type="bug", description="division by zero", repo="owner/repo")
    return state, Diagnosis(), Planner(), Jira(), events


def build(setup, saver):
    state, diagnosis, planner, jira, events = setup
    return module.build_graph(agent=diagnosis, planner=planner, jira=jira, checkpointer=saver, activity_check=lambda state: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_pause_rebuild_resume_and_idempotency(setup, decision):
    state, diagnosis, planner, jira, events = setup
    saver = MemorySaver()
    graph = build(setup, saver)
    config = module.thread_config(state.ticket_id, state.subtask_id)
    await graph.ainvoke(state.model_dump(), config)
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ("human_gate",)
    assert snapshot.values["approval_status"] == "pending"
    assert snapshot.values["plan"][0]["target_file"] == "app.py"
    assert "would_execute" not in [e["stage"] for e in events]
    assert jira.statuses == ["awaiting_approval"]
    assert sum("Needs approval" in c for c in jira.comments) == 1

    graph = build(setup, saver)
    answer = ApprovalDecision(approval_status=decision, note="Needs different behavior" if decision == "rejected" else "")
    result = await module.resume_approval(graph, state.ticket_id, state.subtask_id, answer)
    assert result.approval_status == decision
    assert result.execution_complete == (decision == "approved")
    assert result.status == ("in_review" if decision == "approved" else "needs_human")
    assert jira.statuses[-1] == ("in_review" if decision == "approved" else "blocked")
    if decision == "rejected":
        assert "Needs different behavior" in result.failure_reason
    event_count = len(events)
    await module.resume_approval(graph, state.ticket_id, state.subtask_id, answer)
    assert len(events) == event_count
    assert diagnosis.calls == planner.calls == 1
    assert sum("Needs approval" in c for c in jira.comments) == 1
    assert not (await graph.aget_state(config)).next
    with pytest.raises(module.ApprovalConflict):
        await module.resume_approval(graph, state.ticket_id, state.subtask_id,
                                     ApprovalDecision(approval_status="rejected" if decision == "approved" else "approved", note="other"))
    with pytest.raises(module.ApprovalConflict):
        await module.resume_approval(graph, "wrong-ticket", state.subtask_id, answer)


@pytest.mark.asyncio
async def test_failed_diagnosis_skips_planning(setup):
    state, diagnosis, planner, jira, events = setup
    def fail(state):
        state.status, state.failure_reason = "needs_human", "NoRootCause"
        return state
    diagnosis.run = fail
    graph = build(setup, MemorySaver())
    result = await graph.ainvoke(state.model_dump(), module.thread_config(state.ticket_id, state.subtask_id))
    assert result["status"] == "needs_human"
    assert planner.calls == 0
    assert jira.statuses == ["blocked"]


@pytest.mark.asyncio
async def test_diagnosis_exception_is_preserved_in_failure_reason(setup):
    state, diagnosis, planner, jira, events = setup

    def fail(_state):
        raise PermissionError("cannot read app.py")

    diagnosis.run = fail
    graph = build(setup, MemorySaver())
    result = await graph.ainvoke(
        state.model_dump(),
        module.thread_config(state.ticket_id, state.subtask_id),
    )
    assert result["status"] == "needs_human"
    assert "Running diagnosis failed" in result["failure_reason"]
    assert "PermissionError" in result["failure_reason"]
    assert "cannot read app.py" in result["failure_reason"]


@pytest.mark.asyncio
async def test_jira_status_and_comment_errors_still_pause(setup):
    state, diagnosis, planner, jira, events = setup
    def fail(*args):
        raise RuntimeError("offline")
    jira.set_status = jira.comment = fail
    graph = build(setup, MemorySaver())
    config = module.thread_config(state.ticket_id, state.subtask_id)
    await graph.ainvoke(state.model_dump(), config)
    assert (await graph.aget_state(config)).next == ("human_gate",)
    assert any(e["stage"] == "warning" for e in events)


@pytest.mark.asyncio
async def test_postgres_pause_survives_closed_connection(setup):
    """Real Postgres integration; no Jira/OpenAI calls or ticket-table changes."""
    state, diagnosis, planner, jira, events = setup
    config = module.thread_config(state.ticket_id, state.subtask_id)
    async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
        await saver.setup()
        graph = build(setup, saver)
        await graph.ainvoke(state.model_dump(), config)
    # A fresh Python process can recover the pause using only Postgres + IDs.
    import asyncio
    import subprocess
    import sys
    probe = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", """
import asyncio, sys
from app.orchestrator.graph import open_graph, thread_config
async def main():
    async with open_graph(sys.argv[1], sys.argv[2]) as graph:
        snapshot = await graph.aget_state(thread_config(sys.argv[1], sys.argv[2]))
        assert snapshot.next == ('human_gate',)
        assert snapshot.values['approval_status'] == 'pending'
        assert snapshot.values['plan'][0]['target_file'] == 'app.py'
asyncio.run(main())
""", state.ticket_id, state.subtask_id], capture_output=True, text=True, timeout=20)
    assert probe.returncode == 0, probe.stderr
    async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
        graph = build(setup, saver)
        assert (await graph.aget_state(config)).next == ("human_gate",)
        result = await module.resume_approval(graph, state.ticket_id, state.subtask_id,
                                             ApprovalDecision(approval_status="approved"))
        assert result.execution_complete
        assert diagnosis.calls == planner.calls == 1
        await saver.adelete_thread(config["configurable"]["thread_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_http_endpoints_with_postgres_checkpoint_and_row(setup, monkeypatch, action):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.db.connection import SessionLocal
    from app.db.models import Ticket, Subtask
    from app.web import approval

    state, diagnosis, planner, jira, events = setup
    original_build = module.build_graph
    monkeypatch.setattr(module, "build_graph", lambda **kwargs: original_build(
        agent=diagnosis, planner=planner, jira=jira, activity_check=lambda state: None, **kwargs))

    async def log_event(**kwargs):
        events.append(kwargs)
    monkeypatch.setattr(approval, "log_event", log_event)
    monkeypatch.setattr(approval, "resolve_plan_gate", lambda state: None)
    with SessionLocal() as db:
        db.add(Ticket(id=uuid.UUID(state.ticket_id), source="manual", title="Approval test", description="test"))
        db.flush()
        db.add(Subtask(id=uuid.UUID(state.subtask_id), ticket_id=uuid.UUID(state.ticket_id),
                       type="bug", description="test", status="running"))
        db.commit()
    app = FastAPI()
    app.include_router(approval.router)
    url = f"/tickets/{state.ticket_id}/subtasks/{state.subtask_id}"
    try:
        async with module.open_graph(state.ticket_id, state.subtask_id) as graph:
            await graph.ainvoke(state.model_dump(), module.thread_config(state.ticket_id, state.subtask_id))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # UI reads checkpoints even if the DB mirror was not saved before a crash.
            response = await client.get(f"/tickets/{state.ticket_id}/subtasks")
            assert response.status_code == 200
            assert response.json()[0]["waiting"]
            assert response.json()[0]["state"]["plan"][0]["target_file"] == "app.py"
            assert (await client.post(url + "/reject", json={"note": "  "})).status_code == 422
            assert (await client.post(url + "/approve", json={"plan": []})).status_code == 422
            assert (await client.post(f"/tickets/{uuid.uuid4()}/subtasks/{state.subtask_id}/approve")).status_code == 404
            async with module.open_graph(state.ticket_id, state.subtask_id, lock=True):
                assert (await client.post(url + "/approve")).status_code == 409
            body = {"note": "Use a different error"} if action == "reject" else {}
            response = await client.post(url + "/" + action, json=body)
            assert response.status_code == 200
            assert response.json()["approval_status"] == ("approved" if action == "approve" else "rejected")
            assert (await client.post(url + "/" + action, json=body)).status_code == 200
            saved = (await client.get(f"/tickets/{state.ticket_id}/subtasks")).json()[0]
            assert not saved["waiting"]
            with SessionLocal() as db:
                row = db.get(Subtask, uuid.UUID(state.subtask_id))
                assert row.state["approval_status"] == response.json()["approval_status"]
                ticket = db.get(Ticket, uuid.UUID(state.ticket_id))
                assert ticket.status == ("in_review" if action == "approve" else "needs_human")
            assert diagnosis.calls == planner.calls == 1
    finally:
        async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
            await saver.adelete_thread(module.thread_config(state.ticket_id, state.subtask_id)["configurable"]["thread_id"])
        with SessionLocal() as db:
            db.delete(db.get(Ticket, uuid.UUID(state.ticket_id)))
            db.commit()
