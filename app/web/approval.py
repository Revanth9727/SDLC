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
from app.db.models import Subtask, Ticket, PendingApproval
from app.core.subtasks import ACTIVE_SUBTASK_STATUSES
from app.orchestrator.graph import GATE_NODES, ApprovalConflict, open_graph, resume_approval, thread_config

router = APIRouter()


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    note: str = Field(default="", max_length=4000)


@router.get('/tickets/{ticket_id}/open-pr')
async def open_pr(ticket_id: UUID):
    from app.core.publish import preflight
    try:
        current = await preflight(ticket_id, notify=False)
    except LookupError:
        raise HTTPException(404, "Ticket not found")
    return {"open_pr": current.__dict__ if current else None}


@router.post('/tickets/{ticket_id}/redo-pr')
async def redo_pr(ticket_id: UUID):
    from app.core.publish import redo
    try:
        urls = await redo(ticket_id)
        return {"pr_url": urls[0]}
    except (LookupError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


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
        if state.orchestration_role in {"coordinator", "work"}:
            _update_multi_ticket_status(db, ticket)
            db.commit()
            return
        if state.status == "in_review":
            ticket.status = "in_review"
        elif state.status == "needs_human":
            ticket.status = "needs_human"
            db.query(PendingApproval).filter(PendingApproval.subtask_id == row.id,
                PendingApproval.status == 'PENDING').update({'status': 'EXPIRED'})
        elif state.status == "failed":
            ticket.status = "needs_human"
        elif state.status == "done":
            ticket.status = "done"
        elif state.approval_status == "pending" and state.approval_payload:
            ticket.status = "awaiting_approval"
        else:
            ticket.status = "processing"
        db.commit()


def _update_multi_ticket_status(db, ticket: Ticket) -> None:
    rows = db.query(Subtask).filter(Subtask.ticket_id == ticket.id).all()
    work = [(row, row.state or {}) for row in rows if (row.state or {}).get("orchestration_role") == "work"]
    if not work:
        coordinator = next(((row, state) for row, state in
                            ((row, row.state or {}) for row in rows)
                            if state.get("orchestration_role") == "coordinator"), None)
        if coordinator and coordinator[1].get("approval_status") == "pending":
            ticket.status = "awaiting_approval"
        else:
            ticket.status = "processing"
        return
    statuses = [row.status for row, _ in work]
    if any(status in {"queued", "running", "waiting", "pending"} for status in statuses):
        waiting = any(state.get("approval_status") == "pending" and state.get("approval_payload")
                      for row, state in work if row.status in ACTIVE_SUBTASK_STATUSES)
        ticket.status = "awaiting_approval" if waiting else "processing"
        return
    successes = any(status in {"in_review", "done"} for status in statuses)
    failures = any(status in {"needs_human", "failed"} for status in statuses)
    if successes and failures:
        ticket.status = "mixed"
    elif failures:
        ticket.status = "needs_human"
    elif any(status == "integration_pending" for status in statuses):
        ticket.status = "processing"
    elif any(status == "in_review" for status in statuses):
        ticket.status = "in_review"
    else:
        ticket.status = "done"


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
        row["waiting"] = snapshot.next in {(node,) for node in GATE_NODES} and any(task.interrupts for task in snapshot.tasks)
    return rows


async def _decide(ticket_id: UUID, subtask_id: UUID, decision: ApprovalDecision):
    rows = await asyncio.to_thread(_subtasks, ticket_id, subtask_id)
    if rows[0]["status"] == "superseded":
        raise HTTPException(409, "This subtask was superseded")
    if decision.approval_status == 'rejected' and not decision.note.strip():
        async with open_graph(str(ticket_id), str(subtask_id), lock=True) as graph:
            snapshot = await graph.aget_state(thread_config(str(ticket_id), str(subtask_id)))
            if (not snapshot.values or snapshot.next not in {(node,) for node in GATE_NODES} or
                    not any(task.interrupts for task in snapshot.tasks)):
                raise HTTPException(409, 'This subtask is not waiting for approval')
            # Only the plan gate re-plans on reject (R-48); a blank intent-gate
            # rejection has no "revision" to ask for yet, so it just proceeds
            # below as a normal (blank-note) rejection.
            if snapshot.next == ('human_gate',):
                state = SubtaskState.model_validate(snapshot.values)
                message = 'What should change? Reply with the plan revision you want.'
                await log_event(ticket_id=str(ticket_id), subtask_id=str(subtask_id), agent='orchestrator',
                                stage='approval_feedback_requested', message=message)
                return {**state.model_dump(mode='json'), 'message': message}
    try:
        async with open_graph(str(ticket_id), str(subtask_id), lock=True) as graph:
            state = await resume_approval(graph, str(ticket_id), str(subtask_id), decision)
            await asyncio.to_thread(persist_state, state)
            from app.core.approvals import resolve_plan_gate
            await asyncio.to_thread(resolve_plan_gate, state)
    except ApprovalConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    await log_event(ticket_id=str(ticket_id), subtask_id=str(subtask_id), agent="orchestrator",
                    stage="resumed", message=f"Decision saved: {state.approval_status}")
    if state.orchestration_role == "coordinator" and state.approval_status == "approved":
        from app.orchestrator.scheduler import materialize_and_advance
        work_states = await materialize_and_advance(state)
        if work_states:
            # Keep the historical single-state response contract while surfacing
            # every durable child for multi-subtask callers. The primary state is
            # a real work blackboard, never the coordinator or a pre-run copy.
            result = work_states[0].model_dump(mode="json")
            result["work_subtasks"] = [item.model_dump(mode="json") for item in work_states]
            return result
    elif state.orchestration_role == "work" and state.status in {
        "integration_pending", "in_review", "done", "needs_human", "failed"
    }:
        from app.orchestrator.scheduler import advance_ticket
        await advance_ticket(state.ticket_id)
        # Integration/publication can update this row after the graph returned.
        # Reload it so status, PR URL, and Critic verdict all come from one
        # durable final blackboard.
        rows = await asyncio.to_thread(_subtasks, ticket_id, subtask_id)
        return rows[0]["state"]
    return state.model_dump(mode="json")


@router.post("/tickets/{ticket_id}/subtasks/{subtask_id}/approve")
async def approve(ticket_id: UUID, subtask_id: UUID, body: DecisionBody = DecisionBody()):
    return await _decide(ticket_id, subtask_id, ApprovalDecision(approval_status="approved", note=body.note))


@router.post("/tickets/{ticket_id}/subtasks/{subtask_id}/reject")
async def reject(ticket_id: UUID, subtask_id: UUID, body: DecisionBody):
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
