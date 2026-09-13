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


class Decomposer:
    calls = 0

    def run(self, state):
        self.calls += 1
        state.subtask_specs = [{"spec_id": "1", "type": state.subtask_type, "description": state.description,
                                "repo": state.repo, "depends_on": []}]
        state.decomposition_reasoning = "One clear bug fix."
        return state


async def confirm_intent(graph, ticket_id, subtask_id):
    """Resume past the Planner's mandatory intent-confirmation pause, carrying
    the flow to wherever it next stops (human_gate, or an earlier failure)."""
    return await module.resume_approval(graph, ticket_id, subtask_id,
                                        ApprovalDecision(approval_status="approved"))


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
    async def register(state, jira, message, kind='plan'):
        await module._jira_comment(state, message, jira)
    monkeypatch.setattr(approvals, 'register_plan_gate', register)
    monkeypatch.setattr(approvals, 'resolve_plan_gate', lambda state: None)
    monkeypatch.setattr(pr_sync, 'record_pr', lambda key, result: None)
    class Executor:
        def __init__(self, **kwargs):
            pass

        async def run(self, state):
            state.current_step = len(state.plan)
            state.execution_complete = True
            return state
    class Publisher:
        def publish_changes(self, state):
            return {'url': 'https://github.com/owner/repo/pull/1'}
    class ApprovingCritic:
        def __init__(self, **kwargs):
            pass

        def run(self, state):
            state.critic_verdict = {'approved': True, 'issues': [], 'verifiability': 'ok'}
            return state
    monkeypatch.setattr(module, 'ExecutorAgent', Executor)
    monkeypatch.setattr(module, 'GitHubTool', Publisher)
    monkeypatch.setattr(module, 'CriticAgent', ApprovingCritic)
    state = SubtaskState(ticket_id=str(uuid.uuid4()), subtask_id=str(uuid.uuid4()), jira_key="TEST-1",
                         subtask_type="bug", description="division by zero", repo="owner/repo",
                         confirmed_repos=["owner/repo"])
    return state, Diagnosis(), Planner(), Decomposer(), Jira(), events


def build(setup, saver):
    state, diagnosis, planner, decomposer, jira, events = setup
    return module.build_graph(agent=diagnosis, planner=planner, decomposer=decomposer, jira=jira,
                              memory_search=lambda *a, **k: [], checkpointer=saver, activity_check=lambda state: None)


@pytest.mark.asyncio
async def test_pause_rebuild_resume_and_idempotency(setup):
    state, diagnosis, planner, decomposer, jira, events = setup
    saver = MemorySaver()
    graph = build(setup, saver)
    config = module.thread_config(state.ticket_id, state.subtask_id)
    await graph.ainvoke(state.model_dump(), config)
    await confirm_intent(graph, state.ticket_id, state.subtask_id)
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ("human_gate",)
    assert snapshot.values["approval_status"] == "pending"
    assert snapshot.values["plan"][0]["target_file"] == "app.py"
    assert "would_execute" not in [e["stage"] for e in events]
    # intent gate's own awaiting_approval -> confirmed (in_progress) -> plan gate's awaiting_approval.
    assert jira.statuses == ["awaiting_approval", "in_progress", "awaiting_approval"]
    assert sum("Needs approval" in c for c in jira.comments) == 1

    graph = build(setup, saver)
    answer = ApprovalDecision(approval_status="approved")
    result = await module.resume_approval(graph, state.ticket_id, state.subtask_id, answer)
    assert result.approval_status == "approved"
    assert result.execution_complete
    assert result.status == "in_review"
    assert jira.statuses[-1] == "in_review"
    event_count = len(events)
    await module.resume_approval(graph, state.ticket_id, state.subtask_id, answer)
    assert len(events) == event_count
    assert diagnosis.calls == planner.calls == 1
    assert sum("Needs approval" in c for c in jira.comments) == 1
    assert not (await graph.aget_state(config)).next
    with pytest.raises(module.ApprovalConflict):
        await module.resume_approval(graph, state.ticket_id, state.subtask_id,
                                     ApprovalDecision(approval_status="rejected", note="other"))
    with pytest.raises(module.ApprovalConflict):
        await module.resume_approval(graph, "wrong-ticket", state.subtask_id, answer)


@pytest.mark.asyncio
async def test_rejection_feedback_replans_and_returns_to_gate(setup):
    state, diagnosis, planner, decomposer, jira, events = setup
    graph = build(setup, MemorySaver())
    config = module.thread_config(state.ticket_id, state.subtask_id)
    await graph.ainvoke(state.model_dump(), config)
    await confirm_intent(graph, state.ticket_id, state.subtask_id)
    feedback = 'Also handle None with a clear ValueError'
    answer = ApprovalDecision(approval_status='rejected', note=feedback)
    result = await module.resume_approval(graph, state.ticket_id, state.subtask_id, answer)
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ('human_gate',)
    assert result.approval_status == 'pending'
    assert result.replan_count == 1 and result.last_rejection_note == feedback
    assert result.status == 'running' and result.failure_reason is None
    assert planner.calls == 2
    assert sum('Needs approval' in text for text in jira.comments) == 2
    event_count = len(events)
    duplicate = await module.resume_approval(graph, state.ticket_id, state.subtask_id, answer)
    assert duplicate.replan_count == 1 and len(events) == event_count


