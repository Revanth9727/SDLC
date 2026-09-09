"""Ticket-scoped approval API; checkpoints are the source of truth."""

import asyncio
import re
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.agents.planning import ApprovalDecision
from app.agents.state import SubtaskState
from app.events import log_event
from app.db.connection import SessionLocal
from app.db.models import Subtask, Ticket
from app.core.subtasks import ACTIVE_SUBTASK_STATUSES
from app.core.approvals import resolve_plan_gate
from app.orchestrator.graph import ApprovalConflict, open_graph, resume_approval, thread_config

router = APIRouter()


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    note: str = Field(default="", max_length=4000)


def _subtasks(ticket_id: UUID, subtask_id: UUID | None = None):
    with SessionLocal() as db:
        if db.get(Ticket, ticket_id) is None:
            raise HTTPException(404, "Ticket not found")
        query = db.query(Subtask).filter(Subtask.ticket_id == ticket_id)
        if subtask_id is not None:
            query = query.filter(Subtask.id == subtask_id)
        rows = query.order_by(Subtask.created_at.asc()).all()
        if subtask_id is not None and not rows:
            raise HTTPException(404, "Subtask not found for this ticket")
        return [{"subtask_id": str(row.id), "status": row.status, "state": row.state} for row in rows]


def persist_state(state: SubtaskState):
    with SessionLocal() as db:
        row = db.query(Subtask).filter(
            Subtask.id == UUID(state.subtask_id), Subtask.ticket_id == UUID(state.ticket_id)
        ).one()
        if row.status == "superseded":
            raise ApprovalConflict("This subtask was superseded")
        row.state = state.model_dump(mode="json")
        row.status = state.status
        ticket = db.get(Ticket, UUID(state.ticket_id))
        if state.status == "in_review":
            ticket.status = "in_review"
        elif state.status == "needs_human":
            ticket.status = "needs_human"
        elif state.status == "done":
            ticket.status = "done"
        elif state.approval_status == "pending" and state.approval_payload:
            ticket.status = "awaiting_approval"
        else:
            ticket.status = "processing"
        db.commit()


@router.get("/tickets/{ticket_id}/subtasks")
async def ticket_subtasks(ticket_id: UUID):
    rows = await asyncio.to_thread(_subtasks, ticket_id)
    for row in rows:
        if row["status"] not in ACTIVE_SUBTASK_STATUSES:
            row["waiting"] = False
            continue
        async with open_graph(str(ticket_id), row["subtask_id"]) as graph:
            snapshot = await graph.aget_state(thread_config(str(ticket_id), row["subtask_id"]))
            row['resumable'] = False
            if snapshot.next and snapshot.values.get('approval_status') == 'approved':
                cursor = await graph.checkpointer.conn.execute(
                    'SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked',
                    (f"{ticket_id}:{row['subtask_id']}",))
                row['resumable'] = (await cursor.fetchone())['locked']
        if snapshot.values:
            state = SubtaskState.model_validate(snapshot.values)
            row["state"] = state.model_dump(mode="json")
            row["status"] = state.status
        row["waiting"] = snapshot.next == ("human_gate",) and any(task.interrupts for task in snapshot.tasks)
    return rows


async def _decide(ticket_id: UUID, subtask_id: UUID, decision: ApprovalDecision):
    rows = await asyncio.to_thread(_subtasks, ticket_id, subtask_id)
    if rows[0]["status"] == "superseded":
        raise HTTPException(409, "This subtask was superseded")
    try:
        async with open_graph(str(ticket_id), str(subtask_id), lock=True) as graph:
            state = await resume_approval(graph, str(ticket_id), str(subtask_id), decision)
            await asyncio.to_thread(persist_state, state)
            await asyncio.to_thread(resolve_plan_gate, state)
    except ApprovalConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    await log_event(ticket_id=str(ticket_id), subtask_id=str(subtask_id), agent="orchestrator",
                    stage="resumed", message=f"Decision saved: {state.approval_status}")
    return state.model_dump(mode="json")


@router.post("/tickets/{ticket_id}/subtasks/{subtask_id}/approve")
async def approve(ticket_id: UUID, subtask_id: UUID, body: DecisionBody = DecisionBody()):
    return await _decide(ticket_id, subtask_id, ApprovalDecision(approval_status="approved", note=body.note))


@router.post("/tickets/{ticket_id}/subtasks/{subtask_id}/reject")
async def reject(ticket_id: UUID, subtask_id: UUID, body: DecisionBody):
    if not body.note:
        raise HTTPException(422, "A rejection note is required")
    return await _decide(ticket_id, subtask_id, ApprovalDecision(approval_status="rejected", note=body.note))


_PR_URL_RE = re.compile(r'^https://github\.com/([^/]+/[^/]+)/pull/(\d+)$')
_NO_CI = {'configured': False, 'status': 'none', 'conclusion': None, 'url': None, 'runs': []}


@router.get('/tickets/{ticket_id}/subtasks/{subtask_id}/ci-status')
async def subtask_ci_status(ticket_id: UUID, subtask_id: UUID):
    """Live CI status for the subtask's PR (ai_rules.md R-32/R-46): CI is the
    authoritative check, surfaced alongside the local pytest result already in
    `state`, never persisted/polled server-side — the UI asks when it wants it."""
    rows = await asyncio.to_thread(_subtasks, ticket_id, subtask_id)
    pr_url = (rows[0]['state'] or {}).get('pr_url')
    if not pr_url:
        return _NO_CI
    match = _PR_URL_RE.match(pr_url)
    if not match:
        return _NO_CI
    from app.tools.github_tool import GitHubTool
    return await asyncio.to_thread(GitHubTool().pr_checks, match.group(1), int(match.group(2)))


@router.get('/tickets/{ticket_id}/approvals')
async def registered_approvals(ticket_id: UUID):
    from app.core.approvals import pending_for_ticket
    await asyncio.to_thread(_subtasks, ticket_id)
    return await asyncio.to_thread(pending_for_ticket, ticket_id)


@router.post('/tickets/{ticket_id}/approvals/{approval_id}/{action}')
async def decide_proposal(ticket_id: UUID, approval_id: UUID, action: str, body: DecisionBody = DecisionBody()):
    from app.core.proposals import get_proposal
    from app.core.comment_handling import decide_registered
    if action not in {'approve', 'reject'}:
        raise HTTPException(404, 'Unknown action')
    if action == 'reject' and not body.note:
        raise HTTPException(422, 'A rejection note is required')
    try:
        record = await asyncio.to_thread(get_proposal, approval_id)
        if record['ticket_id'] != str(ticket_id):
            raise HTTPException(404, 'Approval not found for this ticket')
        decision = ApprovalDecision(approval_status='approved' if action == 'approve' else 'rejected', note=body.note)
        return await decide_registered(str(approval_id), decision)
    except LookupError:
        raise HTTPException(404, 'Approval not found')
    except (ApprovalConflict, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
