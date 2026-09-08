"""Reconciliation loop — Jira is the truth (R-28, §5b).

Runs at the START of every poll cycle, BEFORE claiming new tickets.
Detects and fixes every form of drift between the local DB and real Jira status:

  1. local claimed  + Jira == "To Do"            → clear claim; re-queue
  2. local claimed  + Jira in {Done, Blocked}     → mark superseded (human override)
  3. Jira In Progress + no local claim            → mark human_owned; do not claim
  4. Jira "To Do"   + no local claim              → no-op (poller claims it next)
  5. Jira status not in any configured name       → escalation cascade (comment +
                                                    @mention + try blocked + needs_human)
  6. everything else                              → no-op

Isolation guarantee (R-4): within each ticket's reconciliation, only that
ticket's row and events are read/written.  The initial scope scan reads keys
only; no data crosses ticket boundaries.

Escalation cascade (R-11, R-25): must never raise even when every optional
step (status transition, @mention) fails — the minimum guarantee is that a
comment reaches the Jira issue.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db.connection import SessionLocal
from app.db.models import Ticket
from app.events import log_event as ev_log
from app.tools.jira_tool import JiraIssueDetail, JiraTool

logger = logging.getLogger(__name__)

# Module-level sweep cursor — persists across cycles within a process lifetime.
# On (re)start we look back 24 h so recently-active and reopened tickets are
# caught without querying all of Jira history.
_last_sweep_at: datetime | None = None
_FALLBACK_LOOKBACK_HOURS = 24

_JIRA_TODO_STATUS = "To Do"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def reconcile(jira: JiraTool) -> list[dict]:
    """Check every in-scope ticket against current Jira status and fix drift.

    Returns a list of event dicts (one per changed ticket).  Never raises —
    errors for individual tickets are logged and skipped (R-11).
    """
    global _last_sweep_at

    now = datetime.now(timezone.utc)
    since = _last_sweep_at or (now - timedelta(hours=_FALLBACK_LOOKBACK_HOURS))

    logger.info("reconcile: sweep since=%s", since.isoformat())

    scope = _collect_scope(jira, since)
    logger.info("reconcile: %d ticket(s) in scope", len(scope))

    results: list[dict] = []
    for key, detail in scope.items():
        try:
            evt = await _reconcile_one(jira, key, detail)
            if evt:
                results.append(evt)
        except Exception as exc:
            logger.error("reconcile: unhandled error on %r: %s", key, exc)

    _last_sweep_at = now
    logger.info(
        "reconcile: done — %d change(s); next sweep from %s",
        len(results), now.isoformat(),
    )
    return results


# ---------------------------------------------------------------------------
# Scope collection
# ---------------------------------------------------------------------------

def _collect_scope(
    jira: JiraTool,
    since: datetime,
) -> dict[str, "JiraIssueDetail | None"]:
    """Return {external_key: detail_or_None} for every ticket in scope.

    In scope = (a) locally claimed/active  OR  (b) updated in Jira since last sweep.
    The detail for (a)-only entries is fetched lazily inside _reconcile_one.
    """
    scope: dict[str, JiraIssueDetail | None] = {}

    # (a) Locally claimed tickets (claimed_at IS NOT NULL).
    with SessionLocal() as db:
        rows = (
            db.query(Ticket.external_key)
            .filter(
                Ticket.external_key.is_not(None),
                Ticket.claimed_at.is_not(None),
            )
            .all()
        )
    for (ext_key,) in rows:
        scope[ext_key] = None  # detail fetched on demand

    # (b) Jira tickets updated since the last sweep (catches reopened/moved tickets).
    try:
        updated = jira.list_updated_since(since)
        for detail in updated:
            scope.setdefault(detail["key"], detail)
    except Exception as exc:
        logger.warning(
            "reconcile: could not fetch recently-updated Jira tickets: %s — "
            "only locally-claimed tickets will be reconciled this cycle",
            exc,
        )

    return scope


# ---------------------------------------------------------------------------
# Per-ticket drift resolution  (isolation: touches only this ticket — R-4)
# ---------------------------------------------------------------------------

async def _reconcile_one(
    jira: JiraTool,
    key: str,
    detail: "JiraIssueDetail | None",
) -> dict | None:
    """Apply all drift rules for a single ticket.  Returns event dict or None."""

    if detail is None:
        try:
            detail = jira.get_issue_detail(key)
        except Exception as exc:
            logger.warning("reconcile: cannot fetch %r from Jira: %s", key, exc)
            return None

    jira_status: str = detail["status"]
    known = _known_statuses()

    # Fetch this ticket's local row (isolation: only its own row).
    with SessionLocal() as db:
        local: Ticket | None = (
            db.query(Ticket).filter(Ticket.external_key == key).first()
        )

    # ── Rule 1: claimed locally but Jira dragged back to "To Do" ────────────
    if local and local.claimed_at and jira_status == _JIRA_TODO_STATUS:
        return await _clear_claim(key, local, detail)

    # ── Rule 2: claimed locally but Jira moved to Done or Blocked ───────────
    terminal = {
        s.strip().lower()
        for s in [settings.jira_status_done, settings.jira_status_blocked]
        if s.strip()
    }
    if local and local.claimed_at and jira_status.lower() in terminal:
        return await _mark_superseded(key, local, detail)

    # ── Rule 3: Jira shows In Progress but we have no local claim ───────────
    in_progress = settings.jira_status_in_progress.strip().lower()
    if jira_status.lower() == in_progress and (
        local is None or local.claimed_at is None
    ):
        return await _mark_human_owned(key, local, detail)

    # ── Rule 4: "To Do" with no local claim — no-op, poller will claim ──────
    if jira_status == _JIRA_TODO_STATUS and (local is None or local.claimed_at is None):
        logger.debug("reconcile: %r is 'To Do' unclaimed — no-op", key)
        return None

    # ── Rule 5: status not in any configured name → escalate ────────────────
    if jira_status.lower() not in known:
        return await _escalate_unknown(jira, key, local, detail)

    # ── Rule 6: everything in sync — no-op ──────────────────────────────────
    logger.debug("reconcile: %r status=%r — no drift", key, jira_status)
    return None


# ---------------------------------------------------------------------------
# Drift handlers
# ---------------------------------------------------------------------------

async def _clear_claim(
    key: str,
    local: Ticket,
    detail: JiraIssueDetail,
) -> dict:
    """Clear the stale claim — poller will re-pick this ticket next step."""
    ticket_id = str(local.id)
    with SessionLocal() as db:
        db.execute(
            sa_update(Ticket)
            .where(Ticket.id == local.id)
            .values(claimed_at=None, status="new")
        )
        db.commit()
    logger.info("reconcile: CLAIM_CLEARED %r (jira='To Do')", key)
    return await ev_log(
        ticket_id=ticket_id,
        agent="reconcile",
        stage="claim_cleared",
        message=(
            f"{key} claim cleared — Jira was dragged back to 'To Do'; "
            "re-queued for pickup this cycle"
        ),
        key=key,
    )


async def _mark_superseded(
    key: str,
    local: Ticket,
    detail: JiraIssueDetail,
) -> dict:
    """Human moved ticket to Done/Blocked while we held it — mark superseded."""
    ticket_id = str(local.id)
    jira_status = detail["status"]
    with SessionLocal() as db:
        db.execute(
            sa_update(Ticket)
            .where(Ticket.id == local.id)
            .values(claimed_at=None, status="superseded")
        )
        db.commit()
    logger.info("reconcile: SUPERSEDED %r jira_status=%r", key, jira_status)
    return await ev_log(
        ticket_id=ticket_id,
        agent="reconcile",
        stage="superseded",
        message=(
            f"{key} superseded — Jira is '{jira_status}' (human override); "
            "local claim released"
        ),
        key=key,
        jira_status=jira_status,
    )


async def _mark_human_owned(
    key: str,
    local: "Ticket | None",
    detail: JiraIssueDetail,
) -> dict:
    """Jira shows In Progress but we never claimed it — a human is working it."""
    assignee_display = detail.get("assignee_name") or "unknown"

    if local is not None:
        ticket_id = str(local.id)
        with SessionLocal() as db:
            db.execute(
                sa_update(Ticket)
                .where(Ticket.id == local.id)
                .values(status="human_owned")
            )
            db.commit()
    else:
        # First sighting — create a tracking row (not claimed, just recorded).
        with SessionLocal() as db:
            ins = db.execute(
                pg_insert(Ticket)
                .values(
                    source="jira",
                    external_key=key,
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
            ticket_id = str(ins.first()[0])

    logger.info("reconcile: HUMAN_OWNED %r assignee=%r", key, assignee_display)
    return await ev_log(
        ticket_id=ticket_id,
        agent="reconcile",
        stage="human_owned",
        message=(
            f"{key} is In Progress in Jira with no AI claim — "
            f"owned by {assignee_display}; not claiming"
        ),
        key=key,
    )


async def _escalate_unknown(
    jira: JiraTool,
    key: str,
    local: "Ticket | None",
    detail: JiraIssueDetail,
) -> dict:
    """Unknown/unmapped status — escalation cascade (R-11, R-25).

    Must complete even if every optional step fails.  Minimum guarantee:
    a Jira comment reaches a human.
    """
    jira_status = detail["status"]
    account_id = detail.get("assignee_id") or detail.get("reporter_id")
    display_name = (
        detail.get("assignee_name") or detail.get("reporter_name") or "owner"
    )

    logger.warning(
        "reconcile: UNKNOWN_STATUS %r jira_status=%r — escalating", key, jira_status
    )

    # Step 1 (always): post Jira comment @mentioning the owner.
    comment_text = (
        f"⚠️ Automated system notice: '{key}' is in an unmapped status "
        f"'{jira_status}'. The AI agent cannot process tickets in this state. "
        f"Please move this ticket to a known status (To Do / In Progress / Done)."
    )
    try:
        jira.comment_mentioning(key, account_id, display_name, comment_text)
        logger.info("reconcile: escalation comment posted to %r", key)
    except Exception as exc:
        logger.warning("reconcile: escalation comment failed for %r: %s", key, exc)

    # Step 2 (best-effort): try to set status to "blocked" — R-25.
    try:
        jira.set_status(key, "blocked")
    except Exception as exc:
        logger.warning("reconcile: escalation set_blocked failed for %r: %s", key, exc)

    # Step 3: mark local ticket as needs_human.
    if local is not None:
        ticket_id = str(local.id)
        with SessionLocal() as db:
            db.execute(
                sa_update(Ticket)
                .where(Ticket.id == local.id)
                .values(status="needs_human")
            )
            db.commit()
    else:
        with SessionLocal() as db:
            ins = db.execute(
                pg_insert(Ticket)
                .values(
                    source="jira",
                    external_key=key,
                    title=detail["summary"],
                    description=detail["description"],
                    status="needs_human",
                )
                .on_conflict_do_update(
                    index_elements=["external_key"],
                    index_where=Ticket.external_key.is_not(None),
                    set_={"status": "needs_human"},
                )
                .returning(Ticket.id)
            )
            db.commit()
            ticket_id = str(ins.first()[0])

    return await ev_log(
        ticket_id=ticket_id,
        agent="reconcile",
        stage="escalated",
        message=(
            f"{key} escalated — unmapped Jira status '{jira_status}'; "
            f"comment posted @{display_name}; flagged needs_human"
        ),
        key=key,
        jira_status=jira_status,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _known_statuses() -> set[str]:
    """All configured Jira status names (lowercased) plus the hardcoded 'To Do'."""
    configured = [
        settings.jira_status_in_progress,
        settings.jira_status_awaiting_approval,
        settings.jira_status_in_review,
        settings.jira_status_blocked,
        settings.jira_status_done,
        _JIRA_TODO_STATUS,
    ]
    return {s.strip().lower() for s in configured if s.strip()}
