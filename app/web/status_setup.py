"""Project status setup, available on first run and from settings."""
import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from app import events
from app.config import settings
from app.core.status_map import MEANINGS, StatusMap, load_map, save_map, suggest
from app.tools.jira_tool import JiraTool

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / 'templates')


@router.get('/settings/statuses')
def setup_page(request: Request):
    return templates.TemplateResponse('status_setup.html', {'request': request, 'project': settings.jira_project_key})


@router.get('/settings/statuses/data')
def setup_data():
    saved = load_map(settings.jira_project_key)
    error = None
    try:
        statuses = JiraTool().project_status_rows()
    except Exception:
        statuses, error = [], 'Could not fetch Jira statuses. Check Jira configuration and retry.'
    for status in statuses:
        status['meaning'], status['low_confidence'] = suggest(status['name'], status['category'])
    return {'project': settings.jira_project_key, 'statuses': statuses, 'meanings': MEANINGS,
            'saved': saved.model_dump() if saved is not None else None, 'error': error}


@router.post('/settings/statuses')
async def save_statuses(body: StatusMap):
    try:
        actual = {row['id']: row['name'] for row in await asyncio.to_thread(JiraTool().project_status_rows)}
    except Exception:
        raise HTTPException(503, 'Cannot verify Jira statuses. Retry when Jira is available.')
    if any(actual.get(row.id) != row.name for row in body.rows):
        raise HTTPException(422, 'A status was renamed or does not belong to this project. Reload the Jira statuses.')
    await asyncio.to_thread(save_map, settings.jira_project_key, body)
    await events.publish('status-setup:' + settings.jira_project_key,
                         events.make_event('settings', 'status_map_saved', 'Project status map saved',
                                           project=settings.jira_project_key, rows=body.model_dump()['rows']))
    return {'saved': body.model_dump(), 'next': '/'}


@router.get('/settings/statuses/events')
async def setup_events(request: Request):
    async def stream():
        async with events.subscribe('status-setup:' + settings.jira_project_key) as queue:
            while not await request.is_disconnected():
                try:
                    item = await asyncio.wait_for(queue.get(), 15)
                    yield {'event': 'status_map_saved', 'data': json.dumps(item)}
                except asyncio.TimeoutError:
                    yield {'comment': 'keepalive'}
    return EventSourceResponse(stream())
