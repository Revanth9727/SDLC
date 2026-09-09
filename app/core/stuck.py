"""Stuck-ticket detection (R-28, §5b) — deterministic, no LLM.

Runs after reconciliation in each poll cycle, before the claim scan.

For every non-terminal Jira ticket whose duration exceeds STUCK_THRESHOLD_MINUTES:
  - AI-owned  → post a history-aware Jira comment stating what completed and where
                 it is stuck; do NOT change the Jira status (R-28).
  - human-owned/parked → post a "what's blocking / still needed?" comment
                         referencing any history.

Dedup guard: comment at most once per stuck episode.  ``last_stuck_comment_at``
on the Ticket row plus the last persisted stuck event's Jira status tracks this;
a new Jira status is treated as a new episode, while repeated polls in the same
status do not spam.

Isolation: history is loaded by ``ticket_id`` only — never another ticket's data
(R-4 extended to the reconciliation/stuck layer).
"""

import logging
import re
import uuid as _uuid_mod
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import update as sa_update

from app.config import settings
from app.core.ownership import has_active_ai_run, owner_of
from app.db.connection import SessionLocal
from app.db.models import Ticket, TicketEvent
from app.events import log_event as ev_log
from app.tools.jira_tool import JiraIssueDetail, JiraTool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def check_stuck(
    jira: JiraTool,
    emit: Callable[[dict], Awaitable[None]],
) -> list[dict]:
    """Identify stuck non-terminal tickets and post informed Jira comments.

    Scope:
      (a) Locally claimed, AI-owned tickets whose ``claimed_at`` is older than
          the threshold — precise because we set the timestamp ourselves.
      (b) Jira non-terminal tickets whose ``updated`` timestamp is older than the
          threshold — covers active, ready, and parked statuses.

    Returns a list of event dicts (one per comment posted).
    Never raises — errors per ticket are logged and skipped (R-11).
    """
    now = datetime.now(timezone.utc)
    threshold = timedelta(minutes=settings.stuck_threshold_minutes)
    results: list[dict] = []
    processed_keys: set[str] = set()

    # ── (a) AI-owned: precise claimed_at age ────────────────────────────────
    with SessionLocal() as db:
        ai_candidates = (
            db.query(Ticket)
            .filter(
                Ticket.claimed_at.is_not(None),
                Ticket.claimed_at < now - threshold,
            )
            .all()
        )

    for ticket in ai_candidates:
        if not has_active_ai_run(ticket):
            continue  # claimed_at set but already superseded / done
        if ticket.external_key:
            processed_keys.add(ticket.external_key)
        try:
            evt = await _handle_stuck(jira, ticket, "ai", now, emit)
            if evt:
                results.append(evt)
                await emit(evt)
        except Exception as exc:
            logger.error("stuck: AI ticket %s error: %s", ticket.id, exc)

    # ── (b) Human/parked: Jira non-terminal, not updated in > threshold ──────
    try:
        human_candidates = jira.list_non_terminal_stuck(settings.stuck_threshold_minutes)
    except Exception as exc:
        logger.warning("stuck: Jira non-terminal query failed: %s", exc)
        human_candidates = []

    for detail in human_candidates:
        key = detail["key"]
        if key in processed_keys:
            continue  # already handled as AI-owned above

        with SessionLocal() as db:
            ticket = db.query(Ticket).filter(Ticket.external_key == key).first()

        if ticket is None:
            ticket = _ensure_local_ticket(detail)

        if has_active_ai_run(ticket):
            continue  # covered in (a); active AI claim, not human-owned

        try:
            detected_owner = owner_of(ticket, jira)
            owner_for_comment = "human" if detected_owner in {"human", "none"} else detected_owner
            evt = await _handle_stuck(jira, ticket, owner_for_comment, now, emit)
            if evt:
                results.append(evt)
                await emit(evt)
        except Exception as exc:
            logger.error("stuck: human ticket %r error: %s", key, exc)

    logger.info("stuck: check done — %d comment(s) posted", len(results))
    return results


