import hashlib
import hmac
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from app.web import webhooks
from app.core import webhook_processing, comment_handling, proposals, pr_sync
from app.agents.comment_monitor import CommentIntent
from app.agents.planning import ApprovalDecision
from app.db.connection import SessionLocal
from app.db.models import WebhookDelivery, PendingApproval, PRLink
from app.tools.jira_tool import JiraTool


class Jira:
    extract_plain_text = staticmethod(JiraTool.extract_plain_text)
    def __init__(self):
        self.comments, self.statuses = [], []
        self.category = 'new'
    def own_account_id(self):
        return 'bot'
    def get_issue_detail(self, key):
        return {'assignee_id': 'owner', 'status_category': self.category}
    def recent_comments(self, key, limit=12):
        return [{'id': str(i), 'from_tool': False, 'author': 'owner', 'body': body, 'created': None}
                for i, body in enumerate(self.comments)]
    def comment(self, key, text):
        self.comments.append(text)
        return str(len(self.comments))
    def set_status(self, key, stage):
        self.statuses.append(stage)
        return {'applied': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['github', 'jira'])
async def test_signature_required_before_database(source, monkeypatch):
    app = FastAPI(); app.include_router(webhooks.router)
    monkeypatch.setattr(webhooks.settings, source + '_webhook_secret', 'secret')
    monkeypatch.setattr(webhooks, 'enqueue', lambda *a: pytest.fail('unauthenticated data reached DB'))
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/webhooks/' + source, json={})
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_authenticated_delivery_and_comment_semantic_dedupe(db_ticket, monkeypatch):
    app = FastAPI(); app.include_router(webhooks.router)
    monkeypatch.setattr(webhooks.settings, 'jira_webhook_secret', 'secret')
    calls = []
    async def process(delivery):
        calls.append(delivery)
    monkeypatch.setattr(webhooks, 'process_delivery', process)
    comment_id = str(uuid.uuid4())
    body = json.dumps({'webhookEvent': 'comment_created', 'issue': {'key': db_ticket[1]}, 'comment': {'id': comment_id}}).encode()
    headers = {'X-Hub-Signature': 'sha256=' + hmac.new(b'secret', body, hashlib.sha256).hexdigest(),
               'X-Atlassian-Webhook-Identifier': str(uuid.uuid4())}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.post('/webhooks/jira', content=body, headers=headers)).json()['duplicate'] is False
            headers['X-Atlassian-Webhook-Identifier'] = str(uuid.uuid4())
            assert (await client.post('/webhooks/jira', content=body, headers=headers)).json()['duplicate'] is True
        assert len(calls) == 1
    finally:
        with SessionLocal() as db:
            db.delete(db.get(WebhookDelivery, f'jira:comment:{comment_id}')); db.commit()


@pytest.mark.asyncio
async def test_inbox_recovers_failure_once(db_ticket, monkeypatch):
    key = 'github:' + str(uuid.uuid4())
    webhook_processing.enqueue(key, 'github', 'ping', {})
    calls = []
    async def dispatch(*args):
        calls.append(args)
        if len(calls) == 1: raise RuntimeError('temporary failure')
    monkeypatch.setattr(webhook_processing, 'dispatch', dispatch)
    try:
        await webhook_processing.process_delivery(key)
        await webhook_processing.process_delivery(key)
        await webhook_processing.process_delivery(key)
        assert len(calls) == 2
        with SessionLocal() as db:
            assert db.get(WebhookDelivery, key).status == 'processed'
    finally:
        with SessionLocal() as db:
            db.delete(db.get(WebhookDelivery, key)); db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize('author,outcome', [('bot', 'ignored_self'), ('stranger', 'unauthorized')])
async def test_permission_and_self_filter_precede_llm(monkeypatch, author, outcome):
    jira = Jira()
    monkeypatch.setattr(comment_handling, 'context_for', lambda key: {'ticket_id': str(uuid.uuid4())})
    monitor = SimpleNamespace(run=lambda *args: pytest.fail('LLM should not run'))
    result = await comment_handling.handle_comment('TEST-1', {'id': '1', 'author': {'accountId': author}, 'body': 'stop'}, jira, monitor)
    assert result == outcome
    assert bool(jira.comments) == (author == 'stranger')


