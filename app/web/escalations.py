"""Human resolution of durable guard interrupts, scoped to ticket and subtask."""
import asyncio
from pathlib import Path
from uuid import UUID, uuid4
from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates
from langgraph.types import Command
from app.agents.state import SubtaskState
from app.agents.constraints import ConstraintChange
from app.config import settings
from app.core.budget import usage
from app.db.connection import SessionLocal
from app.db.models import Subtask, Ticket
from app.orchestrator.graph import open_graph, thread_config, ApprovalConflict
from app.web.approval import DecisionBody, _subtasks, persist_state
from pydantic import BaseModel, Field

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
    constraint_changes: list[ConstraintChange] = Field(default_factory=list)
    replan: bool = False


class VerificationCredentialsBody(BaseModel):
    escalation_id: UUID
    credentials: dict[str, str] = Field(default_factory=dict)


class IntegrationResolutionBody(BaseModel):
    intended_behavior: str = Field(default="", max_length=8000)
    reject_subtask_id: UUID | None = None


@router.post('/tickets/{ticket_id}/integration/{action}')
async def resolve_integration(ticket_id: UUID, action: str, body: IntegrationResolutionBody):
    """Resolve the R-31 decision surface without ever approving an old artifact."""
    if action not in {'resolve_manually', 'state_intended_behavior', 'reject_change', 'replan'}:
        raise HTTPException(404, 'Unknown integration resolution')
    with SessionLocal() as db:
        rows = list(db.query(Subtask).filter(Subtask.ticket_id == ticket_id).all())
        affected = [row for row in rows if (row.state or {}).get('integration_issue')]
        if not affected:
            raise HTTPException(409, 'This ticket has no unresolved integration decision')
        affected_ids = {str(row.id) for row in affected}
        if action == 'state_intended_behavior' and not body.intended_behavior.strip():
            raise HTTPException(422, 'State the intended combined behavior')
        if action == 'reject_change' and str(body.reject_subtask_id) not in affected_ids:
            raise HTTPException(422, 'Choose one affected sub-task to reject')
        if action in {'resolve_manually', 'replan'}:
            next_replan = max(
                (int(((row.state or {}).get('integration_issue') or {}).get('replan_count', 0))
                 for row in affected), default=0,
            ) + (1 if action == 'replan' else 0)
            for row in affected:
                state = SubtaskState.model_validate(row.state)
                state.integration_issue = {**state.integration_issue, 'resolution': action,
                                           'human_note': body.intended_behavior.strip(),
                                           'replan_count': next_replan}
                state.failure_reason = ('Manual integration resolution requested.' if action == 'resolve_manually'
                                        else 'Re-plan requested; the failed integrated artifact remains blocked.')
                row.state = state.model_dump(mode='json')
            db.commit()
            if action == 'resolve_manually':
                return {'status': 'needs_human', 'action': action}
            if next_replan > settings.max_agent_retries:
                raise HTTPException(
                    409,
                    f'Integration re-plan limit reached ({settings.max_agent_retries}); state the intended behavior or resolve manually',
                )
            # Re-enter through the normal coordinator/Planner path. The failed
            # states remain read-only evidence and can never reach publish.
            from app.main import diagnose_ticket
            return await diagnose_ticket(str(ticket_id))
        rejected = {str(body.reject_subtask_id)} if action == 'reject_change' else set()
        for row in affected:
            state = SubtaskState.model_validate(row.state)
            state.integration_issue = {**state.integration_issue, 'resolution': action,
                                       'human_note': body.intended_behavior.strip()}
            if str(row.id) in rejected:
                state.status = row.status = 'failed'
                state.failure_reason = 'Human rejected this change during integration resolution.'
            else:
                state.status = row.status = 'integration_pending'
                state.failure_reason = None
            row.state = state.model_dump(mode='json')
        ticket = db.get(Ticket, ticket_id)
        if ticket:
            ticket.status = 'processing'
        db.commit()
    from app.core.integration import integrate
    result = await integrate(ticket_id, human_intent=body.intended_behavior.strip() or None,
                             rejected_subtask_ids=rejected)
    return result.model_dump(mode='json')


@router.post('/tickets/{ticket_id}/subtasks/{subtask_id}/verification-credentials')
async def verification_credentials(ticket_id: UUID, subtask_id: UUID, body: VerificationCredentialsBody):
    rows = await asyncio.to_thread(_subtasks, ticket_id, subtask_id)
    state = SubtaskState.model_validate(rows[0]['state'])
    from app.tools.test_preflight import credential_prompt, mark_prompt_offered
    prompt = credential_prompt(str(subtask_id), state.required_test_credentials)
    required = {item['name'] for item in prompt if item['required']}
    allowed = {item['name'] for item in prompt}
    supplied = set(body.credentials)
    if not allowed or state.verifiability != 'unverifiable':
        raise HTTPException(409, 'This subtask is not waiting for test credentials')
    if supplied - allowed:
        raise HTTPException(422, 'Unexpected credential fields: ' + ', '.join(sorted(supplied - allowed)))
    if any(not body.credentials.get(name) for name in required):
        missing = sorted(required - {name for name, value in body.credentials.items() if value})
        raise HTTPException(422, 'Provide all currently required credentials: ' + ', '.join(missing))
    from app.tools.test_runner import ephemeral_test_environment
    resolution = ResolutionBody(escalation_id=body.escalation_id, note='Retry verification with ephemeral credentials')
    mark_prompt_offered(str(subtask_id))
    provided = {name: value for name, value in body.credentials.items() if value}
    with ephemeral_test_environment(provided):
        return await resolve(ticket_id, subtask_id, 'retry', resolution)


@router.post('/tickets/{ticket_id}/subtasks/{subtask_id}/escalation/{action}')
async def resolve(ticket_id: UUID, subtask_id: UUID, action: str, body: ResolutionBody):
    if action not in {'retry', 'reject', 'stay_scope', 'expand_scope', 'resolve_constraints'}:
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
            if recovering and action == 'resolve_constraints':
                if (not state.constraint_resolutions or
                        state.constraint_resolutions[-1].changes != body.constraint_changes or
                        state.constraint_resolution_replan != body.replan):
                    raise HTTPException(409, 'This resolution already has a different decision')
            if any(item.status == 'active' for item in state.constraint_conflicts) and action not in {'resolve_constraints', 'reject'}:
                raise HTTPException(409, 'Resolve the authoritative constraint conflict before retrying')
            if action == 'resolve_constraints' and paused:
                from app.core.constraint_conflicts import resolve_constraint_conflicts
                try:
                    resolve_constraint_conflicts(state.model_copy(deep=True), body.constraint_changes, body.note,
                                                 f'resolution:{subtask_id}:{body.escalation_id}')
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
            await graph.ainvoke(Command(resume={
                'action': action, 'note': body.note,
                'constraint_changes': [item.model_dump() for item in body.constraint_changes],
                'replan': body.replan,
            }) if paused else None, config=config)
            state = SubtaskState.model_validate((await graph.aget_state(config)).values)
            await asyncio.to_thread(persist_state, state)
            return state.model_dump(mode='json')
    except ApprovalConflict as exc:
        raise HTTPException(409, str(exc))