# ---------------------------------------------------------------------------
# Per-ticket handler
# ---------------------------------------------------------------------------

async def _handle_stuck(
    jira: JiraTool,
    ticket: Ticket,
    owner: str,
    now: datetime,
    emit: Callable[[dict], Awaitable[None]],
) -> dict | None:
    """Post one stuck comment for a single ticket if not already done this episode."""

    if not ticket.external_key:
        return None  # manual ticket with no Jira issue

    # Fetch live detail for status/category and @mention (assignee → reporter fallback).
    try:
        detail = jira.get_issue_detail(ticket.external_key)
    except Exception as exc:
        logger.warning("stuck: cannot fetch detail for %r: %s", ticket.external_key, exc)
        return None

    jira_status = detail["status"]
    jira_category = detail.get("status_category")
    if jira_category == "done":
        logger.debug("stuck: %r is done-category — clearing stuck episode", ticket.external_key)
        _clear_stuck_episode(ticket)
        return None

    threshold_mins = settings.stuck_threshold_minutes
    if not _is_jira_stale(detail, now, threshold_mins):
        logger.debug(
            "stuck: %r status=%r changed recently — under threshold, skipping",
            ticket.external_key,
            jira_status,
        )
        return None

    # Dedup guard — one comment per stuck episode/status (R-28).
    last_episode_status = _last_stuck_episode_status(str(ticket.id))
    if (
        ticket.last_stuck_comment_at is not None
        and last_episode_status == jira_status
    ):
        logger.debug(
            "stuck: %r already commented at %s for status=%r — same episode, skipping",
            ticket.external_key,
            ticket.last_stuck_comment_at.isoformat(),
            jira_status,
        )
        return None

    account_id = detail.get("assignee_id") or detail.get("reporter_id")
    display_name = detail.get("assignee_name") or detail.get("reporter_name") or "owner"

    # Read THIS ticket's own event history (isolation: R-4).
    history = _load_history(str(ticket.id))
    context = _history_context(history)

    if owner == "ai":
        elapsed_mins = int((now - ticket.claimed_at).total_seconds() / 60)
        comment_body = (
            f"Automated system notice: the AI agent has held this ticket in "
            f"'{jira_status}' for {elapsed_mins} min "
            f"(threshold: {threshold_mins} min). "
            f"{context}"
            "Jira status intentionally unchanged — please review and re-queue or "
            "reassign if needed."
        )
    else:
        duration_text = _duration_since_updated(detail, now) or f"over {threshold_mins} min"
        comment_body = (
            f"Automated system notice: this has been in '{jira_status}' for "
            f"{duration_text} with no recent updates. "
            f"{context}"
            "Is this still needed, blocked, or ready to move? Any update helps keep the board accurate."
        )

    # Post comment — never raises, never changes Jira status (R-11, R-28).
    try:
        jira.comment_mentioning(ticket.external_key, account_id, display_name, comment_body)
        logger.info(
            "stuck: COMMENT_POSTED key=%r owner=%r mention=%r",
            ticket.external_key, owner, display_name,
        )
    except Exception as exc:
        logger.warning("stuck: comment failed for %r: %s", ticket.external_key, exc)
        return None

    # Persist dedup timestamp (isolation: only this ticket's row).
    with SessionLocal() as db:
        db.execute(
            sa_update(Ticket)
            .where(Ticket.id == ticket.id)
            .values(last_stuck_comment_at=now)
        )
        db.commit()

    return await ev_log(
        ticket_id=str(ticket.id),
        agent="stuck_detector",
        stage="stuck_comment_posted",
        message=(
            f"{ticket.external_key} stuck ({owner}) jira_status={jira_status!r} "
            f"jira_category={jira_category!r}: comment posted @{display_name} — "
            f"{context.strip()}"
        ),
        key=ticket.external_key,
        owner=owner,
        jira_status=jira_status,
        jira_category=jira_category,
    )


# ---------------------------------------------------------------------------
# History helpers (isolation: reads only this ticket's own events)
# ---------------------------------------------------------------------------

