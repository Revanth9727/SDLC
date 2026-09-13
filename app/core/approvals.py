"""Durable gate registry shared by UI and permission-checked Jira comments."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from app.db.connection import SessionLocal
from app.db.models import PendingApproval, Subtask, Ticket
from app.config import settings
from app.core.failures import describe_failure
from app.events import log_event


def _expire_older_pending(db, ticket_id, keep_id=None):
    query = select(PendingApproval).where(
        PendingApproval.ticket_id == UUID(str(ticket_id)),
        PendingApproval.status == 'PENDING',
    )
    if keep_id is not None:
        keep = db.get(PendingApproval, keep_id)
        if keep is not None:
            query = query.where(PendingApproval.subtask_id == keep.subtask_id)
    rows = db.scalars(query).all()
    now = datetime.now(timezone.utc)
    expired = 0
    for row in rows:
        if keep_id is None or row.id != keep_id:
            row.status = 'EXPIRED'
            row.resolved_at = now
            expired += 1
    return expired


def cleanup_pending_gate_pile():
    """Keep one pending approval per subtask; sibling work gates may coexist."""
    with SessionLocal() as db:
        rows = db.scalars(select(PendingApproval).where(
            PendingApproval.status == 'PENDING',
        ).order_by(PendingApproval.ticket_id, PendingApproval.requested_at.desc(),
                   PendingApproval.id.desc())).all()
        newest = set()
        now = datetime.now(timezone.utc)
        expired = 0
        for row in rows:
            identity = (row.ticket_id, row.subtask_id)
            if identity in newest:
                row.status = 'EXPIRED'
                row.resolved_at = now
                expired += 1
            else:
                newest.add(identity)
        db.commit()
        return expired


def _register(state, kind='plan'):
    with SessionLocal() as db:
        proposed = {'kind': kind, **state.approval_payload}
        row = db.scalar(select(PendingApproval).where(PendingApproval.subtask_id == UUID(state.subtask_id)))
        if row is None:
            row = PendingApproval(ticket_id=UUID(state.ticket_id), subtask_id=UUID(state.subtask_id),
                jira_issue_key=state.jira_key, proposed_action=proposed)
            db.add(row)
            db.flush()
        elif row.proposed_action != proposed or row.status != 'PENDING':
            row.proposed_action = proposed
            row.status = 'PENDING'
            row.jira_comment_id = None
            row.decision_note = ''
            row.resolved_at = None
            row.reminded_at = None
        _expire_older_pending(db, state.ticket_id, row.id)
        db.commit()
        db.refresh(row)
        return str(row.id), row.jira_comment_id


async def register_plan_gate(state, jira, message, kind='plan'):
    if not state.jira_key:
        return
    gate_id, posted = await asyncio.to_thread(_register, state, kind)
    if posted:
        return
    text = message + '\nReply approve or reject, or describe a change you want.'
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
    cleanup_pending_gate_pile()
    with SessionLocal() as db:
        rows = db.scalars(select(PendingApproval).where(PendingApproval.ticket_id == UUID(str(ticket_id)),
            PendingApproval.status == 'PENDING').order_by(PendingApproval.requested_at.desc())).all()
        rows = [row for row in rows if not row.subtask_id or db.get(Subtask, row.subtask_id).status != 'superseded']
        return [{'id': str(row.id), 'subtask_id': str(row.subtask_id) if row.subtask_id else None,
                 'proposed_action': row.proposed_action} for row in rows]


async def process_gate_timeouts(jira, *, now: datetime | None = None) -> list[dict]:
    """Send one reminder, then expire and surface unanswered approval gates."""
    now = now or datetime.now(timezone.utc)
    reminder_before = now - timedelta(minutes=settings.human_gate_reminder_minutes)
    expiry_before = now - timedelta(minutes=settings.human_gate_expiry_minutes)
    actions: list[dict] = []
    with SessionLocal() as db:
        rows = list(db.scalars(select(PendingApproval).where(
            PendingApproval.status == 'PENDING').with_for_update()))
        for row in rows:
            if row.requested_at <= expiry_before:
                row.status, row.resolved_at = 'EXPIRED', now
                task = db.get(Subtask, row.subtask_id) if row.subtask_id else None
                reason = (f"Approval gate expired after {settings.human_gate_expiry_minutes} minute(s); "
                          "restart the sub-task to request fresh approval")
                if task:
                    state = {**(task.state or {}), 'status': 'needs_human', 'failure_reason': reason}
                    task.status, task.state = 'needs_human', state
                    ticket = db.get(Ticket, task.ticket_id)
                    if ticket:
                        ticket.status = 'needs_human'
                actions.append({'kind': 'expired', 'ticket_id': str(row.ticket_id),
                                'subtask_id': str(row.subtask_id) if row.subtask_id else None,
                                'jira_key': row.jira_issue_key, 'message': reason})
            elif row.reminded_at is None and row.requested_at <= reminder_before:
                row.reminded_at = now
                actions.append({'kind': 'reminder', 'ticket_id': str(row.ticket_id),
                                'subtask_id': str(row.subtask_id) if row.subtask_id else None,
                                'jira_key': row.jira_issue_key,
                                'message': 'Reminder: this sub-task is still waiting for approval.'})
        db.commit()
    for action in actions:
        try:
            if action['jira_key']:
                await asyncio.to_thread(jira.comment, action['jira_key'], action['message'])
        except Exception as exc:
            action['message'] += f" Jira notification failed: {describe_failure('Posting gate timeout', exc)}"
        await log_event(ticket_id=action['ticket_id'], subtask_id=action['subtask_id'],
                        agent='approval', stage=action['kind'], message=action['message'])
    return actions
