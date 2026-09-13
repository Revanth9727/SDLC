"""Publish synchronous Jira results onto the application's SSE event loop."""
import asyncio
import json
import logging

from app import events
from app.db.connection import SessionLocal
from app.db.models import Ticket, TicketEvent

loop: asyncio.AbstractEventLoop | None = None
logger = logging.getLogger(__name__)


def emit_transition(key: str, result: dict) -> None:
    try:
        message = json.dumps(result, ensure_ascii=False)
        with SessionLocal() as db:
            ticket = db.query(Ticket).filter_by(external_key=key).first()
            if ticket is None:
                return
            ticket_id = str(ticket.id)
            db.add(TicketEvent(ticket_id=ticket.id, agent='jira', stage='status_transition', message=message))
            db.commit()
        if loop and loop.is_running():
            event = events.make_event('jira', 'status_transition', message, ticket_id=ticket_id, **result)
            asyncio.run_coroutine_threadsafe(events.publish(ticket_id, event), loop)
    except Exception:
        logger.warning('Unable to record Jira transition event key=%r', key, exc_info=True)


def emit_ticket_event(ticket_id: str, agent: str, stage: str, payload: dict) -> None:
    """Durable accounting event from a synchronous model call."""
    from uuid import UUID
    message = json.dumps(payload)
    with SessionLocal() as db:
        db.add(TicketEvent(ticket_id=UUID(ticket_id), agent=agent, stage=stage, message=message))
        db.commit()
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(events.publish(ticket_id,
            events.make_event(agent, stage, message, ticket_id=ticket_id, **payload)), loop)
