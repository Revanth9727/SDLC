"""Meaningful step narration for Jira + UI audit trail (R-40/R-41)."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.db.models import Ticket
from app.events import log_event
from app.tools.jira_tool import JiraTool

logger = logging.getLogger(__name__)


async def post_step(
    ticket: Ticket | dict[str, Any],
    message: str,
    *,
    stage: str = "step",
    emit: Callable[[dict], Awaitable[None]] | None = None,
) -> dict:
    """Post one meaningful ticket-level step to Jira and the UI event stream."""
    ticket_id = str(_ticket_value(ticket, "id"))
    external_key = _ticket_value(ticket, "external_key")

    if external_key:
        try:
            JiraTool().comment(external_key, message)
        except Exception as exc:
            logger.warning(
                "post_step: Jira comment failed key=%r stage=%r: %s",
                external_key,
                stage,
                exc,
            )

    event = await log_event(
        ticket_id=ticket_id,
        agent="step",
        stage=stage,
        message=message,
        key=external_key,
    )
    if emit is not None:
        await emit(event)
    return event


def _ticket_value(ticket: Ticket | dict[str, Any], name: str) -> Any:
    if isinstance(ticket, dict):
        return ticket.get(name)
    return getattr(ticket, name)