def _load_history(ticket_id: str) -> list[TicketEvent]:
    """Fetch all events for this ticket in ascending time order (R-4)."""
    with SessionLocal() as db:
        return (
            db.query(TicketEvent)
            .filter(TicketEvent.ticket_id == _uuid_mod.UUID(ticket_id))
            .order_by(TicketEvent.ts.asc())
            .all()
        )


def _ensure_local_ticket(detail: JiraIssueDetail) -> Ticket:
    """Create a local tracking row for a stale Jira ticket if needed."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    with SessionLocal() as db:
        ins = db.execute(
            pg_insert(Ticket)
            .values(
                source="jira",
                external_key=detail["key"],
                title=detail["summary"],
                description=detail["description"],
                status="human_owned",
            )
            .on_conflict_do_update(
                index_elements=["external_key"],
                index_where=Ticket.external_key.is_not(None),
                set_={"status": "human_owned"},
            )
            .returning(Ticket.id)
        )
        db.commit()
        ticket_id = ins.first()[0]

    with SessionLocal() as db:
        return db.query(Ticket).filter(Ticket.id == ticket_id).first()


def _clear_stuck_episode(ticket: Ticket) -> None:
    with SessionLocal() as db:
        db.execute(
            sa_update(Ticket)
            .where(Ticket.id == ticket.id)
            .values(last_stuck_comment_at=None)
        )
        db.commit()


def _last_stuck_episode_status(ticket_id: str) -> str | None:
    """Return the Jira status recorded by this ticket's latest stuck event."""
    events = _load_history(ticket_id)
    for event in reversed(events):
        if event.agent != "stuck_detector" or event.stage != "stuck_comment_posted":
            continue
        match = re.search(r"jira_status='([^']*)'", event.message)
        if match:
            return match.group(1)
        return None
    return None


def _duration_since_updated(detail: JiraIssueDetail, now: datetime) -> str | None:
    updated_dt = _parse_jira_updated(detail)
    if updated_dt is None:
        return None
    minutes = max(0, int((now - updated_dt).total_seconds() / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    rem = minutes % 60
    return f"{hours} hr {rem} min"


def _is_jira_stale(detail: JiraIssueDetail, now: datetime, threshold_mins: int) -> bool:
    updated_dt = _parse_jira_updated(detail)
    if updated_dt is None:
        return True
    return now - updated_dt >= timedelta(minutes=threshold_mins)


def _parse_jira_updated(detail: JiraIssueDetail) -> datetime | None:
    updated = detail.get("updated")
    if not updated:
        return None
    try:
        return datetime.fromisoformat(updated).astimezone(timezone.utc)
    except ValueError:
        return None


def _history_context(events: list[TicketEvent]) -> str:
    """Build a brief, informative context sentence from the event trail.

    Prefers agent work events over system/infra events so the comment
    reflects real progress rather than poller/reconcile noise.
    """
    if not events:
        return "No activity recorded for this ticket yet. "

    # Prefer substantive agent events (not poller / reconcile / stuck_detector).
    _SYSTEM_AGENTS = {"poller", "reconcile", "stuck_detector"}
    agent_events = [e for e in events if e.agent not in _SYSTEM_AGENTS]

    if agent_events:
        last = agent_events[-1]
        first = agent_events[0]
        if last is first:
            return f"Last progress: {last.agent}/{last.stage} — {last.message}. "
        return (
            f"Started at {first.agent}/{first.stage}; "
            f"last progress: {last.agent}/{last.stage} — {last.message}. "
        )

    # Only system events available — report the most recent meaningful one.
    last = events[-1]
    claim_ev = next(
        (e for e in events if e.stage in {"claimed", "started"}), None
    )
    if claim_ev:
        return (
            f"Ticket was {claim_ev.stage} but no agent work has been recorded yet "
            f"(last system event: {last.agent}/{last.stage}). "
        )
    return f"Last system event: {last.agent}/{last.stage} — {last.message}. "
