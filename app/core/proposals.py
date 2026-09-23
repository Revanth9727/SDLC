"""Commands are checkpointed proposals. Only a resumed approval applies them."""
import asyncio
from datetime import datetime, timezone
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import StateGraph, END
from langgraph.types import interrupt, Command
from sqlalchemy import select

from app.agents.planning import ApprovalDecision
from app.agents.state import SubtaskState
from app.agents.constraints import ExecutionConstraint
from app.config import settings
from app.core.subtasks import prepare_subtask, ACTIVE_SUBTASK_STATUSES
from app.core.approvals import _expire_older_pending
from app.db.connection import SessionLocal
from app.db.models import PendingApproval, Ticket, Subtask
from app.events import log_event


def proposal_record(ticket_id, key, comment_id, intent):
    with SessionLocal() as db:
        row = db.scalar(select(PendingApproval).where(PendingApproval.source_comment_id == comment_id))
        if row is None:
            _expire_older_pending(db, ticket_id)
            row = PendingApproval(ticket_id=UUID(ticket_id), jira_issue_key=key, source_comment_id=comment_id,
                proposed_action={'kind': 'command', 'action': intent.action, 'description': intent.proposal})
            db.add(row)
            db.commit()
            db.refresh(row)
        return str(row.id)


def get_proposal(approval_id):
    with SessionLocal() as db:
        row = db.get(PendingApproval, UUID(str(approval_id)))
        if not row:
            raise LookupError('Approval not found')
        return {'id': str(row.id), 'ticket_id': str(row.ticket_id), 'key': row.jira_issue_key,
                'subtask_id': str(row.subtask_id) if row.subtask_id else None,
                'status': row.status, 'action': row.proposed_action, 'note': row.decision_note,
                'source_comment_id': row.source_comment_id}


def _finish(approval_id, decision):
    with SessionLocal() as db:
        row = db.get(PendingApproval, UUID(approval_id))
        row.status = decision.approval_status.upper()
        row.decision_note = decision.note
        row.resolved_at = datetime.now(timezone.utc)
        db.commit()


def _stop(ticket_id):
    with SessionLocal() as db:
        ticket = db.get(Ticket, UUID(ticket_id))
        ticket.status, ticket.claimed_at = 'needs_human', None
        ids = []
        for task in db.scalars(select(Subtask).where(Subtask.ticket_id == ticket.id, Subtask.status.in_(ACTIVE_SUBTASK_STATUSES))):
            task.status = 'superseded'
            ids.append(str(task.id))
        for gate in db.scalars(select(PendingApproval).where(PendingApproval.ticket_id == ticket.id,
                PendingApproval.subtask_id.is_not(None), PendingApproval.status == 'PENDING')):
            gate.status = 'EXPIRED'
            gate.resolved_at = datetime.now(timezone.utc)
        db.commit()
        return ids


def _new_state(record):
    # New work receives another coding-plan approval before it can edit anything.
    with SessionLocal() as db:
        ticket = db.get(Ticket, UUID(record['ticket_id']))
        if not ticket.repos:
            raise ValueError('Confirm a repository before replanning')
        description = ticket.description + '\n\nApproved requested revision: ' + record['action']['description']
        repo, key = ticket.repos[0], ticket.external_key
        ticket.status = 'processing'
        db.commit()
    replacement = prepare_subtask(record['ticket_id'], description=description, reuse_fresh_pending=True)
    state = SubtaskState(ticket_id=record['ticket_id'], subtask_id=replacement.subtask_id,
                        jira_key=key, subtask_type='bug', description=description, repo=repo,
                        prior_attempt=replacement.prior_attempt)
    # A replacement work identity may inherit ticket intent, but not the old
    # step IDs or sibling implementation details. The next plan is gated again.
    for raw in (replacement.prior_attempt or {}).get('execution_constraints', []):
        item = ExecutionConstraint.model_validate(raw)
        if item.constraint_id in (replacement.prior_attempt or {}).get("withdrawn_constraint_ids", []):
            continue
        if item.scope_type == 'ticket' and item.scope_value == state.ticket_id:
            state.execution_constraints.append(item)
        elif ((replacement.prior_attempt or {}).get('repo') == state.repo
              and (replacement.prior_attempt or {}).get('orchestration_role') != 'coordinator'):
            if (item.scope_type == 'subtask' and item.scope_value ==
                    replacement.prior_attempt.get('source_subtask_id')):
                state.pending_execution_constraints.append(item.model_copy(update={'scope_value': state.subtask_id}))
            elif item.scope_type in {'file', 'symbol'}:
                state.pending_execution_constraints.append(item)
            # Old step IDs do not identify steps in a new work identity. Their
            # records remain in prior_attempt for review, not active execution.
    decision = ApprovalDecision.model_validate(record.get('decision') or {
        'approval_status': 'approved', 'note': record.get('note') or '',
    })
    provenance = f"proposal:{record['id']}:jira-comment:{record.get('source_comment_id') or 'unknown'}"
    state.execution_constraints.append(ExecutionConstraint(
        source='human_approval_note', text=record['action']['description'],
        scope_type='subtask', scope_value=state.subtask_id, provenance=provenance,
    ))
    if decision.note:
        state.execution_constraints.append(ExecutionConstraint(
            source='human_approval_note', text=decision.note,
            scope_type='subtask', scope_value=state.subtask_id,
            provenance=decision.provenance or provenance,
        ))
    return state.model_dump(mode='json')


