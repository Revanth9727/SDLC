import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from app.agents.llm import LLMClient
from app.agents.planning import ApprovalDecision
from app.agents.state import SubtaskState
from app.config import settings
from app.core import budget
from app.db.connection import SessionLocal, engine
from app.db.models import Ticket, Subtask, TicketBudget
from app.orchestrator import graph as module


class _Decomposer:
    """Auto-confirms one sub-task = the whole ticket, so these guard/budget
    tests can drive straight to the node they're actually about."""
    def run(self, state):
        state.subtask_specs = [{'spec_id': '1', 'type': state.subtask_type, 'description': state.description,
                                'repo': state.repo, 'depends_on': []}]
        state.decomposition_reasoning = 'One sub-task.'
        return state


async def confirm_intent(g, config):
    """Resume past the Planner's mandatory intent-confirmation pause."""
    return await g.ainvoke(Command(resume=ApprovalDecision(approval_status='approved').model_dump()), config)


@pytest.fixture
def state():
    TicketBudget.__table__.create(engine, checkfirst=True)
    tid, sid = uuid.uuid4(), uuid.uuid4()
    state = SubtaskState(ticket_id=str(tid), subtask_id=str(sid), subtask_type='bug',
                         repo='owner/repo', jira_key='TEST-6', description='Guard division',
                         confirmed_repos=['owner/repo'])
    with SessionLocal() as db:
        db.add(Ticket(id=tid, source='jira', title='Guard test', description='Test', status='processing'))
        db.flush()
        db.add(Subtask(id=sid, ticket_id=tid, type='bug', description='Guard division', status='running', state=state.model_dump()))
        db.commit()
    yield state
    with SessionLocal() as db:
        db.delete(db.get(Ticket, tid))
        db.commit()


def graph(state, agent, saver):
    jira = SimpleNamespace(set_status=Mock(return_value={'applied': True}), comment=Mock())
    return module.build_graph(agent=agent, decomposer=_Decomposer(), memory_search=lambda *a, **k: [],
                              checkpointer=saver, jira=jira, activity_check=lambda _: None), jira


@pytest.mark.asyncio
async def test_invalid_output_retries_then_checkpointed_human_queue(state, monkeypatch):
    from app.main import app
    from app.web import escalations
    bad = SimpleNamespace(run=Mock(return_value={'diagnosis': 'invalid'}))
    g, jira = graph(state, bad, MemorySaver())
    config = module.thread_config(state.ticket_id, state.subtask_id)
    await g.ainvoke(state.model_dump(), config)
    await confirm_intent(g, config)
    snapshot = await g.aget_state(config)
    assert bad.run.call_count == settings.max_agent_retries + 1
    assert snapshot.next == ('human_resolution',)
    assert 'Retry limit' in snapshot.values['failure_reason']
    jira.set_status.assert_called_with('TEST-6', 'blocked')
    assert 'Retry limit' in TestClient(app).get('/escalations').text
    @asynccontextmanager
    async def opened(*args, **kwargs):
        yield g
    monkeypatch.setattr(escalations, 'open_graph', opened)
    client = TestClient(app)
    path = f'/tickets/{state.ticket_id}/subtasks/{state.subtask_id}/escalation/reject'
    result = client.post(path, json={'note': 'Cannot fix safely', 'escalation_id': snapshot.values['escalation_id']})
    assert result.status_code == 200
    assert result.json()['status'] == 'failed'
    assert client.post(path, json={'note': 'duplicate', 'escalation_id': snapshot.values['escalation_id']}).status_code == 409
    assert not (await g.aget_state(config)).next


@pytest.mark.asyncio
async def test_honest_failure_not_retried_and_survives_reopen(state):
    def unable(s):
        s.status, s.failure_reason = 'needs_human', 'Cannot determine the root cause'
        return s
    agent = SimpleNamespace(run=Mock(side_effect=unable))
    config = module.thread_config(state.ticket_id, state.subtask_id)
    async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
        await saver.setup()
        g, jira = graph(state, agent, saver)
        await g.ainvoke(state.model_dump(), config)
        await confirm_intent(g, config)
    async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
        try:
            g, jira = graph(state, agent, saver)
            assert (await g.aget_state(config)).next == ('human_resolution',)
            assert agent.run.call_count == 1
            await g.ainvoke(Command(resume={'action': 'retry', 'note': 'Try again'}), config)
            assert agent.run.call_count == 2
            assert (await g.aget_state(config)).next == ('human_resolution',)
        finally:
            await saver.adelete_thread(config['configurable']['thread_id'])


