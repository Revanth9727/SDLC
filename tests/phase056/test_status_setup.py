import uuid
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.core import status_map, status_events
from app.core.status_map import StatusMap, suggest
from app.db.connection import engine, SessionLocal
from app.db.models import ProjectStatusMap
from app.tools.jira_tool import JiraTool
from app.web import status_setup
from app.main import app


@pytest.fixture
def project(monkeypatch):
    ProjectStatusMap.__table__.create(engine, checkfirst=True)
    project = 'TEST-' + uuid.uuid4().hex
    monkeypatch.setattr(status_setup.settings, 'jira_project_key', project)
    yield project
    with SessionLocal() as db:
        row = db.get(ProjectStatusMap, project)
        if row:
            db.delete(row)
            db.commit()


def test_first_run_save_reload_and_project_isolation(project, monkeypatch):
    statuses = [{'id': '99', 'name': 'Peer inspection', 'category': 'indeterminate'}]
    monkeypatch.setattr(JiraTool, 'project_status_rows', lambda _: statuses)
    client = TestClient(app)
    assert client.get('/', follow_redirects=False).headers['location'] == '/settings/statuses'
    assert 'Jira statuses' in client.get('/settings/statuses').text
    data = client.get('/settings/statuses/data').json()
    assert data['saved'] is None
    assert data['statuses'][0]['low_confidence']
    body = {'rows': [{'id': '99', 'name': 'Peer inspection', 'meaning': 'in-review'}]}
    assert client.post('/settings/statuses', json=body).status_code == 200
    assert client.get('/settings/statuses/data').json()['saved'] == body
    assert status_map.load_map(project).model_dump() == body
    assert status_map.load_map(project + '-other') is None
    assert client.get('/', follow_redirects=False).status_code == 200
    assert client.post('/settings/statuses', json={'rows': [dict(body['rows'][0], id='bad')]}).status_code == 422
    assert client.post('/settings/statuses', json={'rows': body['rows'] * 2}).status_code == 422


@pytest.fixture
def jira(monkeypatch):
    tool = JiraTool()
    monkeypatch.setattr(status_map, 'load_map', lambda _: None)
    monkeypatch.setattr(status_events, 'emit_transition', Mock())
    monkeypatch.setattr(tool, 'get_issue', lambda _: {'status': 'Queued', 'raw_fields': {'status': {'id': '1'}}})
    monkeypatch.setattr(tool, 'get_transitions', lambda _: [
        {'id': 't2', 'status_id': '2', 'name': 'Building', 'category': 'indeterminate'},
        {'id': 't3', 'status_id': '3', 'name': 'Peer inspection', 'category': 'indeterminate'},
    ])
    tool.comment = Mock()
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(tool, '_client', lambda: client)
    return tool, client


@pytest.mark.parametrize('stage,meaning', status_map.STAGE_MEANING.items())
def test_declared_id_wins_at_every_stage(jira, monkeypatch, stage, meaning):
    tool, client = jira
    mapping = StatusMap(rows=[{'id': '3', 'name': 'Old name', 'meaning': meaning}])
    monkeypatch.setattr(status_map, 'load_map', lambda _: mapping)
    result = tool.set_status('TEST-1', stage)
    assert result['layer'] == 'map' and result['applied']
    assert result['meaning'] == meaning and result['to'] == 'Peer inspection'
    assert client.post.call_args.kwargs['json'] == {'transition': {'id': 't3'}}
    status_events.emit_transition.assert_called_once_with('TEST-1', result)


def test_fallback_comment_and_unreachable_declaration(jira, monkeypatch):
    tool, client = jira
    assert tool.set_status('TEST-1', 'in_review')['layer'] == 'category'
    client.post.reset_mock()
    monkeypatch.setattr(tool, 'get_transitions', lambda _: [])
    result = tool.set_status('TEST-1', 'in_review')
    assert result['layer'] == 'comment-and-leave' and not result['applied']
    tool.comment.assert_called_once_with('TEST-1', "couldn't move to in-review — no matching status")
    client.post.assert_not_called()
    monkeypatch.setattr(status_map, 'load_map', lambda _: StatusMap(rows=[{'id': '99', 'name': 'Unavailable', 'meaning': 'in-review'}]))
    result = tool.set_status('TEST-1', 'in_review')
    assert result['layer'] == 'map' and not result['applied']
    assert 'not reachable' in result['reason']
    client.post.assert_not_called()


