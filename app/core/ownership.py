"""Ownership detection for reconciliation and stuck detection (R-28).

Deterministic rule:
  1. Active AI run in local DB -> "ai"
  2. Otherwise Jira active/working category -> "human"
  3. Otherwise -> "none"

This phase has no separate run/checkpoint table yet, so the active local AI-run
signal is a ticket claim (`claimed_at`) that has not been released or superseded.
"""

import logging
from typing import Literal

from app.db.connection import SessionLocal
from app.db.models import Ticket
from app.tools.jira_tool import JiraTool

Owner = Literal["ai", "human", "none"]

logger = logging.getLogger(__name__)

_INACTIVE_LOCAL_STATUSES = {
    "done",
    "failed",
    "human_owned",
    "needs_human",
    "superseded",
}


def has_active_ai_run(ticket: Ticket | None) -> bool:
    """Return True if the local DB says the AI currently owns this ticket."""
    if ticket is None or ticket.claimed_at is None:
        return False
    return ticket.status not in _INACTIVE_LOCAL_STATUSES


def owner_of(ticket: Ticket, jira: JiraTool | None = None) -> Owner:
    """Classify ownership for a local ticket row."""
    if has_active_ai_run(ticket):
        logger.info("ownership.owner_of key=%r owner='ai'", ticket.external_key)
        return "ai"

    if not ticket.external_key:
        logger.info("ownership.owner_of ticket=%s owner='none' reason='manual/no external_key'", ticket.id)
        return "none"

    jira_tool = jira or JiraTool()
    detail = jira_tool.get_issue_detail(ticket.external_key)
    owner = _owner_from_jira_category(detail.get("status_category"))
    logger.info(
        "ownership.owner_of key=%r jira_status=%r jira_category=%r owner=%r",
        ticket.external_key,
        detail["status"],
        detail.get("status_category"),
        owner,
    )
    return owner


def owner_of_key(key: str, jira: JiraTool | None = None) -> dict:
    """Classify ownership by Jira key, including tickets not yet in the local DB."""
    jira_tool = jira or JiraTool()
    with SessionLocal() as db:
        ticket = db.query(Ticket).filter(Ticket.external_key == key).first()

    if ticket is not None and has_active_ai_run(ticket):
        result = {
            "key": key,
            "owner": "ai",
            "jira_status": None,
            "local_ticket_id": str(ticket.id),
            "claimed_at": ticket.claimed_at.isoformat(),
            "reason": "active local AI claim",
        }
        logger.info("ownership.owner_of_key -> %r", result)
        return result

    detail = jira_tool.get_issue_detail(key)
    owner = _owner_from_jira_category(detail.get("status_category"))
    result = {
        "key": key,
        "owner": owner,
        "jira_status": detail["status"],
        "jira_category": detail.get("status_category"),
        "local_ticket_id": str(ticket.id) if ticket else None,
        "claimed_at": ticket.claimed_at.isoformat() if ticket and ticket.claimed_at else None,
        "reason": _reason(owner, ticket),
    }
    logger.info("ownership.owner_of_key -> %r", result)
    return result


def _owner_from_jira_category(jira_category: str | None) -> Owner:
    if jira_category == "indeterminate":
        return "human"
    return "none"


def _reason(owner: Owner, ticket: Ticket | None) -> str:
    if owner == "human":
        return "Jira is active/working and no active local AI claim exists"
    if ticket is None:
        return "no local ticket and Jira is not active/working"
    return "local ticket has no active AI claim and Jira is not active/working"
