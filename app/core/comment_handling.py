"""Permission check and deterministic routing around the comment reasoning agent."""
import asyncio
import re
from uuid import UUID
from sqlalchemy import select

from app.agents.comment_monitor import CommentMonitorAgent
from app.agents.planning import ApprovalDecision
from app.agents.llm import LLMClient
from app.config import settings
from app.core.approvals import pending_for_ticket
from app.core.proposals import proposal_record, run_proposal, get_proposal
from app.db.connection import SessionLocal
from app.db.models import Ticket, Subtask
from app.events import log_event


def context_for(key):
    with SessionLocal() as db:
        ticket = db.scalar(select(Ticket).where(Ticket.external_key == key))
        if not ticket:
            return None
        latest = db.scalar(select(Subtask).where(Subtask.ticket_id == ticket.id).order_by(Subtask.created_at.desc()))
        return {'ticket_id': str(ticket.id), 'title': ticket.title, 'description': ticket.description,
                'status': ticket.status, 'repos': ticket.repos,
                'subtask_id': str(latest.id) if latest else None,
                'state': {k: v for k, v in (latest.state or {}).items() if k in
                    {'diagnosis', 'plan', 'current_step', 'status', 'failure_reason', 'pr_url'}} if latest else {}}


async def decide_registered(approval_id, decision):
    from app.web.approval import _decide
    record = await asyncio.to_thread(get_proposal, approval_id)
    if record['status'] == 'EXPIRED':
        raise ValueError('This approval is expired')
    if record['subtask_id']:
        return await _decide(UUID(record['ticket_id']), UUID(record['subtask_id']), decision)
    return await run_proposal(approval_id, decision)


async def handle_comment(key, comment, jira, monitor=None):
    author = comment.get('author') or {}
    account_id = author.get('accountId')
    own_id = await asyncio.to_thread(jira.own_account_id)
    if not account_id or account_id == own_id or author.get('accountType') == 'app':
        return 'ignored_self'
    context = await asyncio.to_thread(context_for, key)
    if not context:
        return 'untracked'
    detail = await asyncio.to_thread(jira.get_issue_detail, key)
    if account_id != detail.get('assignee_id') and account_id not in settings.jira_approval_account_ids:
        await asyncio.to_thread(jira.comment, key, "You're not authorised to approve or direct this ticket.")
        return 'unauthorized'
    text = jira.extract_plain_text(comment.get('body')).strip()
    match = re.match(r'^(APPROVE|REJECT)\b(?:\s+([0-9a-fA-F-]{36}))?(?:\s+(.*))?$', text, re.I | re.S)
    if match:
        action, gate_id, note = match.groups()
        gates = await asyncio.to_thread(pending_for_ticket, context['ticket_id'])
        if gate_id:
            record = await asyncio.to_thread(get_proposal, gate_id)
            if record['ticket_id'] != context['ticket_id']:
                raise ValueError('Approval belongs to another ticket')
        elif len(gates) == 1:
            gate_id = gates[0]['id']
        else:
            await asyncio.to_thread(jira.comment, key, 'Please include the approval ID from the request; there is no unique pending gate.')
            return 'clarify_gate'
        decision = ApprovalDecision(approval_status='approved' if action.upper() == 'APPROVE' else 'rejected',
                                    note=note or ('Rejected from Jira' if action.upper() == 'REJECT' else ''))
        await decide_registered(gate_id, decision)
        await asyncio.to_thread(jira.comment, key, f"{author.get('displayName', account_id)} {decision.approval_status} approval {gate_id}.")
        return decision.approval_status
    if context.get('subtask_id'):
        from app.orchestrator.graph import open_graph, thread_config
        async with open_graph(context['ticket_id'], context['subtask_id']) as graph:
            snapshot = await graph.aget_state(thread_config(context['ticket_id'], context['subtask_id']))
        if snapshot.values:
            context['state'] = {key: value for key, value in snapshot.values.items() if key in
                {'diagnosis', 'plan', 'current_step', 'status', 'failure_reason', 'pr_url'}}
    usage = LLMClient.get_usage(context['ticket_id'])
    if usage['calls'] >= settings.ticket_call_budget or usage['est_cost_usd'] >= settings.ticket_cost_budget_usd:
        await asyncio.to_thread(jira.comment, key, 'Comment classification paused: ticket model budget exhausted. Please use the UI.')
        return 'budget_exhausted'
    # R-47: the ticket is a two-way chat, not isolated replies — give the model the
    # recent exchange (both sides) so it reads as a continuation, never new
    # instructions by itself; only the comment below is classified/acted on now.
    thread = await asyncio.to_thread(jira.recent_comments, key)
    context['thread'] = [item for item in thread if item.get('id') != str(comment.get('id'))]
    try:
        intent = await asyncio.to_thread((monitor or CommentMonitorAgent()).run, context['ticket_id'], text, context)
    except Exception:
        await asyncio.to_thread(jira.comment, key, 'Could you clarify the requested action or question? I could not classify this comment reliably.')
        return 'ambiguous'
    if intent.intent == 'COMMAND':
        gate_id = await asyncio.to_thread(proposal_record, context['ticket_id'], key, str(comment['id']), intent)
        await run_proposal(gate_id)
        await asyncio.to_thread(jira.comment, key, f"Proposed action: {intent.proposal}\nReply APPROVE {gate_id} or REJECT {gate_id} <note>. No command has been applied.")
    elif intent.intent in {'QUESTION', 'AMBIGUOUS'}:
        await asyncio.to_thread(jira.comment, key, intent.response)
    await log_event(ticket_id=context['ticket_id'], agent='comment_monitor', stage='comment_classified',
                    message=f"{intent.intent}: {'proposal awaiting approval' if intent.intent == 'COMMAND' else 'answered' if intent.intent == 'QUESTION' else 'clarification requested' if intent.intent == 'AMBIGUOUS' else 'ignored'}")
    return intent.intent
