"""Jira poller — deterministic background job (no LLM, R-5, R-27).

Each cycle (every settings.jira_poll_interval_minutes):
  1. Overlap guard: skip tick if previous cycle still running.
  2. Fetch Jira issues in status "To Do" (the ready signal).
  3. For each, CLAIM atomically BEFORE any work:
       a. skip if tickets.claimed_at is already set
       b. insert/mark the ticket row with claimed_at = now
       c. flip Jira status to in_progress via JiraTool.set_status (R-25)
  4. Emit a structured event per claimed ticket.

Real agent processing (Planner etc.) is wired in later phases; "process" here
means: persist the record and emit the event. Sequential for now (Phase 10 adds
parallelism).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db.connection import SessionLocal
from app.db.models import Ticket
from app.events import log_event as ev_log
from app.tools.jira_tool import JiraTool

logger = logging.getLogger(__name__)

# Overlap guard — held for the duration of a running cycle.
_RUNNING = asyncio.Lock()

_JIRA_TODO_STATUS = "To Do"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def poll_cycle(emit: Callable[[dict], Awaitable[None]]) -> list[dict]:
    """Run one poll cycle.

    Skips immediately if the previous cycle has not finished yet (R-27).
    Returns the list of claim-event dicts for callers that want them.
    """
    if _RUNNING.locked():
        logger.info("poller: previous cycle still running — skipping tick")
        return []
    async with _RUNNING:
        return await _do_poll(emit)


async def start_loop(emit: Callable[[dict], Awaitable[None]]) -> asyncio.Task:
    """Start the background poll loop. Call once at app startup."""
    interval = settings.jira_poll_interval_minutes * 60

    async def _loop() -> None:
        logger.info(
            "poller: loop started — interval=%d min", settings.jira_poll_interval_minutes
        )
        while True:
            await poll_cycle(emit)
            await asyncio.sleep(interval)

    return asyncio.create_task(_loop(), name="jira_poller")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

async def _do_poll(emit: Callable[[dict], Awaitable[None]]) -> list[dict]:
    jira = JiraTool()
    try:
        issues = jira.list_by_status(_JIRA_TODO_STATUS)
    except Exception as exc:
        logger.error("poller: jira query failed: %s", exc)
        return []

    logger.info("poller: found %d '%s' issues", len(issues), _JIRA_TODO_STATUS)
    claimed: list[dict] = []
    for issue in issues:
        event = await _claim_one(jira, issue)
        if event:
            claimed.append(event)
            await emit(event)

    logger.info("poller: cycle done — claimed %d ticket(s)", len(claimed))
    return claimed


async def _claim_one(jira: JiraTool, issue: dict) -> dict | None:
    """Claim one ticket atomically via two-step DB upsert.

    Step A: UPDATE WHERE claimed_at IS NULL — claims an existing unclaimed row.
    Step B: INSERT ON CONFLICT DO NOTHING — claims if the row doesn't exist yet.
    If neither returns a row, another process beat us to it; we skip.
    The partial unique index on external_key makes both steps safe across processes.
    """
    key: str = issue["key"]
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        # Step A: claim an existing unclaimed ticket.
        upd = db.execute(
            sa_update(Ticket)
            .where(Ticket.external_key == key, Ticket.claimed_at.is_(None))
            .values(claimed_at=now)
            .returning(Ticket.id)
        )
        row = upd.first()

        if row is None:
            # Step B: insert if it doesn't exist yet (ON CONFLICT = already claimed).
            ins = db.execute(
                pg_insert(Ticket)
                .values(
                    source="jira",
                    external_key=key,
                    title=issue["summary"],
                    description=issue["description"],
                    status="new",
                    claimed_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=["external_key"],
                    index_where=Ticket.external_key.is_not(None),
                )
                .returning(Ticket.id)
            )
            row = ins.first()

        db.commit()

        if row is None:
            logger.info("poller: %r already claimed — skipping", key)
            return None

        ticket_id = str(row.id)

    logger.info("poller: CLAIMED %r ticket_id=%s", key, ticket_id)

    # Persist + publish so the event survives a page reload (R-7).
    await ev_log(
        ticket_id=ticket_id,
        agent="poller",
        stage="claimed",
        message=f"{key} claimed from Jira and recorded locally",
        key=key,
    )

    # 3c: flip Jira status — never raise on failure (R-11)
    try:
        result = jira.set_status(key, "in_progress")
        logger.info("poller: set_status %r -> applied=%s", key, result["applied"])
    except Exception as exc:
        logger.warning("poller: set_status failed for %r: %s", key, exc)

    return {
        "type": "claimed",
        "key": key,
        "title": issue["summary"],
        "ticket_id": ticket_id,
        "claimed_at": now.isoformat(),
    }
