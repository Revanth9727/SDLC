"""In-process async pub/sub for per-ticket event streaming (R-7, R-23, §9).

Every agent activation, tool call, guard decision, and stage transition should
call publish() here.  Any number of SSE clients can subscribe() to watch a
ticket's stream live.

Design constraints:
- Pure asyncio — no external broker, no threads (R-5: deterministic code only).
- Per-ticket isolation: subscribe(ticket_id) only receives that ticket's events
  (R-4 extended to the event layer).
- Ephemeral: events are in-memory queues only.  Audit persistence is a later
  phase (R-12 does not require this module to persist; the checkpointer does).
- Never raises: publish silently drops events when there are no subscribers so
  tools are never blocked by stream consumers (R-11).
"""

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)

# ticket_id -> list of live subscriber queues
_subs: dict[str, list[asyncio.Queue]] = defaultdict(list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def make_event(
    agent: str,
    stage: str,
    message: str,
    *,
    ticket_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a canonical event dict with the standard {agent, stage, message, ts} shape."""
    ev: dict[str, Any] = {
        "agent": agent,
        "stage": stage,
        "message": message,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if ticket_id is not None:
        ev["ticket_id"] = ticket_id
    ev.update(extra)
    return ev


async def publish(ticket_id: str, event: dict[str, Any]) -> None:
    """Broadcast event to all current subscribers for ticket_id.

    Never raises.  Logs at DEBUG level so every tool call is traceable (R-7).
    """
    queues = list(_subs.get(ticket_id, []))
    logger.debug(
        "events.publish ticket=%r agent=%r stage=%r subs=%d",
        ticket_id, event.get("agent"), event.get("stage"), len(queues),
    )
    for q in queues:
        try:
            await q.put(event)
        except Exception as exc:
            logger.warning("events.publish: queue put failed: %s", exc)


@asynccontextmanager
async def subscribe(ticket_id: str) -> AsyncGenerator[asyncio.Queue, None]:
    """Context manager that registers a queue and yields it to the caller.

    Usage::

        async with events.subscribe(ticket_id) as q:
            while True:
                event = await asyncio.wait_for(q.get(), timeout=20)
                yield event

    The queue is automatically deregistered when the context exits, so
    disconnected clients never accumulate stale queues.
    """
    q: asyncio.Queue = asyncio.Queue()
    _subs[ticket_id].append(q)
    logger.debug(
        "events.subscribe ticket=%r total_subs=%d",
        ticket_id, len(_subs[ticket_id]),
    )
    try:
        yield q
    finally:
        bucket = _subs.get(ticket_id)
        if bucket and q in bucket:
            bucket.remove(q)
        if not _subs.get(ticket_id):
            _subs.pop(ticket_id, None)
        logger.debug("events.unsubscribe ticket=%r", ticket_id)


def subscriber_count(ticket_id: str) -> int:
    """Return the number of active SSE subscribers for a ticket (useful in tests)."""
    return len(_subs.get(ticket_id, []))
