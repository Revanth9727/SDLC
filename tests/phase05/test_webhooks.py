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
async def test_github_signature_required_before_database(monkeypatch):
    app = FastAPI(); app.include_router(webhooks.router)
    monkeypatch.setattr(webhooks.settings, 'github_webhook_secret', 'secret')
    monkeypatch.setattr(webhooks, 'enqueue', lambda *a: pytest.fail('unauthenticated data reached DB'))
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/webhooks/github', json={})
        assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['', '?secret=wrong'])
async def test_jira_query_secret_required_before_database(query, monkeypatch):
    app = FastAPI(); app.include_router(webhooks.router)
    monkeypatch.setattr(webhooks.settings, 'jira_webhook_secret', 'secret')
    monkeypatch.setattr(webhooks, 'enqueue', lambda *a: pytest.fail('unauthenticated data reached DB'))
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/webhooks/jira' + query, json={})
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
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            url = '/webhooks/jira?secret=secret&triggeredByUser=true'
            response = await client.post(url, content=body)
            assert response.status_code == 200
            assert response.json()['duplicate'] is False
            assert (await client.post(url, content=body)).json()['duplicate'] is True
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
@pytest.mark.parametrize('author,posted_by_app,outcome', [
    ('bot', True, 'ignored_self'),
    ('stranger', False, 'unauthorized'),
])
async def test_permission_and_self_filter_precede_llm(monkeypatch, author, posted_by_app, outcome):
    jira = Jira()
    monkeypatch.setattr(comment_handling, 'context_for', lambda key: {'ticket_id': str(uuid.uuid4())})
    monkeypatch.setattr(comment_handling, 'is_app_comment', lambda comment_id: posted_by_app)
    monitor = SimpleNamespace(run=(lambda *args: pytest.fail('app comment reached classifier')) if posted_by_app else
                              (lambda *args: CommentIntent(intent='CHATTER')))
    result = await comment_handling.handle_comment('TEST-1', {'id': '1', 'author': {'accountId': author}, 'body': 'stop'}, jira, monitor)
    assert result == outcome
    assert bool(jira.comments) == (author == 'stranger')


@pytest.mark.asyncio
async def test_human_sharing_api_account_is_allowed_by_allowlist_and_logs_decisions(monkeypatch, caplog):
    jira = Jira(); jira.own_account_id = lambda: 'shared-account'
    ticket_id, gate_id = str(uuid.uuid4()), str(uuid.uuid4())
    monkeypatch.setattr(comment_handling, 'context_for', lambda key: {'ticket_id': ticket_id})
    monkeypatch.setattr(comment_handling, 'is_app_comment', lambda comment_id: False)
    monkeypatch.setattr(comment_handling.settings, 'jira_approval_account_ids', ['shared-account'])
    monkeypatch.setattr(comment_handling, 'pending_for_ticket', lambda key: [{'id': gate_id}])
    decisions = []
    async def decide(gate, decision): decisions.append((gate, decision.approval_status))
    monkeypatch.setattr(comment_handling, 'decide_registered', decide)
    caplog.set_level('INFO', logger='app.core.comment_handling')
    result = await comment_handling.handle_comment('TEST-1', {
        'id': 'human-comment', 'author': {'accountId': 'shared-account'}, 'body': 'APPROVE'}, jira,
        SimpleNamespace(run=lambda *args: CommentIntent(intent='APPROVE')))
    assert result == 'approved'
    assert decisions == [(gate_id, 'approved')]
    assert 'authorized=True' in caplog.text
    assert 'intent=APPROVE' in caplog.text
    assert 'gate_matched' in caplog.text
    assert 'action=resumed' in caplog.text


@pytest.mark.asyncio
async def test_unassigned_ticket_with_empty_allowlist_fails_closed_clearly(monkeypatch):
    jira = Jira()
    jira.get_issue_detail = lambda key: {'assignee_id': None, 'status_category': 'indeterminate'}
    monkeypatch.setattr(comment_handling, 'context_for', lambda key: {'ticket_id': str(uuid.uuid4())})
    monkeypatch.setattr(comment_handling, 'is_app_comment', lambda comment_id: False)
    monkeypatch.setattr(comment_handling.settings, 'jira_approval_account_ids', [])
    result = await comment_handling.handle_comment('ANY-42', {
        'id': 'human-comment', 'author': {'accountId': 'any-human'}, 'body': 'APPROVE'}, jira,
        SimpleNamespace(run=lambda *args: CommentIntent(intent='APPROVE')))
    assert result == 'no_authorized_approver'
    assert jira.comments[-1] == ('No authorized approver configured: assign this Jira ticket or set '
                                  'JIRA_APPROVAL_ACCOUNT_IDS in the app .env.')


def test_all_gate_comment_intents_use_cheap_model_tier():
    from app.agents.comment_monitor import CommentMonitorAgent
    class LLM:
        def __init__(self): self.calls = []
        def complete_json(self, system, user, schema, **kwargs):
            self.system = system
            self.calls.append(kwargs)
            return schema(intent='APPROVE')
    llm = LLM()
    result = CommentMonitorAgent(llm).run('ticket', 'APPROVE', {'state': {'approval_status': 'pending'}})
    assert result.intent == 'APPROVE'
    assert llm.calls == [{'tier': 'cheap', 'ticket_id': 'ticket'}]
    assert all(phrase in llm.system for phrase in ('yes', 'lgtm', 'go ahead', 'ship it', 'do X instead'))


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
    intent = CommentIntent(intent=action)
    result = await comment_handling.handle_comment('TEST-1', {'id': '1', 'author': {'accountId': 'owner'}, 'body': action},
                                                   jira, SimpleNamespace(run=lambda *args: intent))
    assert result == ('approved' if action == 'APPROVE' else 'feedback_requested')
    assert decisions[0][0] == gate_id