def proposal_graph(saver):
    def gate(raw):
        decision = ApprovalDecision.model_validate(interrupt(raw['action']))
        return {**raw, 'decision': decision.model_dump()}

    async def prepare(raw):
        decision = ApprovalDecision.model_validate(raw['decision'])
        if decision.approval_status == 'approved' and raw['action']['action'] == 'replan':
            state = await asyncio.to_thread(_new_state, raw)
            return {**raw, 'new_state': state}
        return raw

    async def apply(raw):
        from app.orchestrator.graph import run_diagnosis_graph
        from app.web.approval import persist_state
        from app.tools.jira_tool import JiraTool
        decision = ApprovalDecision.model_validate(raw['decision'])
        if decision.approval_status == 'approved':
            if raw['action']['action'] == 'stop':
                stopped = await asyncio.to_thread(_stop, raw['ticket_id'])
                await cleanup_stopped(raw['ticket_id'], stopped or [])
                await asyncio.to_thread(JiraTool().set_status, raw['key'], 'blocked')
            else:
                state = await run_diagnosis_graph(SubtaskState.model_validate(raw['new_state']))
                await asyncio.to_thread(persist_state, state)
        await asyncio.to_thread(_finish, raw['id'], decision)
        await log_event(ticket_id=raw['ticket_id'], agent='comment_monitor', stage='proposal_resolved',
                        message=f"Command proposal {decision.approval_status}: {raw['action']['description']}")
        return {**raw, 'complete': True}

    graph = StateGraph(dict)
    graph.add_node('gate', gate)
    graph.add_node('prepare', prepare)
    graph.add_node('apply', apply)
    graph.set_entry_point('gate')
    graph.add_edge('gate', 'prepare')
    graph.add_edge('prepare', 'apply')
    graph.add_edge('apply', END)
    return graph.compile(checkpointer=saver)


async def run_proposal(approval_id, decision=None):
    from app.orchestrator.graph import ApprovalConflict
    record = await asyncio.to_thread(get_proposal, approval_id)
    async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
        await saver.setup()
        cursor = await saver.conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked',
                                          (f"proposal:{approval_id}",))
        if not (await cursor.fetchone())['locked']:
            raise ApprovalConflict('Proposal is already resuming')
        config = {'configurable': {'thread_id': f'proposal:{approval_id}'}}
        graph = proposal_graph(saver)
        snapshot = await graph.aget_state(config)
        if not snapshot.values:
            await graph.ainvoke(record, config)
            snapshot = await graph.aget_state(config)
        if decision:
            previous = snapshot.values.get('decision')
            if previous and previous != decision.model_dump():
                raise ApprovalConflict('Proposal already has another decision')
            if previous:
                if snapshot.next:
                    await graph.ainvoke(None, config)
            else:
                await graph.ainvoke(Command(resume=decision.model_dump()), config)
        return (await graph.aget_state(config)).values


async def cleanup_stopped(ticket_id, subtask_ids):
    from psycopg import AsyncConnection
    from app.tools.repo_tool import RepoTool
    for subtask_id in subtask_ids:
        async with await AsyncConnection.connect(settings.database_url, autocommit=True) as conn:
            cursor = await conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s, 0))',
                                        (f'{ticket_id}:{subtask_id}',))
            if (await cursor.fetchone())[0]:
                await asyncio.to_thread(RepoTool().cleanup_workspace, subtask_id)
            # An executing graph owns its workspace until the next boundary,
            # where require_active routes it to escalation and cleanup.
