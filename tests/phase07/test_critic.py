"""Critic: validates the Executor's completed change before PR (architecture.md
§7d, ai_rules.md R-31/R-32). Unit tests hit CriticAgent directly with a fake LLM;
graph tests prove the reject->redo->approve loop and its escalation cap."""
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agents.critic import CriticAgent, CriticVerdict
from app.agents.planning import Step
from app.agents.state import SubtaskState, UnresolvedCheck
from app.agents.executor import _record_interface_uncertainties
from app.config import settings
from app.db.connection import SessionLocal, engine
from app.db.models import Ticket, Subtask, TicketBudget
from app.orchestrator import graph as module


class FakeLLM:
    def __init__(self, verdict: dict):
        self.verdict = verdict
        self.calls: list[tuple] = []

    def complete_json(self, system, user, schema, **kwargs):
        self.calls.append((system, user, kwargs))
        return schema.model_validate(self.verdict)

    def get_usage(self, ticket_id):
        return {'calls': len(self.calls), 'tokens': len(self.calls) * 50, 'est_cost_usd': 0.001 * len(self.calls)}


def _state(**overrides):
    base = dict(
        ticket_id=str(uuid.uuid4()), subtask_id=str(uuid.uuid4()), subtask_type='bug',
        description='Guard division by zero', repo='owner/repo', approval_status='approved',
        plan=[Step(step_id='1', intent='Guard b == 0', target_file='app.py'),
              Step(step_id='2', intent='Add regression test', target_file='test_app.py', action='create')],
        current_step=2, execution_complete=True,
        steps_done=[
            {'step_id': '1', 'intent': 'Guard b == 0', 'target_file': 'app.py', 'content': 'x',
             'before_sha256': 'a', 'report': {'matches': []}, 'tests': {'passed': True, 'returncode': 0, 'output': '', 'outcome': 'passed'}},
            {'step_id': '2', 'intent': 'Add regression test', 'target_file': 'test_app.py', 'content': 'y',
             'before_sha256': 'b', 'report': {'matches': []}, 'tests': {'passed': True, 'returncode': 0, 'output': '2 passed', 'outcome': 'passed'}},
        ],
        file_changes={'app.py': 'def divide(a, b):\n    if b == 0:\n        raise ValueError()\n    return a/b\n',
                      'test_app.py': 'from app import divide\ndef test_zero():\n    ...\n'},
        diagnosis={'root_cause': 'No zero guard', 'files': ['app.py'], 'reasoning': 'Direct division'},
        verifiability='verified',
    )
    base.update(overrides)
    return SubtaskState(**base)


# --- CriticAgent unit tests -------------------------------------------------

def test_critic_approves_a_good_fix_and_tracks_budget():
    llm = FakeLLM({'approved': True, 'issues': [], 'verifiability': 'ok',
                   'test_validity': 'valid', 'test_issues': [], 'implementation_valid': True})
    state = _state()
    result = CriticAgent(llm).run(state)
    assert result.critic_verdict == {'approved': True, 'issues': [], 'verifiability': 'ok',
                                     'test_validity': 'valid', 'test_issues': [],
                                     'implementation_valid': True}
    assert result.budget_used.calls == 1


def test_critic_rejects_with_specific_issues():
    llm = FakeLLM({'approved': False, 'issues': ['Guard does not handle negative b as the ticket also describes'],
                   'verifiability': 'ok', 'test_validity': 'valid', 'test_issues': [],
                   'implementation_valid': False})
    result = CriticAgent(llm).run(_state())
    verdict = CriticVerdict.model_validate(result.critic_verdict)
    assert not verdict.approved
    assert 'negative b' in verdict.issues[0]


def test_critic_flags_no_tests_without_forcing_rejection():
    # R-32: no_tests is reported, not by itself grounds for rejection — nothing
    # more the Executor can do if the plan already decided none was addable.
    llm = FakeLLM({'approved': True, 'issues': [], 'verifiability': 'no_tests',
                   'test_validity': 'valid', 'test_issues': [], 'implementation_valid': True})
    result = CriticAgent(llm).run(_state(verifiability='no_tests'))
    verdict = CriticVerdict.model_validate(result.critic_verdict)
    assert verdict.approved and verdict.verifiability == 'no_tests'


def test_critic_flags_uncovered_change_and_rejects():
    llm = FakeLLM({'approved': False, 'issues': ['The new negative-b branch in divide() has no test exercising it'],
                   'verifiability': 'uncovered_change', 'test_validity': 'valid', 'test_issues': [],
                   'implementation_valid': False})
    result = CriticAgent(llm).run(_state())
    verdict = CriticVerdict.model_validate(result.critic_verdict)
    assert not verdict.approved and verdict.verifiability == 'uncovered_change'
    assert 'negative-b branch' in verdict.issues[0]


