"""Durable webhook inbox with cross-worker exclusion and bounded recovery."""
import asyncio
import logging
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from psycopg import AsyncConnection

from app.config import settings
from app.db.connection import SessionLocal
from app.db.models import WebhookDelivery, PRLink
from app.tools.github_tool import GitHubTool
from app.tools.jira_tool import JiraTool
from app.core.comment_handling import handle_comment, context_for
from app.core.pr_sync import extract_key, sync_pr, reconcile_prs
from app.events import log_event

logger = logging.getLogger(__name__)


def enqueue(delivery_id, source, event, payload):
    with SessionLocal() as db:
        inserted = db.execute(insert(WebhookDelivery).values(id=delivery_id, source=source, event=event, payload=payload)
                             .on_conflict_do_nothing().returning(WebhookDelivery.id)).scalar_one_or_none()
        db.commit()
        return inserted is not None


def _load(delivery_id):
    with SessionLocal() as db:
        row = db.get(WebhookDelivery, delivery_id)
        if not row or row.status == 'processed' or row.attempts >= 3:
            return None
        row.attempts += 1
        db.commit()
        return row.source, row.event, row.payload


def _finish(delivery_id, error=None):
    with SessionLocal() as db:
        row = db.get(WebhookDelivery, delivery_id)
        row.status = 'failed' if error else 'processed'
        row.error = error
        db.commit()


async def dispatch(source, event, payload, jira=None, github=None):
    jira, github = jira or JiraTool(), github or GitHubTool()
    key = None
    if source == 'github':
        repo = (payload.get('repository') or {}).get('full_name', '')
        pr = payload.get('pull_request')
        if pr and event == 'pull_request':
            key = extract_key(pr.get('title', ''), (pr.get('head') or {}).get('ref', ''))
            def linked_key():
                with SessionLocal() as db:
                    link = db.get(PRLink, str(pr['id']))
                    return link.jira_issue_key if link else None
            key = await asyncio.to_thread(linked_key) or key
            if not key:
                messages = await asyncio.to_thread(github.pr_commit_messages, repo, int(pr['number']))
                key = extract_key(*messages)
            if not key or not key.startswith(settings.jira_project_key + '-'):
                return 'unlinked'
            context = await asyncio.to_thread(context_for, key)
            if context and repo not in (context['repos'] or []):
                return 'unconfirmed_repo'
            # Fetch current truth: old/out-of-order deliveries cannot unmerge or
            # reopen a PR in our cache.
            snapshot = await asyncio.to_thread(github.pr_snapshot, repo, int(pr['number']))
            await sync_pr(key, snapshot, jira, github)
        elif event in {'delete', 'push'}:
            branch = payload.get('ref', '').removeprefix('refs/heads/')
            def links():
                with SessionLocal() as db:
                    return [(r.jira_issue_key, r.number) for r in db.scalars(select(PRLink).where(PRLink.repo == repo, PRLink.branch == branch))]
            for key, number in await asyncio.to_thread(links):
                snapshot = await asyncio.to_thread(github.pr_snapshot, repo, number)
                if event == 'delete' and payload.get('ref_type') == 'branch' and snapshot['state'] != 'merged':
                    snapshot['state'] = 'closed'
                await sync_pr(key, snapshot, jira, github)
        else:
            return 'ignored'
    else:
        key = (payload.get('issue') or {}).get('key')
        if not key or not key.startswith(settings.jira_project_key + '-'):
            return 'out_of_scope'
        if event == 'comment_created':
            return await handle_comment(key, payload.get('comment') or {}, jira)
        if event not in {'jira:issue_updated', 'jira:issue_created'}:
            return 'ignored'
        detail = await asyncio.to_thread(jira.get_issue_detail, key)
        reopened = any(item.get('field') == 'status' and jira.status_category(item.get('fromString', '')) == 'done'
                       for item in (payload.get('changelog') or {}).get('items', []))
        # Both webhook and polling call the same two reconciliation functions.
        await reconcile_prs(jira, key, reopened=reopened)
        from app.core.reconcile import _reconcile_one
        await _reconcile_one(jira, key, detail)
    if key:
        context = await asyncio.to_thread(context_for, key)
        if context:
            await log_event(ticket_id=context['ticket_id'], agent='webhook', stage=event, message=f'{source} {event} reconciled')
    return 'processed'


async def process_delivery(delivery_id):
    async with await AsyncConnection.connect(settings.database_url, autocommit=True) as conn:
        cursor = await conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s, 0))', (f'webhook:{delivery_id}',))
        if not (await cursor.fetchone())[0]:
            return
        item = await asyncio.to_thread(_load, delivery_id)
        if item is None:
            return
        try:
            await dispatch(*item)
        except Exception as exc:
            from app.core.failures import describe_failure
            error = describe_failure('Processing webhook', exc)
            await asyncio.to_thread(_finish, delivery_id, error)
            key = (item[2].get('issue') or {}).get('key')
            if key:
                context = await asyncio.to_thread(context_for, key)
                if context:
                    await log_event(ticket_id=context['ticket_id'], agent='webhook', stage='failed', message=error)
            logger.error('%s', error)
        else:
            await asyncio.to_thread(_finish, delivery_id)


async def recover_deliveries():
    def pending():
        with SessionLocal() as db:
            return list(db.scalars(select(WebhookDelivery.id).where(WebhookDelivery.status != 'processed',
                WebhookDelivery.attempts < 3).order_by(WebhookDelivery.created_at).limit(50)))
    for delivery_id in await asyncio.to_thread(pending):
        await process_delivery(delivery_id)