@pytest.mark.asyncio
@pytest.mark.parametrize('intent', ['QUESTION', 'CHATTER', 'AMBIGUOUS', 'COMMAND'])
async def test_rich_comment_routes_without_auto_action(monkeypatch, intent):
    jira = Jira(); calls = []
    ticket_id = str(uuid.uuid4())
    monkeypatch.setattr(comment_handling, 'context_for', lambda key: {'ticket_id': ticket_id})
    monkeypatch.setattr(comment_handling, 'proposal_record', lambda *args: 'gate-id')
    async def gated(gate_id):
        calls.append(gate_id)
    async def event(**kwargs): pass
    monkeypatch.setattr(comment_handling, 'run_proposal', gated)
    monkeypatch.setattr(comment_handling, 'log_event', event)
    result = CommentIntent(intent=intent, action='stop' if intent == 'COMMAND' else None,
                           proposal='Stop the current run' if intent == 'COMMAND' else '', response='Here is the status?')
    monitor = SimpleNamespace(run=lambda *args: result)
    assert await comment_handling.handle_comment('TEST-1', {'id': '1', 'author': {'accountId': 'owner'}, 'body': 'a comment'}, jira, monitor) == intent
    assert calls == (['gate-id'] if intent == 'COMMAND' else [])
    assert len(jira.comments) == (0 if intent == 'CHATTER' else 1)


@pytest.mark.asyncio
async def test_comment_classification_receives_conversation_thread_not_just_the_latest_message(monkeypatch):
    jira = Jira()
    jira.comments = ['Diagnosis done; root cause found in app.py', 'Plan ready, awaiting approval']
    ticket_id = str(uuid.uuid4())
    monkeypatch.setattr(comment_handling, 'context_for', lambda key: {'ticket_id': ticket_id})
    seen = {}
    def capture(ticket_id_arg, comment, context):
        seen['context'] = context
        return CommentIntent(intent='CHATTER')
    monitor = SimpleNamespace(run=capture)
    await comment_handling.handle_comment(
        'TEST-1', {'id': 'new', 'author': {'accountId': 'owner'}, 'body': 'thanks'}, jira, monitor)
    thread = seen['context']['thread']
    assert [item['body'] for item in thread] == jira.comments  # oldest first, as a dialogue
    assert all(item['id'] != 'new' for item in thread)  # the triggering comment isn't its own history


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['APPROVE', 'REJECT'])
async def test_jira_decision_uses_registered_shared_gate(monkeypatch, action):
    jira = Jira(); decisions = []
    ticket_id, gate_id = str(uuid.uuid4()), str(uuid.uuid4())
    monkeypatch.setattr(comment_handling, 'context_for', lambda key: {'ticket_id': ticket_id})
    monkeypatch.setattr(comment_handling, 'pending_for_ticket', lambda key: [{'id': gate_id}])
    async def decide(gate, decision): decisions.append((gate, decision))
    monkeypatch.setattr(comment_handling, 'decide_registered', decide)
    result = await comment_handling.handle_comment('TEST-1', {'id': '1', 'author': {'accountId': 'owner'}, 'body': action}, jira)
    assert result == ('approved' if action == 'APPROVE' else 'rejected')
    assert decisions[0][0] == gate_id


@pytest.mark.asyncio
async def test_command_is_checkpointed_until_decision(db_ticket, monkeypatch):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from app.config import settings
    ticket_id, key = db_ticket
    intent = CommentIntent(intent='COMMAND', action='stop', proposal='Stop current attempt')
    gate_id = proposals.proposal_record(ticket_id, key, str(uuid.uuid4()), intent)
    effects = []
    monkeypatch.setattr(proposals, '_stop', lambda ticket_id: effects.append(ticket_id))
    from app.tools import jira_tool
    monkeypatch.setattr(jira_tool, 'JiraTool', Jira)
    try:
        result = await proposals.run_proposal(gate_id)
        assert 'decision' not in result and effects == []
        result = await proposals.run_proposal(gate_id, ApprovalDecision(approval_status='approved'))
        assert result['complete'] and effects == [ticket_id]
        await proposals.run_proposal(gate_id, ApprovalDecision(approval_status='approved'))
        assert effects == [ticket_id]
        with SessionLocal() as db:
            assert db.get(PendingApproval, uuid.UUID(gate_id)).status == 'APPROVED'
    finally:
        async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
            await saver.adelete_thread('proposal:' + gate_id)