def test_critic_prompt_grounds_in_the_actual_change_and_prior_feedback():
    llm = FakeLLM({'approved': True, 'issues': [], 'verifiability': 'ok',
                   'test_validity': 'valid', 'test_issues': [], 'implementation_valid': True})
    CriticAgent(llm).run(_state(critic_feedback=['add a negative-b test']))
    import json
    payload = json.loads(llm.calls[0][1])
    assert payload['diagnosis']['root_cause'] == 'No zero guard'
    assert 'app.py' in payload['changed_files']
    assert payload['last_test_result']['outcome'] == 'passed'
    assert payload['prior_critic_feedback'] == ['add a negative-b test']


def test_unresolved_interface_check_reaches_critic_and_persisted_verdict():
    state = _state()
    interface_result = SimpleNamespace(
        skipped_checks=['ExternalResult.passed'],
        unresolved_types=['ExternalResult'],
    )
    _record_interface_uncertainties(state, interface_result, 'tests/test_service.py')

    # The typed blackboard field survives the same JSON round-trip used by DB
    # checkpoints before the Critic receives it.
    state = SubtaskState.model_validate(state.model_dump(mode='json'))
    llm = FakeLLM({
        'approved': True,
        'issues': [],
        'verifiability': 'ok',
        'test_validity': 'valid',
        'test_issues': [],
        'implementation_valid': True,
    })
    result = CriticAgent(llm).run(state)

    import json
    payload = json.loads(llm.calls[0][1])
    unresolved = payload['checks_that_could_not_be_statically_verified']
    assert unresolved == [{
        'owner': 'ExternalResult',
        'attribute': 'passed',
        'reason': 'Interface manifest entry for ExternalResult could not be resolved',
        'source': 'tests/test_service.py',
        'impact': 'Use of ExternalResult.passed in the generated test was not statically verified',
    }]
    verdict = CriticVerdict.model_validate(result.critic_verdict)
    assert verdict.approved is True
    assert verdict.confidence == 'medium'
    assert verdict.unresolved_checks == [UnresolvedCheck.model_validate(unresolved[0])]


def test_critic_cannot_treat_an_unresolved_check_as_high_confidence_pass():
    unresolved = UnresolvedCheck(
        owner='ExternalResult', attribute='passed', reason='type could not be resolved',
        source='tests/test_service.py', impact='result.passed was not statically verified',
    )
    with pytest.raises(Exception, match='cannot claim high or unspecified confidence'):
        CriticVerdict.model_validate({
            'approved': True,
            'issues': [],
            'verifiability': 'ok',
            'test_validity': 'valid',
            'test_issues': [],
            'implementation_valid': True,
            'confidence': 'high',
            'unresolved_checks': [unresolved.model_dump()],
        })


def test_critic_verdict_rejects_invalid_verifiability_value():
    with pytest.raises(Exception):
        CriticVerdict.model_validate({'approved': True, 'issues': [], 'verifiability': 'sort-of'})


# --- Graph integration -------------------------------------------------------

@pytest.fixture
def gstate():
    TicketBudget.__table__.create(engine, checkfirst=True)
    tid, sid = uuid.uuid4(), uuid.uuid4()
    state = SubtaskState(ticket_id=str(tid), subtask_id=str(sid), subtask_type='bug', jira_key='TEST-7',
                         repo='owner/repo', confirmed_repos=['owner/repo'], description='Guard division',
                         approval_status='approved', base_commit='base-sha',
                         plan=[Step(step_id='1', intent='Guard b == 0', target_file='app.py')])
    with SessionLocal() as db:
        db.add(Ticket(id=tid, source='jira', title='Critic test', description='Test', status='processing'))
        db.flush()
        db.add(Subtask(id=sid, ticket_id=tid, type='bug', description='Guard division', status='running', state=state.model_dump()))
        db.commit()
    yield state
    with SessionLocal() as db:
        db.delete(db.get(Ticket, tid))
        db.commit()


def _graph(agent, executor, critic):
    jira = SimpleNamespace(set_status=Mock(return_value={'applied': True}), comment=Mock())
    publisher = SimpleNamespace(publish_changes=Mock(return_value={
        'id': '123', 'number': 1, 'url': 'https://github.com/owner/repo/pull/1',
        'repo': 'owner/repo', 'branch': 'sdlc/test', 'state': 'open'}))
    g = module.build_graph(agent=agent, planner=_planner_stub(), decomposer=_decomposer_stub(), executor=executor,
                           critic=critic, publisher=publisher, checkpointer=MemorySaver(), jira=jira,
                           memory_search=lambda *a, **k: [], activity_check=lambda _: None)
    return g, jira, publisher


def _decomposer_stub():
    # This graph's plan already lives on the fixture state; the Planner just
    # needs to confirm "one sub-task = the whole ticket" without a real repo.
    def run(state):
        state.subtask_specs = [{'spec_id': '1', 'type': state.subtask_type, 'description': state.description,
                                'repo': state.repo, 'depends_on': []}]
        state.decomposition_reasoning = 'One sub-task.'
        return state
    return SimpleNamespace(run=Mock(side_effect=run))


