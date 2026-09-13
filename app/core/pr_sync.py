"""One PR/Jira transition matrix used by webhooks and polling."""
import asyncio
import logging
from datetime import datetime, timezone
import re
from sqlalchemy import select
from app.db.connection import SessionLocal
from app.db.models import PRLink, Ticket, Subtask, SubtaskMemory
from app.events import log_event
from app.tools.github_tool import GitHubTool
from app.tools.jira_tool import JiraTool

logger = logging.getLogger(__name__)


def extract_key(*texts: str) -> str | None:
    keys = set(re.findall(r'(?i)\b[A-Z]{2,10}-\d+\b', ' '.join(texts)))
    normalized = {key.upper() for key in keys}
    return next(iter(normalized)) if len(normalized) == 1 else None


def record_pr(key, snapshot):
    with SessionLocal() as db:
        row = db.get(PRLink, str(snapshot['id']))
        if row and row.jira_issue_key != key:
            raise ValueError('PR is already linked to another Jira ticket')
        if row is None:
            row = PRLink(github_pr_id=str(snapshot['id']), jira_issue_key=key, repo=snapshot['repo'],
                number=snapshot['number'], branch=snapshot['branch'], url=snapshot['url'],
                pr_state=snapshot['state'])
            db.add(row)
        db.commit()


def _sync(key, snapshot, jira, github, reopened=False):
    completed_states = []
    record_pr(key, snapshot)
    with SessionLocal() as db:
        row = db.scalar(select(PRLink).where(PRLink.github_pr_id == str(snapshot['id'])).with_for_update())
        if row.pr_state == 'merged':
            snapshot['state'] = 'merged'  # merged history is immutable, even on stale delivery
        detail = jira.get_issue_detail(key)
        category = detail.get('status_category')
        previous_category = row.jira_category
        messages = []
        transitioned_done = False
        local = db.scalar(select(Ticket).where(Ticket.external_key == key))
        state = snapshot['state']
        reopened = reopened or (previous_category == 'done' and category in {'new', 'indeterminate'})
        if reopened:
            jira.set_status(key, 'in_progress')
            if state == 'merged':
                messages.append(f"Ticket reopened, but PR #{row.number} already merged — a new PR/branch is needed.")
            elif state == 'closed':
                messages.append(f"Ticket reopened; PR #{row.number} remains closed without merging.")
            else:
                github.comment_pr(row.repo, row.number, 'Ticket moved back to In Progress')
                messages.append(f"Ticket reopened; PR #{row.number} is still open.")
        if row.notified_state != state:
            if state == 'merged':
                messages.append(f"PR #{row.number} merged: {row.url}")
                if not reopened:
                    transitioned_done = bool(jira.set_status(key, 'done').get('applied'))
                    if local:
                        local.status, local.claimed_at = 'done', None
            elif state == 'open':
                messages.append(f"PR #{row.number} opened: {row.url}")
                if not reopened:
                    jira.set_status(key, 'in_review')
            else:
                messages.append(f"PR #{row.number} closed without merge / branch abandoned: {row.url}")
        for message in messages:
            jira.comment(key, message)
        row.pr_state, row.notified_state = state, state
        row.jira_category = 'done' if transitioned_done else category
        row.updated_at = datetime.now(timezone.utc)
        if local and state == 'closed' and local.status == 'in_review':
            local.status = 'needs_human'
        if local and state in {'merged', 'closed'}:
            for task in db.scalars(select(Subtask).where(Subtask.ticket_id == local.id, Subtask.status == 'in_review')):
                if (task.state or {}).get('pr_url') == row.url:
                    task.status = 'done' if state == 'merged' else 'needs_human'
                    task.state = {**task.state, 'status': task.status,
                                  'failure_reason': None if state == 'merged' else 'PR closed without merge'}
                    if state == 'merged':
                        completed_states.append(task.state)
        db.commit()
        ticket_id = str(local.id) if local else None
    # The PR-open path normally wrote this already. A merge retries only when that
    # non-critical write failed, preserving one summarization call per resolution.
    for raw_state in completed_states:
        try:
            with SessionLocal() as db:
                exists = db.scalar(select(SubtaskMemory.id).where(
                    SubtaskMemory.subtask_id == raw_state['subtask_id']))
            if not exists:
                from app.agents.state import SubtaskState
                from app.memory.store import write_resolution
                write_resolution(SubtaskState.model_validate(raw_state), 'success')
        except Exception:
            logger.warning('Could not write done-state memory for %s', raw_state.get('subtask_id'), exc_info=True)
    return ticket_id, messages


async def sync_pr(key, snapshot, jira=None, github=None, *, reopened=False):
    ticket_id, messages = await asyncio.to_thread(_sync, key, snapshot, jira or JiraTool(), github or GitHubTool(), reopened)
    if ticket_id:
        for message in messages:
            await log_event(ticket_id=ticket_id, agent='pr_sync', stage='pr_state', message=message)
        if snapshot.get('state') in {'merged', 'closed'}:
            # Closing/merging lifts R-53's one-open-PR gate. Any already-tested
            # integration result can now publish its next PR without more LLM work.
            from app.orchestrator.scheduler import advance_ticket
            await advance_ticket(ticket_id)


def links_for(key=None):
    with SessionLocal() as db:
        query = select(PRLink)
        if key:
            query = query.where(PRLink.jira_issue_key == key)
        return [{'key': row.jira_issue_key, 'repo': row.repo, 'number': row.number} for row in db.scalars(query)]


async def reconcile_prs(jira=None, key=None, reopened=False):
    github = GitHubTool()
    for link in await asyncio.to_thread(links_for, key):
        snapshot = await asyncio.to_thread(github.pr_snapshot, link['repo'], link['number'])
        await sync_pr(link['key'], snapshot, jira, github, reopened=reopened)
