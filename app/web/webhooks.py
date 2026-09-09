"""Authenticate raw webhook bytes before storing or acting on their contents."""
import hashlib
import hmac
import json
import asyncio
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from app.config import settings
from app.core.webhook_processing import enqueue, process_delivery

router = APIRouter()


def verify_signature(body: bytes, header: str, secret: str):
    if not secret:
        raise HTTPException(503, 'Webhook secret is not configured')
    expected = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), header.encode()):
        raise HTTPException(401, 'Invalid webhook signature')


def verify_jira_secret(provided: str, configured: str):
    if not configured:
        raise HTTPException(503, 'Webhook secret is not configured')
    if not provided or not hmac.compare_digest(provided.encode(), configured.encode()):
        raise HTTPException(401, 'Invalid Jira webhook secret')


async def receive(source, request, background):
    # Jira Cloud UI webhooks do not sign request bodies. Authenticate their
    # callback URL before reading or storing the payload. Unrelated Jira query
    # parameters (for example triggeredByUser) remain supported.
    if source == 'jira':
        verify_jira_secret(request.query_params.get('secret', ''), settings.jira_webhook_secret)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 2_000_000:
            raise HTTPException(413, 'Webhook payload is too large')
    if source == 'github':
        signature = request.headers.get('x-hub-signature-256', '')
        verify_signature(bytes(body), signature, settings.github_webhook_secret)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'Invalid JSON')
    if not isinstance(payload, dict):
        raise HTTPException(400, 'Webhook payload must be an object')
    event = request.headers.get('x-github-event', '') if source == 'github' else payload.get('webhookEvent', '')
    delivery = request.headers.get('x-github-delivery' if source == 'github' else 'x-atlassian-webhook-identifier')
    if not event or len(event) > 64:
        raise HTTPException(400, 'A valid event is required')
    # Jira may retry a comment with another webhook delivery ID. Its comment ID
    # is the stable semantic delivery/dedupe key; no Jira-specific header is
    # required for comment_created.
    if source == 'jira' and event == 'comment_created':
        comment_id = (payload.get('comment') or {}).get('id')
        if not comment_id:
            raise HTTPException(400, 'Comment ID is required')
        delivery = f'comment:{comment_id}'
    elif not delivery or len(delivery) > 180:
        raise HTTPException(400, 'A valid delivery identifier is required')
    delivery_id = f'{source}:{delivery}'
    inserted = await asyncio.to_thread(enqueue, delivery_id, source, event, payload)
    if inserted:
        background.add_task(process_delivery, delivery_id)
    return {'accepted': True, 'duplicate': not inserted}


@router.post('/webhooks/github')
async def github_webhook(request: Request, background: BackgroundTasks):
    return await receive('github', request, background)


@router.post('/webhooks/jira')
async def jira_webhook(request: Request, background: BackgroundTasks):
    return await receive('jira', request, background)
