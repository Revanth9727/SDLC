"""Check cancellation at each execution/publication boundary."""
from uuid import UUID
from sqlalchemy import select
from app.db.connection import SessionLocal
from app.db.models import Subtask, Ticket


def require_active(state):
    with SessionLocal() as db:
        task = db.scalar(select(Subtask).where(Subtask.id == UUID(state.subtask_id),
                                               Subtask.ticket_id == UUID(state.ticket_id)))
        ticket = db.get(Ticket, UUID(state.ticket_id))
        if not task or task.status not in {'pending', 'waiting', 'running'}:
            raise ValueError('This attempt was stopped or superseded; no further edits or PRs are allowed')
        if ticket.status in {'done', 'superseded', 'needs_human'}:
            raise ValueError('The ticket is no longer active; restart through a new approval')