def test_transition_and_comment_failures_are_nonblocking(jira, monkeypatch):
    tool, client = jira
    client.post.side_effect = RuntimeError('offline')
    assert not tool.set_status('TEST-1', 'in_review')['applied']
    monkeypatch.setattr(tool, 'get_transitions', lambda _: [])
    tool.comment.side_effect = RuntimeError('offline')
    assert 'could not be posted' in tool.set_status('TEST-1', 'in_review')['reason']


def test_suggestions():
    assert suggest('Custom ready', 'new') == ('ready-to-pick-up', False)
    assert suggest('Awaiting approval', 'indeterminate')[0] == 'awaiting-approval'
    assert suggest('Something unfamiliar', None)[1]


@pytest.mark.asyncio
async def test_save_emits_event(project, monkeypatch):
    from app import events
    monkeypatch.setattr(JiraTool, 'project_status_rows', lambda _: [])
    async with events.subscribe('status-setup:' + project) as queue:
        await status_setup.save_statuses(StatusMap(rows=[]))
        event = queue.get_nowait()
    assert event['stage'] == 'status_map_saved'
    assert event['project'] == project
    assert status_map.load_map(project).rows == []


def test_encrypted_token_survives_new_store_and_is_reused(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from app.db.models import RepoToken
    from app.tools.repo_tokens import RepoTokenStore
    from app.tools.repo_tool import RepoTool
    repo = 'test/' + uuid.uuid4().hex
    key = Fernet.generate_key().decode()
    store = RepoTokenStore(key)
    try:
        store.save(repo, 'example-private-token')
        with SessionLocal() as db:
            assert 'example-private-token' not in db.get(RepoToken, repo).encrypted_token
        reloaded = RepoTokenStore(key)
        tool = RepoTool(token_store=reloaded)
        monkeypatch.setattr(tool, '_checkout_path', lambda *args: tmp_path / 'repo')
        monkeypatch.setattr(tool, '_clone_public', Mock(side_effect=RuntimeError('private')))
        private = Mock()
        monkeypatch.setattr(tool, '_clone_with_token', private)
        monkeypatch.setattr(tool, '_git', Mock())
        tool.clone_or_pull(repo, 'test-subtask')
        private.assert_called_once_with(repo, tmp_path / 'repo', 'example-private-token')
    finally:
        with SessionLocal() as db:
            row = db.get(RepoToken, repo)
            if row:
                db.delete(row)
                db.commit()


@pytest.mark.asyncio
async def test_transition_event_is_streamed_and_persisted(monkeypatch):
    import asyncio
    from app import events
    from app.db.models import Ticket, TicketEvent
    ticket_id = uuid.uuid4()
    key = 'TEST-' + uuid.uuid4().hex[:16]
    with SessionLocal() as db:
        db.add(Ticket(id=ticket_id, source='jira', external_key=key, title='Status event', description='Test'))
        db.commit()
    monkeypatch.setattr(status_events, 'loop', asyncio.get_running_loop())
    result = {'meaning': 'in-review', 'layer': 'map', 'to': 'Peer inspection', 'applied': True}
    try:
        async with events.subscribe(str(ticket_id)) as queue:
            await asyncio.to_thread(status_events.emit_transition, key, result)
            event = await asyncio.wait_for(queue.get(), 2)
            assert all(event[k] == v for k, v in result.items())
        with SessionLocal() as db:
            recorded = db.query(TicketEvent).filter_by(ticket_id=ticket_id).one()
            assert 'Peer inspection' in recorded.message and 'map' in recorded.message
    finally:
        with SessionLocal() as db:
            db.delete(db.get(Ticket, ticket_id))
            db.commit()
