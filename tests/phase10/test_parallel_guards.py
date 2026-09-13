"""Parallel orchestration and remaining deterministic guard edge cases."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select, update

from app.agents.planning import Step
from app.agents.state import SubtaskState
from app.config import settings
from app.core import budget
from app.core.approvals import _register, process_gate_timeouts
from app.core.freshness import check_freshness
from app.core.guard import check
from app.db.connection import SessionLocal
from app.db.init_db import ensure_guard_schema
from app.db.models import PendingApproval, Subtask, Ticket, TicketBudget, TicketEvent


@pytest.fixture(autouse=True)
def schema():
    ensure_guard_schema()


def _state(ticket_id=None, subtask_id=None):
    return SubtaskState(
        ticket_id=str(ticket_id or uuid.uuid4()), subtask_id=str(subtask_id or uuid.uuid4()),
        jira_key='PAR-1', subtask_type='bug', description='Change calculate_total', repo='org/orders',
        plan=[Step(step_id='1', intent='Fix total', target_file='orders.py')],
        diagnosed_file_hashes={'orders.py': 'old-hash'}, freshness_recorded=True,
        base_commit='old-commit', approval_status='approved',
    )


def test_freshness_drift_escalates_before_executor():
    repo = SimpleNamespace(verify_freshness=Mock(return_value=('new-commit', ['orders.py'])))
    state = check_freshness(_state(), repo)
    assert state.status == 'needs_human'
    assert 'orders.py' in state.failure_reason
    assert state.current_step == 0


def test_freshness_unaffected_targets_advance_snapshot():
    repo = SimpleNamespace(verify_freshness=Mock(return_value=('new-commit', [])))
    state = check_freshness(_state(), repo)
    assert state.status == 'running'
    assert state.base_commit == 'new-commit'


def test_global_ticket_wall_time_budget_stops_guard(monkeypatch):
    ticket_id, subtask_id = uuid.uuid4(), uuid.uuid4()
    state = _state(ticket_id, subtask_id)
    with SessionLocal() as db:
        db.add(Ticket(id=ticket_id, source='jira', title='Timed', description='Timed', status='processing'))
        db.flush()
        db.add(Subtask(id=subtask_id, ticket_id=ticket_id, type='bug', description='Timed',
                       status='running', state=state.model_dump(mode='json')))
        db.commit()
    try:
        budget.reserve(str(ticket_id))
        with SessionLocal() as db:
            db.execute(update(TicketBudget).where(TicketBudget.ticket_id == ticket_id).values(
                started_at=datetime.now(timezone.utc) - timedelta(seconds=5)))
            db.commit()
        monkeypatch.setattr(settings, 'ticket_time_budget_seconds', 1)
        guarded = check(state)
        assert guarded.status == 'needs_human'
        assert 'Budget reached' in guarded.failure_reason
        assert guarded.budget_used.elapsed_seconds >= 1
    finally:
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            if ticket:
                db.delete(ticket)
                db.commit()


@pytest.mark.asyncio
async def test_gate_reminds_once_then_expires(monkeypatch):
    ticket_id, subtask_id, gate_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    state = _state(ticket_id, subtask_id)
    state.approval_payload = {'plan': []}
    with SessionLocal() as db:
        db.add(Ticket(id=ticket_id, source='jira', external_key='TIME-1', title='Gate',
                      description='Gate', status='awaiting_approval'))
        db.flush()
        db.add(Subtask(id=subtask_id, ticket_id=ticket_id, type='bug', description='Gate',
                       status='running', state=state.model_dump(mode='json')))
        db.flush()
        db.add(PendingApproval(id=gate_id, ticket_id=ticket_id, subtask_id=subtask_id,
                               jira_issue_key='TIME-1', proposed_action={'kind': 'plan'},
                               requested_at=now - timedelta(minutes=2)))
        db.commit()
    jira = SimpleNamespace(comment=Mock(return_value='comment'))
    monkeypatch.setattr(settings, 'human_gate_reminder_minutes', 1)
    monkeypatch.setattr(settings, 'human_gate_expiry_minutes', 10)
    try:
        first = await process_gate_timeouts(jira, now=now)
        second = await process_gate_timeouts(jira, now=now + timedelta(minutes=1))
        expired = await process_gate_timeouts(jira, now=now + timedelta(minutes=20))
        assert [item['kind'] for item in first] == ['reminder']
        assert second == []
        assert [item['kind'] for item in expired] == ['expired']
        assert jira.comment.call_count == 2
        with SessionLocal() as db:
            assert db.get(PendingApproval, gate_id).status == 'EXPIRED'
            assert db.get(Subtask, subtask_id).status == 'needs_human'
            stages = list(db.scalars(select(TicketEvent.stage).where(TicketEvent.ticket_id == ticket_id)))
            assert stages == ['reminder', 'expired']
    finally:
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            if ticket:
                db.delete(ticket)
                db.commit()


def test_independent_subtasks_can_hold_separate_pending_gates():
    ticket_id = uuid.uuid4()
    states = [_state(ticket_id, uuid.uuid4()), _state(ticket_id, uuid.uuid4())]
    with SessionLocal() as db:
        db.add(Ticket(id=ticket_id, source='jira', external_key='PAR-1', title='Parallel',
                      description='Parallel', status='awaiting_approval'))
        db.flush()
        for state in states:
            state.approval_payload = {'plan': [], 'spec_id': state.subtask_id}
            db.add(Subtask(id=uuid.UUID(state.subtask_id), ticket_id=ticket_id, type='bug',
                           description=state.description, status='running',
                           state=state.model_dump(mode='json')))
        db.commit()
    try:
        for state in states:
            _register(state)
        with SessionLocal() as db:
            gates = list(db.scalars(select(PendingApproval).where(
                PendingApproval.ticket_id == ticket_id, PendingApproval.status == 'PENDING')))
            assert {str(gate.subtask_id) for gate in gates} == {state.subtask_id for state in states}
    finally:
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            if ticket:
                db.delete(ticket)
                db.commit()
