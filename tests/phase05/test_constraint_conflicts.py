"""Authoritative intent conflicts are human decisions, never Executor retries."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
from uuid import UUID

import pytest
from httpx import AsyncClient, ASGITransport
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from app.agents.constraints import ExecutionConstraint, ConstraintChange
from app.agents.executor import ExecutorAgent
from app.agents.planning import Step, ApprovalDecision
from app.agents.state import SubtaskState
from app.config import settings
from app.core.constraint_conflicts import check_constraint_conflicts, normalized_behaviors, resolve_constraint_conflicts
from app.core.execution_constraints import append_constraints, ensure_requirement, record_decision, scoped_constraints, inherited_ticket_constraints
from app.orchestrator import graph as module
from tests.phase05.test_execution import no_event, local_with_scenario_check, local_with_style_example
from tests.phase06.test_guards import state


def work():
    return SubtaskState(ticket_id='ticket', subtask_id='task', subtask_type='bug', repo='org/repo',
                        description='Cover string inputs', approval_status='approved', plan=[
                            Step(step_id='1', intent='check', target_file='a.py', target_symbols=['check']),
                            Step(step_id='2', intent='other', target_file='b.py', target_symbols=['other']),
                        ])


def constraint(outcome='PASS', scope='subtask', value='task', source='human_approval_note', provenance='gate:1', subject='Empty strings'):
    return ExecutionConstraint(source=source, text=f'{subject} must {outcome}.', scope_type=scope,
                               scope_value=value, provenance=provenance)


def test_identical_records_dedupe_across_capture_and_payload():
    s = work(); c = constraint()
    append_constraints(s.execution_constraints, [c, c.model_copy()])
    assert len(s.execution_constraints) == 1
    s.execution_constraints.append(c.model_copy())  # Older checkpoints can contain duplicates.
    assert len(scoped_constraints(s, s.plan[0])) == 1
    assert not check_constraint_conflicts(s)
    assert c.constraint_id == ExecutionConstraint.model_validate_json(c.model_dump_json()).constraint_id


def test_same_text_with_distinct_provenance_is_duplicate_without_losing_provenance():
    s = work(); s.execution_constraints = [constraint(), constraint(provenance='gate:2')]
    assert not check_constraint_conflicts(s)
    assert s.constraint_relationships[0].status == 'duplicate'
    assert len(s.execution_constraints) == 2


def test_equivalent_normalized_behavior_is_compatible():
    s = work(); s.execution_constraints = [constraint(), constraint().model_copy(update={'text': 'Empty strings should be allowed.'})]
    assert not check_constraint_conflicts(s)
    assert s.constraint_relationships[0].status == 'compatible'


@pytest.mark.parametrize('a,b', [
    (('subtask', 'task'), ('subtask', 'task')),
    (('ticket', 'ticket'), ('subtask', 'task')),
    (('ticket', 'ticket'), ('file', 'a.py')),
    (('step', '1'), ('file', 'a.py')),
    (('file', 'a.py'), ('symbol', 'check')),
    (('symbol', 'check'), ('symbol', 'check')),
])
def test_proven_scope_overlap_blocks_opposing_behavior(a, b):
    s = work(); s.execution_constraints = [constraint('PASS', *a), constraint('FAIL', *b)]
    assert check_constraint_conflicts(s)
    assert s.status == 'needs_human' and s.retry_count == 0
    conflict = s.constraint_conflicts[0]
    assert conflict.constraint_ids == [c.constraint_id for c in s.execution_constraints]
    assert [b.outcome for b in conflict.normalized_behaviors] == ['PASS', 'FAIL']
    assert conflict.affected_targets[0]['subtask_id'] == 'task'
    assert conflict.constraints[0].provenance == 'gate:1'
    assert conflict.status == 'active'
    assert not s.failure_contexts and not s.retry_attempts


@pytest.mark.parametrize('a,b', [
    (('file', 'a.py'), ('file', 'b.py')),
    (('step', '1'), ('step', '2')),
    (('symbol', 'check'), ('symbol', 'other')),
    (('ticket', 'other-ticket'), ('subtask', 'task')),
    (('subtask', 'other-task'), ('file', 'a.py')),
])
def test_unrelated_scope_does_not_conflict(a, b):
    s = work(); s.execution_constraints = [constraint('PASS', *a), constraint('FAIL', *b)]
    assert not check_constraint_conflicts(s)
    assert s.constraint_relationships[0].status == 'non_overlapping'


@pytest.mark.parametrize('text', [
    'Improve how empty strings behave', 'Empty strings should pass when configured',
    'It is false that empty strings should pass', 'Empty strings should not be allowed',
])
def test_arbitrary_text_remains_unknown(text):
    s = work(); s.execution_constraints = [constraint('FAIL'), constraint().model_copy(update={'text': text})]
    assert not check_constraint_conflicts(s)
    assert s.constraint_relationships[0].status == 'unknown'


def test_different_behavior_subjects_are_not_compared():
    s = work(); s.execution_constraints = [constraint(), constraint('FAIL', subject='Empty forbidden phrases')]
    assert not check_constraint_conflicts(s)
    assert s.constraint_relationships[0].status == 'unknown'


def test_unknown_cross_scope_without_plan_is_not_guessed():
    s = work(); s.plan = []
    s.execution_constraints = [constraint('PASS', 'file', 'a.py'), constraint('FAIL', 'symbol', 'check')]
    assert not check_constraint_conflicts(s)
    assert s.constraint_relationships[0].status == 'unknown'


def test_conflicting_clauses_in_one_authoritative_note_are_detected():
    s = work(); s.execution_constraints = [constraint().model_copy(update={'text': 'Empty strings must PASS. Empty strings must FAIL.'})]
    assert check_constraint_conflicts(s)
    assert len(s.constraint_conflicts) == 1


@pytest.mark.parametrize('source', ['critic_correction', 'prior_attempt'])
def test_advisory_sources_do_not_become_authoritative(source):
    s = work(); s.execution_constraints = [constraint(), constraint('FAIL', source=source)]
    s.attempt_history = ['Empty strings must FAIL.']
    s.prior_attempt = {'execution_constraints': [constraint('FAIL').model_dump()]}
    assert not check_constraint_conflicts(s)


@pytest.mark.asyncio
async def test_executor_stops_before_any_llm_or_repository_work():
    s = work(); s.execution_constraints = [constraint(), constraint('FAIL')]
    llm, repo = Mock(), Mock()
    result = await ExecutorAgent(llm, repo, emit=no_event).run(s)
    assert result.status == 'needs_human'
    assert not llm.mock_calls and not repo.mock_calls
    assert result.retry_count == 0 and not result.retry_attempts and not result.failure_contexts
    assert not result.file_changes


@pytest.mark.parametrize('replacement', [None, 'Empty strings should pass.', 'The intended string handling is clarified by the approved plan.'])
def test_explicit_resolution_and_tombstone_survive_roundtrip(replacement):
    s = work(); s.description = 'Empty strings must FAIL.'
    ensure_requirement(s)
    old = s.execution_constraints[0]
    append_constraints(s.execution_constraints, [constraint()])
    assert check_constraint_conflicts(s)
    resolve_constraint_conflicts(s, [ConstraintChange(constraint_id=old.constraint_id, replacement_text=replacement)],
                                'Allow empty strings; withdraw or replace the old requirement.', 'human:decision:1')
    s = SubtaskState.model_validate_json(s.model_dump_json())
    ensure_requirement(s)
    record_decision(s, ApprovalDecision(approval_status='approved'))
    assert not check_constraint_conflicts(s)
    assert old.constraint_id in s.withdrawn_constraint_ids
    assert old.text not in [item['text'] for item in scoped_constraints(s)]
    assert s.constraint_conflicts[0].status == 'resolved'
    assert s.constraint_resolutions[0].provenance == 'human:decision:1'


def test_still_conflicting_resolution_is_atomic_and_never_latest_wins():
    s = work(); s.execution_constraints = [constraint(), constraint('FAIL')]
    check_constraint_conflicts(s); before = s.model_dump_json()
    with pytest.raises(ValueError, match='still contains'):
        resolve_constraint_conflicts(s, [ConstraintChange(constraint_id=s.execution_constraints[1].constraint_id,
                                    replacement_text='Empty strings should fail.')], 'Latest note', 'gate:latest')
    assert s.model_dump_json() == before


def test_plain_approval_note_cannot_silently_replace_old_intent():
    s = work(); s.execution_constraints = [constraint()]
    record_decision(s, ApprovalDecision(approval_status='approved', note='Empty strings must FAIL.'))
    assert s.status == 'needs_human' and any(c.status == 'active' for c in s.constraint_conflicts)


def test_withdrawn_ticket_constraint_is_not_inherited():
    s = work(); c = constraint(scope='ticket', value='ticket')
    s.execution_constraints = [c]; s.withdrawn_constraint_ids = [c.constraint_id]
    assert inherited_ticket_constraints(s) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['memory', 'postgres'])
@pytest.mark.parametrize('replan', [False, True])
async def test_conflict_checkpoint_restart_human_resolution_and_reapproval(state, backend, replan, monkeypatch):
    from app.web import escalations
    from app.main import app
    monkeypatch.setattr(settings, 'ui_basic_auth_username', '')
    monkeypatch.setattr(settings, 'ui_basic_auth_password', '')
    state.description = 'Empty strings must PASS.'
    state.plan = [Step(step_id='1', intent='Cover input behavior', target_file='a.py')]
    state.approval_status = 'approved'
    state.execution_constraints = [constraint(value=state.subtask_id), constraint('FAIL', value=state.subtask_id)]
    state.guard_node = 'execute'
    assert check_constraint_conflicts(state)
    state.diagnosis = {'root_cause': 'Cover inputs', 'files': ['a.py'], 'reasoning': 'Tests needed'}
    memory = MemorySaver()
    calls, plans = [], []
    async def execute(s):
        calls.append(s.model_copy(deep=True))
        s.status, s.failure_reason = 'needs_human', 'Reached Executor after human resolution'
        return s
    async def gate(*args, **kwargs):
        pass
    monkeypatch.setattr('app.core.freshness.check_freshness', lambda s, repo: s)
    def plan(s):
        plans.append(s.model_copy(deep=True))
        return s
    def build(saver):
        return module.build_graph(checkpointer=saver, executor=SimpleNamespace(run=execute), planner=SimpleNamespace(run=plan),
                                  repo_tool=SimpleNamespace(cleanup_workspace=lambda *_: None),
                                  jira=SimpleNamespace(comment=lambda *a: None, set_status=lambda *a: {}),
                                  memory_writer=lambda *a: None, gate_store=gate, activity_check=lambda s: None)
    @asynccontextmanager
    async def connection():
        if backend == 'memory':
            yield memory
        else:
            async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
                await saver.setup()
                yield saver
    config = module.thread_config(state.ticket_id, state.subtask_id)
    async with connection() as saver:
        graph = build(saver)
        await graph.aupdate_state(config, state.model_dump(mode='json'), as_node='execute')
        await graph.ainvoke(None, config)
        first = await graph.aget_state(config)
        assert first.next == ('human_resolution',) and not calls
    async with connection() as saver:
        graph = build(saver)
        snapshot = await graph.aget_state(config)
        assert snapshot.values['constraint_conflicts'] == first.values['constraint_conflicts']
        @asynccontextmanager
        async def opened(*args, **kwargs):
            yield graph
        monkeypatch.setattr(escalations, 'open_graph', opened)
        client = AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver')
        page = await client.get('/escalations')
        assert page.status_code == 200
        assert 'constraint-resolution-mount' in page.text
        assert 'Empty strings must FAIL.' in page.text
        assert 'constraint_resolution.js' in page.text
        url = f'/tickets/{state.ticket_id}/subtasks/{state.subtask_id}/escalation/'
        identity = {'escalation_id': snapshot.values['escalation_id'], 'note': 'Empty strings are allowed.'}
        assert (await client.post(url + 'retry', json=identity)).status_code == 409
        assert (await client.post(url + 'resolve_constraints', json=identity)).status_code == 422
        changes = [{'constraint_id': state.execution_constraints[1].constraint_id}]
        response = await client.post(url + 'resolve_constraints', json={**identity, 'constraint_changes': changes, 'replan': replan})
        assert response.status_code == 200, response.text
        paused = await graph.aget_state(config)
        assert paused.next == ('human_gate',) and not calls
        assert len(plans) == int(replan)
        assert paused.values['approval_status'] == 'pending'
        assert paused.values['constraint_conflicts'][0]['status'] == 'resolved'
        assert (await client.post(url + 'resolve_constraints', json={**identity, 'constraint_changes': changes})).status_code == 409
        await module.resume_approval(graph, state.ticket_id, state.subtask_id, ApprovalDecision(approval_status='approved'))
        assert len(calls) == 1
        assert not any(c.status == 'active' for c in calls[0].constraint_conflicts)
        assert calls[0].retry_count == 0
        await client.aclose()
        if backend == 'postgres':
            await saver.adelete_thread(config['configurable']['thread_id'])


@pytest.mark.parametrize('a,b', [
    (('file', 'unplanned.py'), ('ticket', 'ticket')),
    (('file', 'a.py'), ('step', '2')),
    (('file', 'a.py'), ('symbol', 'other')),
])
def test_no_approved_plan_target_in_common_means_no_conflict(a, b):
    s = work(); s.execution_constraints = [constraint('PASS', *a), constraint('FAIL', *b)]
    assert not check_constraint_conflicts(s)
    assert s.constraint_relationships[0].status == 'non_overlapping'


def test_same_behavior_across_broad_and_narrow_scope_is_compatible():
    s = work(); s.execution_constraints = [constraint(scope='ticket', value='ticket'), constraint()]
    assert not check_constraint_conflicts(s)
    assert s.constraint_relationships[0].status == 'compatible'


@pytest.mark.asyncio
async def test_conflicting_approval_note_interrupts_before_executor(state):
    state.description = 'Empty strings must PASS.'
    state.plan = [Step(step_id='1', intent='Cover strings', target_file='a.py')]
    state.approval_payload = {'plan': [step.model_dump() for step in state.plan]}
    state.approval_status = 'pending'
    state.guard_node = 'prepare_approval'
    executor = SimpleNamespace(run=AsyncMock())
    graph = module.build_graph(checkpointer=MemorySaver(), executor=executor,
                               repo_tool=SimpleNamespace(cleanup_workspace=lambda *_: None),
                               jira=SimpleNamespace(comment=lambda *a: None, set_status=lambda *a: {}),
                               memory_writer=lambda *a: None, activity_check=lambda s: None)
    config = module.thread_config(state.ticket_id, state.subtask_id)
    await graph.aupdate_state(config, state.model_dump(mode='json'), as_node='prepare_approval')
    await graph.ainvoke(None, config)
    result = await module.resume_approval(graph, state.ticket_id, state.subtask_id,
                                         ApprovalDecision(approval_status='approved', note='Empty strings must FAIL.'))
    assert result.status == 'needs_human'
    assert (await graph.aget_state(config)).next == ('human_resolution',)
    executor.run.assert_not_called()
    assert not result.retry_attempts and result.retry_count == 0 and not result.failure_contexts
    assert {c.source for c in result.constraint_conflicts[0].constraints} == {'ticket_requirement', 'human_approval_note'}


def test_withdrawn_original_description_is_not_redelivered_as_intent():
    from app.core.execution_constraints import constraint_description
    s = work(); s.description = 'Empty strings must FAIL.'
    ensure_requirement(s); original = s.execution_constraints[0]
    append_constraints(s.execution_constraints, [constraint()])
    check_constraint_conflicts(s)
    resolve_constraint_conflicts(s, [ConstraintChange(constraint_id=original.constraint_id)], 'Keep PASS.', 'human:1')
    assert 'FAIL' not in constraint_description(s)
    assert s.description == original.text  # Audit remains intact.


@pytest.mark.asyncio
async def test_resolved_intent_reaches_real_executor_without_old_description(local_with_scenario_check):
    from tests.phase05.test_retry_evidence import Candidates
    from app.tools.test_runner import run_tests
    tool, s, _ = local_with_scenario_check
    s.description = 'Empty strings should fail.'
    ensure_requirement(s); original = s.execution_constraints[0]
    s.execution_constraints.append(constraint(value=s.subtask_id).model_copy(update={'text': 'Empty strings should pass.'}))
    assert check_constraint_conflicts(s)
    resolve_constraint_conflicts(s, [ConstraintChange(constraint_id=original.constraint_id)], 'Keep empty strings allowed.', 'human:1')
    # The graph tests above prove that this approval requires the resumed human gate.
    s.status = 'running'
    record_decision(s, ApprovalDecision(approval_status='approved'))
    candidate = 'from app import check\ndef test_empty():\n    result = check("")\n    assert result.passed\n'
    llm = Candidates([{'full_content': candidate}])
    before = (tool.remote / 'app.py').read_bytes()
    result = await ExecutorAgent(llm, tool, test_runner=run_tests, emit=no_event).run(s)
    assert result.execution_complete, result.failure_reason
    assert len(llm.prompts) == 1 and result.retry_count == 0
    assert original.text not in llm.prompts[0]['description']
    assert original.text not in [item['text'] for item in llm.prompts[0]['execution_constraints']]
    assert llm.prompts[0]['constraint_resolutions'][0]['provenance'] == 'human:1'
    assert (tool.remote / 'app.py').read_bytes() == before


def test_new_human_approval_cannot_silently_reuse_withdrawn_note_identity():
    s = work(); s.description = 'Empty strings should pass.'
    decision = ApprovalDecision(approval_status='approved', note='Empty strings should fail.')
    record_decision(s, decision)
    old = next(c for c in s.execution_constraints if c.source == 'human_approval_note')
    resolve_constraint_conflicts(s, [ConstraintChange(constraint_id=old.constraint_id)], 'Withdraw FAIL.', 'resolution:1')
    record_decision(s, decision)  # A later explicit approval must be checked again.
    assert check_constraint_conflicts(s)
    active_ids = {identifier for c in s.constraint_conflicts if c.status == 'active' for identifier in c.constraint_ids}
    assert old.constraint_id not in active_ids


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['critic_correction', 'prior_attempt'])
async def test_advisory_cannot_disable_authoritative_test_validation(local_with_scenario_check, source):
    from tests.phase05.test_retry_evidence import Candidates
    from app.tools.test_runner import run_tests
    tool, s, _ = local_with_scenario_check
    s.description = 'Empty strings should pass.'
    s.execution_constraints.append(constraint('FAIL', value=s.subtask_id, source=source).model_copy(
        update={'text': 'Empty strings should fail.'}))
    bad = 'from app import check\ndef test_empty():\n    result = check("")\n    assert not result.passed\n'
    good = bad.replace('assert not result.passed', 'assert result.passed')
    llm = Candidates([{'full_content': bad}, {'full_content': good}])
    result = await ExecutorAgent(llm, tool, test_runner=run_tests, emit=no_event).run(s)
    assert result.execution_complete, result.failure_reason
    assert len(llm.prompts) == 2
    assert result.retry_attempts['1'][0].failure_type == 'RequirementContradictionError'
    assert not result.constraint_conflicts
