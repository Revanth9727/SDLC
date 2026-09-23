import uuid

import pytest

from app.agents.state import SubtaskState
from app.db.connection import SessionLocal
from app.db.models import Subtask, Ticket
from app.orchestrator.scheduler import advance_ticket, materialize_subtasks


@pytest.fixture
def coordinator():
    ticket_id, coordinator_id = uuid.uuid4(), uuid.uuid4()
    state = SubtaskState(
        ticket_id=str(ticket_id), subtask_id=str(coordinator_id), jira_key="MULTI-1",
        subtask_type="bug", description="Three independent requests", repo="org/api",
        confirmed_repos=["org/api", "org/web"], orchestration_role="coordinator",
        approval_status="approved",
        subtask_specs=[
            {"spec_id": "1", "type": "bug", "description": "Fix tax", "repo": "org/api", "depends_on": []},
            {"spec_id": "2", "type": "feature", "description": "Expose total", "repo": "org/web", "depends_on": ["1"]},
            {"spec_id": "3", "type": "bug", "description": "Fix discount", "repo": "org/api", "depends_on": []},
        ],
    )
    with SessionLocal() as db:
        db.add(Ticket(id=ticket_id, source="jira", external_key="MULTI-1", title="Multi",
                      description="Three", status="processing"))
        db.add(Subtask(id=coordinator_id, ticket_id=ticket_id, type="bug", description=state.description,
                       status="running", state=state.model_dump(mode="json")))
        db.commit()
    yield state
    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        if ticket:
            db.delete(ticket)
            db.commit()


def test_materializes_isolated_states_and_uuid_dependencies(coordinator):
    coordinator.prior_attempt = {"failure_reason": "prior mistake"}
    states = materialize_subtasks(coordinator)
    assert [state.spec_id for state in states] == ["1", "2", "3"]
    assert [state.description for state in states] == ["Fix tax", "Expose total", "Fix discount"]
    assert states[0].subtask_specs == [] and states[1].subtask_specs == []
    assert states[0].confirmed_repos == ["org/api"]
    assert states[1].confirmed_repos == ["org/web"]
    assert states[1].depends_on == [states[0].subtask_id]
    assert all(state.prior_attempt == {"failure_reason": "prior mistake"} for state in states)
    states[0].prior_attempt["failure_reason"] = "locally considered"
    assert states[1].prior_attempt["failure_reason"] == "prior mistake"
    assert all(state.diagnosis is None and state.plan == [] and state.current_step == 0 for state in states)
    with SessionLocal() as db:
        assert db.get(Subtask, uuid.UUID(coordinator.subtask_id)).status == "done"
        rows = db.query(Subtask).filter(Subtask.ticket_id == uuid.UUID(coordinator.ticket_id),
                                        Subtask.id != uuid.UUID(coordinator.subtask_id)).all()
        assert all(row.status == "queued" for row in rows)


@pytest.mark.asyncio
async def test_runs_parallel_ready_wave_then_dependency_order(coordinator):
    materialize_subtasks(coordinator)
    seen = []
    first_wave_ready = __import__('asyncio').Event()

    async def runner(state):
        seen.append((state.spec_id, tuple(state.depends_on), state.description, tuple(state.confirmed_repos)))
        if state.spec_id in {'1', '3'}:
            if {item[0] for item in seen} >= {'1', '3'}:
                first_wave_ready.set()
            await __import__('asyncio').wait_for(first_wave_ready.wait(), timeout=1)
        state.status = "in_review"
        state.pr_url = f"https://github.com/{state.repo}/pull/{state.spec_id}"
        return state

    class Repo:
        def clone_or_pull(self, repo, subtask_id):
            return "/tmp/fake"

    await advance_ticket(coordinator.ticket_id, runner=runner, repo_tool=Repo())
    assert [item[0] for item in seen] == ["1", "3", "2"]
    assert all(len(item[3]) == 1 for item in seen)
    with SessionLocal() as db:
        work = [row for row in db.query(Subtask).filter(Subtask.ticket_id == uuid.UUID(coordinator.ticket_id)).all()
                if (row.state or {}).get("orchestration_role") == "work"]
        assert all(row.status == "in_review" for row in work)
        assert db.get(Ticket, uuid.UUID(coordinator.ticket_id)).status == "in_review"


@pytest.mark.asyncio
async def test_partial_failure_continues_independent_work_and_marks_mixed(coordinator):
    coordinator.subtask_specs[1]["depends_on"] = []
    materialize_subtasks(coordinator)
    seen = []

    async def runner(state):
        seen.append(state.spec_id)
        if state.spec_id == "2":
            state.status, state.failure_reason = "needs_human", "Deliberate test escalation"
        else:
            state.status = "in_review"
            state.pr_url = f"https://github.com/{state.repo}/pull/{state.spec_id}"
        return state

    class Repo:
        def clone_or_pull(self, repo, subtask_id):
            return "/tmp/fake"

    await advance_ticket(coordinator.ticket_id, runner=runner, repo_tool=Repo())
    assert seen == ["1", "2", "3"]
    with SessionLocal() as db:
        work = [row for row in db.query(Subtask).filter(Subtask.ticket_id == uuid.UUID(coordinator.ticket_id)).all()
                if (row.state or {}).get("orchestration_role") == "work"]
        assert [row.status for row in work].count("in_review") == 2
        assert [row.status for row in work].count("needs_human") == 1
        assert db.get(Ticket, uuid.UUID(coordinator.ticket_id)).status == "mixed"


def test_rejects_cyclic_dependency_graph(coordinator):
    coordinator.subtask_specs[0]["depends_on"] = ["2"]
    coordinator.subtask_specs[1]["depends_on"] = ["1"]
    with pytest.raises(ValueError, match="cyclic"):
        materialize_subtasks(coordinator)
