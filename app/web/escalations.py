"""Human resolution of durable guard interrupts, scoped to ticket and subtask."""
import asyncio
from pathlib import Path
from uuid import UUID, uuid4
from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates
from langgraph.types import Command
from app.agents.state import SubtaskState
from app.core.budget import usage
from app.db.connection import SessionLocal
from app.db.models import Subtask
from app.orchestrator.graph import open_graph, thread_config, ApprovalConflict
from app.web.approval import DecisionBody, _subtasks, persist_state

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / 'templates')


@router.get('/escalations')
def queue(request: Request):
    with SessionLocal() as db:
        rows = db.query(Subtask).filter(Subtask.status == 'needs_human').order_by(Subtask.created_at).all()
        for row in rows:
            if not (row.state or {}).get('escalation_id'):
                row.state = {**(row.state or {}), 'escalation_id': str(uuid4())}
        db.commit()
        items = [{'ticket_id': str(row.ticket_id), 'id': str(row.id), 'state': row.state or {},
                  'description': row.description} for row in rows]
    return templates.TemplateResponse('escalations.html', {'request': request, 'items': items})


@router.get('/tickets/{ticket_id}/budget')
def ticket_budget(ticket_id: UUID):
    _subtasks(ticket_id)
    return usage(str(ticket_id)) or {'calls': 0, 'tokens': 0, 'est_cost_usd': 0}


class ResolutionBody(DecisionBody):
    escalation_id: UUID


@router.post('/tickets/{ticket_id}/subtasks/{subtask_id}/escalation/{action}')
async def resolve(ticket_id: UUID, subtask_id: UUID, action: str, body: ResolutionBody):
    if action not in {'retry', 'reject'}:
        raise HTTPException(404, 'Unknown resolution')
    rows = await asyncio.to_thread(_subtasks, ticket_id, subtask_id)
    if rows[0]['status'] == 'superseded':
        raise HTTPException(409, 'Subtask was superseded')
    try:
        async with open_graph(str(ticket_id), str(subtask_id), lock=True) as graph:
            config = thread_config(str(ticket_id), str(subtask_id))
            snapshot = await graph.aget_state(config)
            # Older phases ended on escalation; adopt their saved state into
            # the same interrupt path without rerunning agents or tool actions.
            if not snapshot.next and rows[0]['status'] == 'needs_human':
                stored = rows[0]['state'] or {}
                if stored.get('escalation_id') != str(body.escalation_id):
                    raise HTTPException(409, 'This escalation changed; reload before deciding')
                try:
                    legacy = SubtaskState.model_validate(stored)
                except ValueError:
                    raise HTTPException(422, 'This older attempt has incomplete state. Open its ticket to resolve the repository and start a new diagnosis.')
                await graph.aupdate_state(config, legacy.model_dump(mode='json'), as_node='escalate')
                await graph.ainvoke(None, config=config)
                snapshot = await graph.aget_state(config)
            if snapshot.values.get('escalation_id') != str(body.escalation_id):
                raise HTTPException(409, 'This escalation changed; reload before deciding')
            paused = snapshot.next == ('human_resolution',) and any(t.interrupts for t in snapshot.tasks)
            recovering = (snapshot.next and not any(t.interrupts for t in snapshot.tasks)
                          and snapshot.values.get('resolution') == action
                          and snapshot.values.get('approval_note') == body.note)
            if not paused and not recovering:
                raise HTTPException(409, 'This subtask is not waiting for human resolution')
            state = SubtaskState.model_validate(snapshot.values)
            if state.ticket_id != str(ticket_id) or state.subtask_id != str(subtask_id):
                raise HTTPException(409, 'Checkpoint identity mismatch')
            await graph.ainvoke(Command(resume={'action': action, 'note': body.note}) if paused else None, config=config)
            state = SubtaskState.model_validate((await graph.aget_state(config)).values)
            await asyncio.to_thread(persist_state, state)
            return state.model_dump(mode='json')
    except ApprovalConflict as exc:
        raise HTTPException(409, str(exc))
