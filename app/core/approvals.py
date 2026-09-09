"""Durable gate registry shared by UI and permission-checked Jira comments."""
import asyncio
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from app.db.connection import SessionLocal
from app.db.models import PendingApproval, Subtask
from app.core.failures import describe_failure
from app.events import log_event


def _register(state):
    with SessionLocal() as db:
        row = db.scalar(select(PendingApproval).where(PendingApproval.subtask_id == UUID(state.subtask_id)))
        if row is None:
            row = PendingApproval(ticket_id=UUID(state.ticket_id), subtask_id=UUID(state.subtask_id),
                jira_issue_key=state.jira_key, proposed_action={'kind': 'plan', **state.approval_payload})
            db.add(row)
            db.commit()
            db.refresh(row)
        return str(row.id), row.jira_comment_id


async def register_plan_gate(state, jira, message):
    if not state.jira_key:
        return
    gate_id, posted = await asyncio.to_thread(_register, state)
    if posted:
        return
    text = message + f'\nReply APPROVE {gate_id} or REJECT {gate_id} <note> to authorize this plan.'
    try:
        comment_id = await asyncio.to_thread(jira.comment, state.jira_key, text)
        def save():
            with SessionLocal() as db:
                row = db.get(PendingApproval, UUID(gate_id))
                row.jira_comment_id = str(comment_id or 'posted')
                db.commit()
        await asyncio.to_thread(save)
    except Exception as exc:
        await log_event(ticket_id=state.ticket_id, subtask_id=state.subtask_id, agent='jira', stage='warning',
                        message=describe_failure('Posting approval request', exc))


def resolve_plan_gate(state):
    with SessionLocal() as db:
        row = db.scalar(select(PendingApproval).where(PendingApproval.subtask_id == UUID(state.subtask_id)))
        if row:
            row.status = state.approval_status.upper()
            row.decision_note = state.approval_note
            row.resolved_at = datetime.now(timezone.utc)
            db.commit()


def pending_for_ticket(ticket_id):
    with SessionLocal() as db:
        rows = db.scalars(select(PendingApproval).where(PendingApproval.ticket_id == UUID(str(ticket_id)),
            PendingApproval.status == 'PENDING').order_by(PendingApproval.requested_at)).all()
        rows = [row for row in rows if not row.subtask_id or db.get(Subtask, row.subtask_id).status != 'superseded']
        return [{'id': str(row.id), 'subtask_id': str(row.subtask_id) if row.subtask_id else None,
                 'proposed_action': row.proposed_action} for row in rows]
