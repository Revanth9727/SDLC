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
        # A running subtask is the authority for an in-place retry. The ticket's
        # aggregate may temporarily be needs_human because of a sibling or a
        # previous node; that must not cancel this active checkpoint mid-loop.
        if not ticket or ticket.status in {'done', 'superseded'}:
            raise ValueError('The ticket is no longer active; restart through a new approval')
