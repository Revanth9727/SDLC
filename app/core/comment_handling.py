"""Permission check and deterministic routing around the comment reasoning agent."""
import asyncio
import logging
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
from app.core.jira_comments import is_app_comment

logger = logging.getLogger(__name__)
_GATE_ID_RE = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')


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
        logger.info("jira.comment approval_resume gate_id=%r kind=plan subtask_id=%r",
                    str(approval_id), record['subtask_id'])
        return await _decide(UUID(record['ticket_id']), UUID(record['subtask_id']), decision)
    logger.info("jira.comment approval_resume gate_id=%r kind=proposal", str(approval_id))
    return await run_proposal(approval_id, decision)


async def handle_comment(key, comment, jira, monitor=None):
    author = comment.get('author') or {}
    account_id = author.get('accountId')
    comment_id = str(comment.get('id') or '')
    logger.info("jira.comment received key=%r comment_id=%r author_id=%r account_type=%r",
                key, comment_id, account_id, author.get('accountType'))
    posted_by_app = await asyncio.to_thread(is_app_comment, comment_id)
    if not account_id or posted_by_app or author.get('accountType') == 'app':
        logger.info("jira.comment ignored key=%r comment_id=%r reason=%s", key, comment_id,
                    'missing_author' if not account_id else 'app_comment_id' if posted_by_app else 'app_account')
        return 'ignored_self'
    context = await asyncio.to_thread(context_for, key)
    if not context:
        logger.info("jira.comment ignored key=%r comment_id=%r reason=untracked_ticket", key, comment_id)
        return 'untracked'
    text = jira.extract_plain_text(comment.get('body')).strip()
    # R-53: this deterministic gate precedes comment classification, so an
    # existing PR consumes zero model calls until an authorised human decides.
    from app.core.publish import preflight, redo
    existing_pr = await preflight(context['ticket_id'], github=None, notify=False)
    if existing_pr:
        detail = await asyncio.to_thread(jira.get_issue_detail, key)
        authorized = account_id == detail.get('assignee_id') or account_id in settings.jira_approval_account_ids
        if not authorized:
            await asyncio.to_thread(jira.comment, key, "You're not authorised to replace this ticket's PR.")
            return 'unauthorized'
        normalized = re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()
        if normalized in {'redo pr', 'replace pr', 'replace the pr', 'redo the pr'}:
            urls = await redo(context['ticket_id'], jira=jira)
            await asyncio.to_thread(jira.comment, key, f"Replacement PR created: {urls[0]}")
            return 'pr_replaced'
        if normalized in {'keep', 'keep pr', 'keep the pr'}:
            await asyncio.to_thread(jira.comment, key, f"Keeping PR #{existing_pr.number}: {existing_pr.url}")
            await log_event(ticket_id=context['ticket_id'], agent='publish', stage='pr_kept',
                            message=f"Keeping PR #{existing_pr.number}: {existing_pr.url}")
            return 'pr_kept'
        await preflight(context['ticket_id'], jira=jira, notify=True)
        return 'open_pr_requires_decision'
    if context.get('subtask_id'):
        from app.orchestrator.graph import open_graph, thread_config
        async with open_graph(context['ticket_id'], context['subtask_id']) as graph:
            snapshot = await graph.aget_state(thread_config(context['ticket_id'], context['subtask_id']))
        if snapshot.values:
            context['state'] = {key: value for key, value in snapshot.values.items() if key in
                {'diagnosis', 'plan', 'current_step', 'status', 'failure_reason', 'pr_url'}}
    from app.core.budget import usage as durable_usage, budget_reason
    usage = await asyncio.to_thread(LLMClient.get_usage, context['ticket_id'])
    saved = await asyncio.to_thread(durable_usage, context['ticket_id'])
    if budget_reason(usage, saved.get('limits') if saved else None):
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
        logger.exception("jira.comment classification_failed key=%r comment_id=%r", key, comment_id)
        await asyncio.to_thread(jira.comment, key, 'Could you clarify the requested action or question? I could not classify this comment reliably.')
        return 'ambiguous'
    logger.info("jira.comment classified key=%r comment_id=%r intent=%s tier=cheap", key, comment_id, intent.intent)
    detail = await asyncio.to_thread(jira.get_issue_detail, key)
    is_assignee = account_id == detail.get('assignee_id')
    is_allowlisted = account_id in settings.jira_approval_account_ids
    authorized = is_assignee or is_allowlisted
    no_approver_configured = not detail.get('assignee_id') and not settings.jira_approval_account_ids
    logger.info("jira.comment authorization key=%r comment_id=%r authorized=%s assignee=%s allowlisted=%s",
                key, comment_id, authorized, is_assignee, is_allowlisted)
    if not authorized:
        if no_approver_configured:
            message = ('No authorized approver configured: assign this Jira ticket or set '
                       'JIRA_APPROVAL_ACCOUNT_IDS in the app .env.')
            outcome = 'no_authorized_approver'
        else:
            message = "You're not authorised to approve or direct this ticket."
            outcome = 'unauthorized'
        await asyncio.to_thread(jira.comment, key, message)
        logger.info("jira.comment action key=%r comment_id=%r action=%s", key, comment_id, outcome)
        return outcome
    if intent.intent in {'APPROVE', 'REJECT', 'REVISE'}:
        gates = await asyncio.to_thread(pending_for_ticket, context['ticket_id'])
        logger.info("jira.comment pending_gates key=%r comment_id=%r count=%d", key, comment_id, len(gates))
        explicit_id = _GATE_ID_RE.search(text)
        gate_id = intent.gate_id or (explicit_id.group(0) if explicit_id else None)
        if gate_id:
            try:
                record = await asyncio.to_thread(get_proposal, gate_id)
            except (LookupError, ValueError):
                record = None
            if not record or record['ticket_id'] != context['ticket_id'] or record['status'] != 'PENDING':
                logger.warning("jira.comment gate_mismatch key=%r comment_id=%r gate_id=%r", key, comment_id, gate_id)
                await asyncio.to_thread(jira.comment, key, 'That approval is no longer current for this ticket.')
                return 'clarify_gate'
        elif len(gates) == 1:
            gate_id = gates[0]['id']
        else:
            message = ('There is no current approval waiting for this ticket.' if not gates else
                       'Multiple sub-tasks are waiting for approval. Open the ticket and approve the intended sub-task.')
            await asyncio.to_thread(jira.comment, key, message)
            logger.info("jira.comment action key=%r comment_id=%r action=clarify_gate count=%d",
                        key, comment_id, len(gates))
            return 'clarify_gate'
        logger.info("jira.comment gate_matched key=%r comment_id=%r gate_id=%r", key, comment_id, gate_id)
        decision = ApprovalDecision(
            approval_status='approved' if intent.intent == 'APPROVE' else 'rejected',
            note=intent.feedback,
        )
        result = await decide_registered(gate_id, decision)
        resumed = decision.approval_status == 'approved' or bool(decision.note)
        action = 'resumed' if resumed else 'feedback_requested'
        logger.info("jira.comment action key=%r comment_id=%r action=%s gate_id=%r", key, comment_id, action, gate_id)
        if resumed:
            decision_word = 'approved' if intent.intent == 'APPROVE' else 'requested a revision to'
            await asyncio.to_thread(jira.comment, key,
                f"{author.get('displayName', account_id)} {decision_word} the current plan.")
        else:
            await asyncio.to_thread(jira.comment, key, (result or {}).get('message', 'What should change?'))
        return 'approved' if intent.intent == 'APPROVE' else 'replanning' if resumed else 'feedback_requested'
    if intent.intent == 'COMMAND':
        gate_id = await asyncio.to_thread(proposal_record, context['ticket_id'], key, str(comment['id']), intent)
        await run_proposal(gate_id)
        await asyncio.to_thread(jira.comment, key, f"Proposed action: {intent.proposal}\nReply approve or reject, or describe a change you want. No command has been applied.")
        logger.info("jira.comment action key=%r comment_id=%r action=proposal_created gate_id=%r",
                    key, comment_id, gate_id)
    elif intent.intent in {'QUESTION', 'AMBIGUOUS'}:
        await asyncio.to_thread(jira.comment, key, intent.response)
        logger.info("jira.comment action key=%r comment_id=%r action=%s",
                    key, comment_id, 'answered' if intent.intent == 'QUESTION' else 'clarification_requested')
    else:
        logger.info("jira.comment action key=%r comment_id=%r action=ignored_chatter", key, comment_id)
    await log_event(ticket_id=context['ticket_id'], agent='comment_monitor', stage='comment_classified',
                    message=f"{intent.intent}: {'proposal awaiting approval' if intent.intent == 'COMMAND' else 'answered' if intent.intent == 'QUESTION' else 'clarification requested' if intent.intent == 'AMBIGUOUS' else 'ignored'}")
    return intent.intent