@pytest.mark.asyncio
async def test_failed_diagnosis_skips_planning(setup):
    state, diagnosis, planner, decomposer, jira, events = setup
    def fail(state):
        state.status, state.failure_reason = "needs_human", "NoRootCause"
        return state
    diagnosis.run = fail
    graph = build(setup, MemorySaver())
    config = module.thread_config(state.ticket_id, state.subtask_id)
    await graph.ainvoke(state.model_dump(), config)
    result = await confirm_intent(graph, state.ticket_id, state.subtask_id)
    assert result.status == "needs_human"
    assert planner.calls == 0
    assert jira.statuses == ["awaiting_approval", "in_progress", "blocked"]


@pytest.mark.asyncio
async def test_diagnosis_exception_is_preserved_in_failure_reason(setup):
    state, diagnosis, planner, decomposer, jira, events = setup

    def fail(_state):
        raise PermissionError("cannot read app.py")

    diagnosis.run = fail
    graph = build(setup, MemorySaver())
    config = module.thread_config(state.ticket_id, state.subtask_id)
    await graph.ainvoke(state.model_dump(), config)
    result = await confirm_intent(graph, state.ticket_id, state.subtask_id)
    assert result.status == "needs_human"
    assert "Running diagnosis failed" in result.failure_reason
    assert "PermissionError" in result.failure_reason
    assert "cannot read app.py" in result.failure_reason


@pytest.mark.asyncio
async def test_jira_status_and_comment_errors_still_pause(setup):
    state, diagnosis, planner, decomposer, jira, events = setup
    def fail(*args):
        raise RuntimeError("offline")
    jira.set_status = jira.comment = fail
    graph = build(setup, MemorySaver())
    config = module.thread_config(state.ticket_id, state.subtask_id)
    await graph.ainvoke(state.model_dump(), config)
    await confirm_intent(graph, state.ticket_id, state.subtask_id)
    assert (await graph.aget_state(config)).next == ("human_gate",)
    assert any(e["stage"] == "warning" for e in events)


@pytest.mark.asyncio
async def test_postgres_pause_survives_closed_connection(setup):
    """Real Postgres integration; no Jira/OpenAI calls or ticket-table changes."""
    state, diagnosis, planner, decomposer, jira, events = setup
    config = module.thread_config(state.ticket_id, state.subtask_id)
    async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
        await saver.setup()
        graph = build(setup, saver)
        await graph.ainvoke(state.model_dump(), config)
        await confirm_intent(graph, state.ticket_id, state.subtask_id)
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

    state, diagnosis, planner, decomposer, jira, events = setup
    original_build = module.build_graph
    monkeypatch.setattr(module, "build_graph", lambda **kwargs: original_build(
        agent=diagnosis, planner=planner, decomposer=decomposer, memory_search=lambda *a, **k: [], jira=jira,
        activity_check=lambda state: None, **kwargs))

    async def log_event(**kwargs):
        events.append(kwargs)
    monkeypatch.setattr(approval, "log_event", log_event)
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
            await confirm_intent(graph, state.ticket_id, state.subtask_id)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # UI reads checkpoints even if the DB mirror was not saved before a crash.
            response = await client.get(f"/tickets/{state.ticket_id}/subtasks")
            assert response.status_code == 200
            assert response.json()[0]["waiting"]
            assert response.json()[0]["state"]["plan"][0]["target_file"] == "app.py"
            blank = await client.post(url + "/reject", json={"note": "  "})
            assert blank.status_code == 200
            assert blank.json()['approval_status'] == 'pending'
            assert 'What should change?' in blank.json()['message']
            assert (await client.post(url + "/approve", json={"plan": []})).status_code == 422
            assert (await client.post(f"/tickets/{uuid.uuid4()}/subtasks/{state.subtask_id}/approve")).status_code == 404
            async with module.open_graph(state.ticket_id, state.subtask_id, lock=True):
                assert (await client.post(url + "/approve")).status_code == 409
            body = {"note": "Use a different error"} if action == "reject" else {}
            response = await client.post(url + "/" + action, json=body)
            assert response.status_code == 200
            assert response.json()["approval_status"] == ("approved" if action == "approve" else "pending")
            assert (await client.post(url + "/" + action, json=body)).status_code == 200
            saved = (await client.get(f"/tickets/{state.ticket_id}/subtasks")).json()[0]
            assert saved["waiting"] == (action == 'reject')
            with SessionLocal() as db:
                row = db.get(Subtask, uuid.UUID(state.subtask_id))
                assert row.state["approval_status"] == response.json()["approval_status"]
                ticket = db.get(Ticket, uuid.UUID(state.ticket_id))
                assert ticket.status == ("in_review" if action == "approve" else "awaiting_approval")
            assert diagnosis.calls == 1
            assert planner.calls == (1 if action == 'approve' else 2)
    finally:
        async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
            await saver.adelete_thread(module.thread_config(state.ticket_id, state.subtask_id)["configurable"]["thread_id"])
        with SessionLocal() as db:
            db.delete(db.get(Ticket, uuid.UUID(state.ticket_id)))
            db.commit()