def fake_provider(text='pong', tokens=100):
    create = Mock(return_value=SimpleNamespace(usage=SimpleNamespace(total_tokens=tokens),
                 choices=[SimpleNamespace(message=SimpleNamespace(content=text))]))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), create


def test_budget_is_durable_and_denies_next_call(state, monkeypatch):
    monkeypatch.setattr(settings, 'ticket_call_budget', 2)
    provider, create = fake_provider()
    client = LLMClient(client=provider)
    client.complete('system', 'user', ticket_id=state.ticket_id)
    client.complete('system', 'user', ticket_id=state.ticket_id)
    LLMClient.reset_usage()
    assert LLMClient.get_usage(state.ticket_id)['calls'] == 2
    assert LLMClient.get_usage(state.ticket_id)['tokens'] == 200
    assert LLMClient.get_usage(state.ticket_id)['est_cost_usd'] > 0
    with pytest.raises(budget.BudgetExceeded, match='2 calls'):
        client.complete('system', 'user', ticket_id=state.ticket_id)
    assert create.call_count == 2
    budget.extend(state.ticket_id)
    client.complete('system', 'user', ticket_id=state.ticket_id)
    assert LLMClient.get_usage(state.ticket_id)['calls'] == 3


@pytest.mark.parametrize('field,value', [('ticket_token_budget', 50), ('ticket_cost_budget_usd', 0.000001)])
def test_token_and_cost_limits_stop_further_spend(state, monkeypatch, field, value):
    monkeypatch.setattr(settings, field, value)
    provider, create = fake_provider()
    client = LLMClient(client=provider)
    client.complete('system', 'user', ticket_id=state.ticket_id)
    with pytest.raises(budget.BudgetExceeded):
        client.complete('system', 'user', ticket_id=state.ticket_id)
    assert create.call_count == 1


def test_failed_provider_call_is_counted(state):
    provider, create = fake_provider()
    create.side_effect = RuntimeError('provider unavailable')
    with pytest.raises(RuntimeError):
        LLMClient(client=provider).complete('system', 'user', ticket_id=state.ticket_id)
    assert LLMClient.get_usage(state.ticket_id)['calls'] == 1


def test_no_progress_and_cross_ticket_output_are_rejected(state):
    from app.core.guard import validate_output
    with pytest.raises(ValueError, match='forward progress'):
        validate_output(state, state.model_copy(), 'execute')
    changed = state.model_copy(update={'ticket_id': str(uuid.uuid4())})
    with pytest.raises(ValueError, match='protected identity'):
        validate_output(state, changed, 'diagnosis')


@pytest.mark.asyncio
async def test_budget_guard_pauses_before_agent(state, monkeypatch):
    monkeypatch.setattr(settings, 'ticket_call_budget', 1)
    budget.reserve(state.ticket_id)
    agent = SimpleNamespace(run=Mock())
    g, jira = graph(state, agent, MemorySaver())
    config = module.thread_config(state.ticket_id, state.subtask_id)
    await g.ainvoke(state.model_dump(), config)
    snap = await g.aget_state(config)
    assert snap.next == ('human_resolution',)
    assert '1 calls' in snap.values['failure_reason']
    agent.run.assert_not_called()


def test_concurrent_calls_cannot_pass_last_budget_slot(state, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(settings, 'ticket_call_budget', 1)
    def attempt():
        try:
            return budget.reserve(state.ticket_id)
        except budget.BudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == [False, True]
    assert budget.usage(state.ticket_id)['calls'] == 1


@pytest.mark.asyncio
async def test_duplicate_retry_cannot_consume_new_escalation(state, monkeypatch):
    from app.main import app
    from app.web import escalations
    bad = SimpleNamespace(run=Mock(return_value={}))
    g, _ = graph(state, bad, MemorySaver())
    config = module.thread_config(state.ticket_id, state.subtask_id)
    await g.ainvoke(state.model_dump(), config)
    await confirm_intent(g, config)
    first = await g.aget_state(config)
    @asynccontextmanager
    async def opened(*args, **kwargs):
        yield g
    monkeypatch.setattr(escalations, 'open_graph', opened)
    client = TestClient(app)
    url = f'/tickets/{state.ticket_id}/subtasks/{state.subtask_id}/escalation/retry'
    body = {'note': 'Retry now', 'escalation_id': first.values['escalation_id']}
    assert client.post(url, json=body).status_code == 200
    calls = bad.run.call_count
    assert client.post(url, json=body).status_code == 409
    assert bad.run.call_count == calls