def _diagnosis_stub():
    def run(state):
        state.diagnosis = {'root_cause': 'No zero guard', 'files': ['app.py'], 'reasoning': 'x'}
        return state
    return SimpleNamespace(run=Mock(side_effect=run))


def _planner_stub():
    # The graph always (re)runs step_planner on entry; keep the plan already on
    # the fixture state instead of hitting a real repo.
    def run(state):
        return state
    return SimpleNamespace(run=Mock(side_effect=run))


def _complete_executor():
    async def run(state):
        state.current_step = len(state.plan)
        state.execution_complete = True
        state.steps_done = [{'step_id': '1', 'intent': 'x', 'target_file': 'app.py', 'content': 'y',
                             'before_sha256': 'a', 'report': {'matches': []},
                             'tests': {'passed': True, 'returncode': 0, 'output': '', 'outcome': 'passed'}}]
        state.file_changes = {'app.py': 'y'}
        return state
    return SimpleNamespace(run=Mock(side_effect=run))


from app.agents.planning import ApprovalDecision


async def _approve(graph, state):
    config = module.thread_config(state.ticket_id, state.subtask_id)
    approved = ApprovalDecision(approval_status='approved')
    await graph.ainvoke(state.model_dump(), config)
    assert (await graph.aget_state(config)).next == ('intent_gate',)  # Planner confirms first
    await module.resume_approval(graph, state.ticket_id, state.subtask_id, approved)
    assert (await graph.aget_state(config)).next == ('human_gate',)  # then the plan gate
    return await module.resume_approval(graph, state.ticket_id, state.subtask_id, approved)


@pytest.mark.asyncio
async def test_critic_approves_and_uses_the_shared_publish_step(gstate):
    approving = SimpleNamespace(run=Mock(side_effect=lambda s: (
        setattr(s, 'critic_verdict', {'approved': True, 'issues': [], 'verifiability': 'ok',
                                      'test_validity': 'valid', 'test_issues': [],
                                      'implementation_valid': True}), s)[1]))
    g, jira, publisher = _graph(_diagnosis_stub(), _complete_executor(), approving)
    result = await _approve(g, gstate)
    assert result.status == 'in_review'
    assert result.pr_url == 'https://github.com/owner/repo/pull/1'
    assert approving.run.call_count == 1
    publisher.publish_changes.assert_called_once()
    assert any('Critic' in str(c) for c in jira.comment.call_args_list)


@pytest.mark.asyncio
async def test_critic_rejection_sends_it_back_to_executor_with_issues(gstate):
    verdicts = iter([
        {'approved': False, 'issues': ['Add a negative-b test'], 'test_validity': 'valid',
         'test_issues': [], 'implementation_valid': False},
        {'approved': True, 'issues': [], 'verifiability': 'ok', 'test_validity': 'valid',
         'test_issues': [], 'implementation_valid': True},
    ])
    seen_feedback = []
    seen_history = []
    def run(state):
        seen_feedback.append(list(state.critic_feedback))
        seen_history.append(list(state.attempt_history))
        state.critic_verdict = next(verdicts)
        return state
    critic = SimpleNamespace(run=Mock(side_effect=run))
    executor = _complete_executor()
    g, jira, publisher = _graph(_diagnosis_stub(), executor, critic)
    result = await _approve(g, gstate)
    assert result.status == 'in_review'
    assert critic.run.call_count == 2
    assert executor.run.call_count == 2  # redone once after the rejection
    assert result.critic_retry_count == 1
    assert seen_feedback == [[], ['Add a negative-b test']]  # 2nd pass saw the 1st rejection's issues
    assert seen_history == [[], ['Critic rejection 1: Add a negative-b test']]
    assert any('requested changes' in str(c).lower() for c in jira.comment.call_args_list)


@pytest.mark.asyncio
async def test_critic_rejection_cap_escalates_to_needs_human(gstate):
    def always_reject(state):
        state.critic_verdict = {'approved': False, 'issues': ['Still missing coverage'],
                                'verifiability': 'uncovered_change', 'test_validity': 'valid',
                                'test_issues': [], 'implementation_valid': False}
        return state
    critic = SimpleNamespace(run=Mock(side_effect=always_reject))
    executor = _complete_executor()
    g, jira, publisher = _graph(_diagnosis_stub(), executor, critic)
    result = await _approve(g, gstate)
    assert result.status == 'needs_human'
    assert 'Critic rejected' in result.failure_reason
    assert 'Still missing coverage' in result.failure_reason
    assert len(result.attempt_history) == settings.max_agent_retries + 1
    assert critic.run.call_count == settings.max_agent_retries + 1
    publisher.publish_changes.assert_not_called()
    jira.set_status.assert_called_with('TEST-7', 'blocked')