@pytest.mark.asyncio
@pytest.mark.parametrize('intent_name', ['REJECT', 'REVISE'])
async def test_jira_feedback_decision_uses_shared_gate_resume(monkeypatch, intent_name):
    jira = Jira(); decisions = []
    ticket_id, gate_id = str(uuid.uuid4()), str(uuid.uuid4())
    monkeypatch.setattr(comment_handling, 'context_for', lambda key: {'ticket_id': ticket_id})
    monkeypatch.setattr(comment_handling, 'pending_for_ticket', lambda key: [{'id': gate_id}])
    async def decide(gate, decision):
        decisions.append((gate, decision))
        return {'approval_status': 'pending'}
    monkeypatch.setattr(comment_handling, 'decide_registered', decide)
    intent = CommentIntent(intent=intent_name, feedback='Handle None too')
    result = await comment_handling.handle_comment('TEST-1', {
        'id': 'feedback', 'author': {'accountId': 'owner'}, 'body': 'reject - handle None'}, jira,
        SimpleNamespace(run=lambda *args: intent))
    assert result == 'replanning'
    assert decisions[0][0] == gate_id
    assert decisions[0][1] == ApprovalDecision(
        approval_status='rejected', note='Handle None too',
        provenance=f'jira:TEST-1:comment:feedback:gate:{gate_id}',
    )


@pytest.mark.asyncio
async def test_question_answers_without_consuming_pending_gate(monkeypatch):
    jira = Jira(); ticket_id = str(uuid.uuid4())
    monkeypatch.setattr(comment_handling, 'context_for', lambda key: {'ticket_id': ticket_id})
    monkeypatch.setattr(comment_handling, 'pending_for_ticket', lambda key: pytest.fail('question must not consume gate'))
    intent = CommentIntent(intent='QUESTION', response='The plan changes app.py and its regression test.')
    result = await comment_handling.handle_comment('TEST-1', {
        'id': 'question', 'author': {'accountId': 'owner'}, 'body': 'What changes?'}, jira,
        SimpleNamespace(run=lambda *args: intent))
    assert result == 'QUESTION'
    assert jira.comments[-1] == intent.response


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
    assert jira.statuses == ['in_review']
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
        description='Handle zero', repo='owner/repo', confirmed_repos=['owner/repo'],
        plan=[Step(step_id='1', intent='Guard zero', target_file='app.py')])
    jira = Jira(); calls = []
    class Diagnosis:
        def run(self, state):
            state.diagnosis = {'root_cause': 'No guard', 'reasoning': 'Direct divide', 'files': ['app.py']}
            return state
    class Planner:
        def run(self, state): return state
    class Decomposer:
        def run(self, state):
            state.subtask_specs = [{'spec_id': '1', 'type': state.subtask_type, 'description': state.description,
                                    'repo': state.repo, 'depends_on': []}]
            state.decomposition_reasoning = 'One clear bug fix.'
            return state
    class Executor:
        async def run(self, state):
            calls.append('execute')
            state.current_step, state.execution_complete = 1, True
            state.base_commit = 'base-sha'
            state.file_changes = {'app.py': 'def divide(a, b):\n    return a / b\n'}
            return state
    class Publisher:
        def publish_changes(self, state):
            calls.append('publish')
            return {'id': str(uuid.uuid4()), 'number': 1, 'url': 'https://github.com/owner/repo/pull/1',
                    'state': 'open', 'repo': 'owner/repo', 'branch': 'sdlc/' + sid}
    class Critic:
        def run(self, state):
            calls.append('critic')
            state.critic_verdict = {'approved': True, 'issues': [], 'verifiability': 'ok',
                                    'test_validity': 'valid', 'test_issues': [],
                                    'implementation_valid': True}
            return state
    factory = graph_module.build_graph
    monkeypatch.setattr(graph_module, 'build_graph', lambda **kwargs: factory(agent=Diagnosis(), planner=Planner(),
        decomposer=Decomposer(), executor=Executor(), critic=Critic(), publisher=Publisher(), jira=jira,
        memory_search=lambda *a, **k: [], **kwargs))
    with SessionLocal() as db:
        db.add(Subtask(id=uuid.UUID(sid), ticket_id=uuid.UUID(ticket_id), type='bug', description='test', status='running'))
        db.commit()
    try:
        await graph_module.run_diagnosis_graph(state)
        # The Planner's intent-confirmation pauses first; confirm it (as the UI
        # would) so the flow reaches the plan gate this test is actually about.
        async with graph_module.open_graph(ticket_id, sid, lock=True) as g:
            await graph_module.resume_approval(g, ticket_id, sid, ApprovalDecision(approval_status='approved'))
        gates = pending_for_ticket(ticket_id)
        assert len(gates) == 1
        gate_id = gates[0]['id']
        assert any('Reply approve or reject, or describe a change you want.' in text for text in jira.comments)
        assert not any(gate_id in text for text in jira.comments)
        result = await comment_handling.handle_comment(key, {'id': 'test-comment', 'author': {'accountId': 'owner'},
            'body': 'go ahead'}, jira,
            SimpleNamespace(run=lambda *args: CommentIntent(intent='APPROVE')))
        assert result == 'approved' and calls == ['execute', 'critic', 'publish']
        await _decide(uuid.UUID(ticket_id), uuid.UUID(sid), ApprovalDecision(approval_status='approved'))
        assert calls == ['execute', 'critic', 'publish']
        assert proposals.get_proposal(gate_id)['status'] == 'APPROVED'
    finally:
        async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
            await saver.adelete_thread(f'{ticket_id}:{sid}')