@pytest.mark.asyncio
async def test_pr_matrix_merge_reopen_closed_open_and_stale(db_ticket):
    ticket_id, key = db_ticket
    jira = Jira(); comments = []
    github = SimpleNamespace(comment_pr=lambda repo, number, text: comments.append(text))
    snapshot = {'id': str(uuid.uuid4()), 'number': 1, 'url': 'https://github.com/owner/repo/pull/1',
                'repo': 'owner/repo', 'branch': 'sdlc/test', 'state': 'open'}
    await pr_sync.sync_pr(key, snapshot.copy(), jira, github)
    assert jira.statuses == ['in_progress']
    count = len(jira.comments)
    await pr_sync.sync_pr(key, snapshot.copy(), jira, github)
    assert len(jira.comments) == count
    snapshot['state'] = 'closed'
    await pr_sync.sync_pr(key, snapshot.copy(), jira, github)
    await pr_sync.sync_pr(key, snapshot.copy(), jira, github, reopened=True)
    assert 'remains closed' in jira.comments[-1]
    snapshot['state'] = 'open'
    await pr_sync.sync_pr(key, snapshot.copy(), jira, github, reopened=True)
    assert comments == ['Ticket moved back to In Progress']
    snapshot['state'] = 'merged'
    await pr_sync.sync_pr(key, snapshot.copy(), jira, github)
    assert jira.statuses[-1] == 'done'
    jira.category = 'indeterminate'
    await pr_sync.sync_pr(key, snapshot.copy(), jira, github, reopened=True)
    assert 'new PR/branch is needed' in jira.comments[-1]
    snapshot['state'] = 'open'  # stale payload can never unmerge history
    await pr_sync.sync_pr(key, snapshot.copy(), jira, github)
    with SessionLocal() as db:
        assert db.get(PRLink, snapshot['id']).pr_state == 'merged'


def test_key_extraction_rejects_cross_ticket_ambiguity():
    assert pr_sync.extract_key('Fix SCRUM-12', 'sdlc/SCRUM-12') == 'SCRUM-12'
    assert pr_sync.extract_key('SCRUM-12 and SCRUM-13') is None


@pytest.mark.asyncio
async def test_real_plan_registry_jira_and_ui_share_one_resume(db_ticket, monkeypatch):
    from app.orchestrator import graph as graph_module
    from app.agents.state import SubtaskState
    from app.agents.planning import Step
    from app.db.models import Subtask
    from app.core.approvals import pending_for_ticket
    from app.web.approval import _decide
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from app.config import settings
    ticket_id, key = db_ticket
    sid = str(uuid.uuid4())
    state = SubtaskState(ticket_id=ticket_id, subtask_id=sid, subtask_type='bug', jira_key=key,
        description='Handle zero', repo='owner/repo', plan=[Step(step_id='1', intent='Guard zero', target_file='app.py')])
    jira = Jira(); calls = []
    class Diagnosis:
        def run(self, state):
            state.diagnosis = {'root_cause': 'No guard', 'reasoning': 'Direct divide', 'files': ['app.py']}
            return state
    class Planner:
        def run(self, state): return state
    class Executor:
        async def run(self, state):
            calls.append('execute')
            state.current_step, state.execution_complete = 1, True
            return state
    class Publisher:
        def publish_changes(self, state):
            calls.append('publish')
            return {'id': str(uuid.uuid4()), 'number': 1, 'url': 'https://github.com/owner/repo/pull/1',
                    'state': 'open', 'repo': 'owner/repo', 'branch': 'sdlc/' + sid}
    factory = graph_module.build_graph
    monkeypatch.setattr(graph_module, 'build_graph', lambda **kwargs: factory(agent=Diagnosis(), planner=Planner(),
        executor=Executor(), publisher=Publisher(), jira=jira, **kwargs))
    with SessionLocal() as db:
        db.add(Subtask(id=uuid.UUID(sid), ticket_id=uuid.UUID(ticket_id), type='bug', description='test', status='running'))
        db.commit()
    try:
        await graph_module.run_diagnosis_graph(state)
        gates = pending_for_ticket(ticket_id)
        assert len(gates) == 1
        gate_id = gates[0]['id']
        assert any(gate_id in text for text in jira.comments)
        result = await comment_handling.handle_comment(key, {'id': 'test-comment', 'author': {'accountId': 'owner'},
            'body': f'APPROVE {gate_id}'}, jira)
        assert result == 'approved' and calls == ['execute', 'publish']
        await _decide(uuid.UUID(ticket_id), uuid.UUID(sid), ApprovalDecision(approval_status='approved'))
        assert calls == ['execute', 'publish']
        assert pending_for_ticket(ticket_id) == []
    finally:
        async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
            await saver.adelete_thread(f'{ticket_id}:{sid}')
