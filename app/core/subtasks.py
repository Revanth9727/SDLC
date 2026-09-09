"""Deterministic subtask replacement and history summaries (R-40/R-43)."""

from __future__ import annotations

import uuid
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text

from app.db.connection import SessionLocal, engine
from app.db.models import Subtask, Ticket, TicketEvent
from app.tools.jira_tool import JiraTool

logger = logging.getLogger(__name__)

ACTIVE_SUBTASK_STATUSES = ("running", "waiting", "pending")


@dataclass(frozen=True)
class SubtaskReplacement:
    subtask_id: str
    superseded: tuple[dict[str, str], ...]
    reused: bool = False


def summarize_subtask(subtask: Subtask, events: list[TicketEvent]) -> str:
    """Describe only this subtask's durable state and events."""
    state = subtask.state or {}
    parts: list[str] = []
    diagnosis = state.get("diagnosis") or {}
    root_cause = str(diagnosis.get("root_cause") or "").strip()
    if root_cause:
        files = diagnosis.get("files") or []
        location = f" in {files[0]}" if files else ""
        parts.append(f"Diagnosis completed: {root_cause}{location}.")

    plan = state.get("plan") or []
    if plan:
        parts.append(f"Plan prepared with {len(plan)} step(s).")

    steps_done = state.get("steps_done") or []
    if steps_done:
        labels = [_step_label(step) for step in steps_done[:3]]
        suffix = " and more" if len(steps_done) > 3 else ""
        parts.append(f"Completed {len(steps_done)} step(s): {', '.join(labels)}{suffix}.")

    if state.get("approval_status") == "pending" and state.get("approval_payload"):
        stopped = "It stopped waiting for plan approval."
    elif state.get("failure_reason"):
        stopped = f"It stopped because {state['failure_reason']}."
    elif plan:
        current = int(state.get("current_step") or 0)
        stopped = f"It stopped before executing plan step {current + 1}."
    elif root_cause:
        stopped = "It stopped after diagnosis, before a plan was completed."
    else:
        last = events[-1] if events else None
        stopped = (
            f"It stopped after {last.agent}/{last.stage}: {last.message}."
            if last else "It stopped before diagnosis completed."
        )

    progress = " ".join(parts) if parts else "No completed agent work was recorded."
    return (
        f"Previous attempt {subtask.id}: {progress} {stopped} "
        "The attempt is now superseded; this summary is preserved for the fresh run."
    )


def prepare_subtask(
    ticket_id: str | uuid.UUID,
    *,
    description: str,
    subtask_type: str = "bug",
    jira: JiraTool | None = None,
    reuse_fresh_pending: bool = False,
) -> SubtaskReplacement:
    """Supersede active attempts and create one fresh subtask atomically."""
    parsed_ticket_id = uuid.UUID(str(ticket_id))
    summaries: list[dict[str, str]] = []
    with SessionLocal() as db:
        # Serialize replacement across poll workers and manual diagnose requests.
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": str(parsed_ticket_id)},
        )
        ticket = db.get(Ticket, parsed_ticket_id)
        if ticket is None:
            raise LookupError(f"ticket {parsed_ticket_id} not found")
        active = list(
            db.scalars(
                select(Subtask)
                .where(
                    Subtask.ticket_id == parsed_ticket_id,
                    Subtask.status.in_(ACTIVE_SUBTASK_STATUSES),
                )
                .order_by(Subtask.created_at.desc(), Subtask.id.desc())
            )
        )
        reused = bool(
            reuse_fresh_pending
            and active
            and active[0].status == "pending"
            and not active[0].state
        )
        if reused:
            keep = active.pop(0)
        else:
            keep = None

        for old in active:
            events = list(
                db.scalars(
                    select(TicketEvent)
                    .where(
                        TicketEvent.ticket_id == parsed_ticket_id,
                        TicketEvent.subtask_id == old.id,
                    )
                    .order_by(TicketEvent.ts.asc())
                )
            )
            summary = summarize_subtask(old, events)
            old.status = "superseded"
            db.add(
                TicketEvent(
                    ticket_id=parsed_ticket_id,
                    subtask_id=old.id,
                    agent="subtask_lifecycle",
                    stage="superseded",
                    message=summary,
                )
            )
            summaries.append({"subtask_id": str(old.id), "message": summary})

        # Comment before constructing the replacement, while the per-ticket lock
        # prevents another worker from creating a competing active row.
        tool = jira or JiraTool()
        if ticket.external_key:
            for item in summaries:
                try:
                    tool.comment(ticket.external_key, item["message"])
                except Exception as exc:
                    # The DB audit trail and lifecycle invariant must still hold
                    # when Jira is temporarily unavailable.
                    logger.warning(
                        "subtask lifecycle: Jira summary comment failed key=%r subtask=%s: %s",
                        ticket.external_key,
                        item["subtask_id"],
                        exc,
                    )

        if keep is None:
            keep = Subtask(
                ticket_id=parsed_ticket_id,
                type=subtask_type,
                description=description,
                status="pending",
                depends_on=[],
            )
            db.add(keep)
            db.flush()
        db.commit()
        return SubtaskReplacement(
            subtask_id=str(keep.id),
            superseded=tuple(summaries),
            reused=reused,
        )


def cleanup_active_subtask_pile() -> int:
    """Close all but the newest active attempt per ticket, then enforce R-43."""
    cleaned = 0
    with SessionLocal() as db:
        ticket_ids = list(
            db.scalars(
                select(Subtask.ticket_id)
                .where(Subtask.status.in_(ACTIVE_SUBTASK_STATUSES))
                .distinct()
            )
        )
        for ticket_id in ticket_ids:
            active = list(
                db.scalars(
                    select(Subtask)
                    .where(
                        Subtask.ticket_id == ticket_id,
                        Subtask.status.in_(ACTIVE_SUBTASK_STATUSES),
                    )
                    .order_by(Subtask.created_at.desc(), Subtask.id.desc())
                )
            )
            for old in active[1:]:
                old.status = "superseded"
                cleaned += 1
        db.commit()
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_subtasks_one_active_per_ticket "
            "ON subtasks (ticket_id) WHERE status IN ('running', 'waiting', 'pending')"
        ))
    return cleaned


def _step_label(step: Any) -> str:
    if isinstance(step, dict):
        return str(step.get("intent") or step.get("step_id") or "unnamed step")
    return str(step)
