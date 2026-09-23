"""Accumulated evidence, exact duplicate rejection, and checkpointed retry budgets."""
from contextlib import asynccontextmanager
import hashlib
import json

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, StateGraph

from app.agents.execution import EditProposal
from app.agents.executor import ExecutorAgent
from app.agents.state import RetryAttempt, SubtaskState
from app.config import settings
from app.core.guard import validate_output
from app.core.retry_evidence import DuplicateCandidate, fingerprint, preview, proposal_content, reject_duplicate
from app.tools.test_runner import TestResult
from tests.phase05.test_execution import LLM, local, local_with_style_example, no_event


class Candidates(LLM):
    def __init__(self, candidates):
        super().__init__()
        self.candidates = candidates

    def complete_json(self, system, user, schema, **kwargs):
        self.prompts.append(json.loads(user))
        candidate = self.candidates[min(self.calls, len(self.candidates) - 1)]
        self.calls += 1
        return schema.model_validate(candidate)


def edit(search, replace='    return a / b'):
    return {'blocks': [{'search': search, 'replace': replace}]}


GOOD = edit('    return a / b', '    if b == 0:\n        raise ValueError("zero")\n    return a / b')
BAD = edit('a completely missing candidate')


def passing(_checkout):
    return TestResult(passed=True, returncode=0, output='1 passed')


@pytest.fixture(autouse=True)
def retry_limit(monkeypatch):
    monkeypatch.setattr(settings, 'max_agent_retries', 2)


@pytest.mark.asyncio
async def test_attempt_three_receives_both_failures_within_one_invocation(local):
    tool, state, _ = local
    llm = Candidates([BAD, edit('a different absent region'), GOOD])
    result = await ExecutorAgent(llm, tool, test_runner=passing, emit=no_event).run(state)
    assert result.execution_complete, result.failure_reason
    assert [len(p['retry_attempts']) for p in llm.prompts] == [0, 1, 2]
    history = llm.prompts[2]['retry_attempts']
    assert [entry['attempt_number'] for entry in history] == [1, 2]
    assert [entry['failure_type'] for entry in history] == ['NoMatch', 'NoMatch']
    assert 'a completely missing candidate' in history[0]['candidate_content']
    assert 'a different absent region' in history[1]['candidate_content']
    assert all(entry['failure_evidence']['closest_source'] for entry in history)
    assert llm.prompts[2]['repair_feedback'].count('SEARCH not found') == 2
    assert llm.prompts[2]['file_text'] == 'def divide(a, b):\n    return a / b\n'
    assert llm.prompts[2]['execution_constraints'][0]['text'] == state.description


@pytest.mark.asyncio
@pytest.mark.parametrize('finish', [True, False])
async def test_repeated_edit_is_not_applied_and_consumes_budget(local, monkeypatch, finish):
    import app.agents.executor as module
    tool, state, _ = local
    applied = []
    real = module.apply_edits
    def apply(before, blocks):
        applied.append(blocks)
        return real(before, blocks)
    monkeypatch.setattr(module, 'apply_edits', apply)
    llm = Candidates([BAD, BAD, GOOD if finish else BAD])
    result = await ExecutorAgent(llm, tool, test_runner=passing, emit=no_event).run(state)
    assert llm.calls == 3
    assert len(applied) == (2 if finish else 1)
    assert result.execution_complete == finish
    history = result.retry_attempts['1']
    assert history[1].failure_type == 'DuplicateCandidate'
    assert history[1].failure_evidence['duplicate_of_attempt'] == 1
    assert 'materially different' in llm.prompts[2]['retry_attempts'][1]['corrective_instruction']
    if not finish:
        assert result.status == 'needs_human'
        assert len(history) == settings.max_agent_retries + 1
        again = Candidates([GOOD])
        await ExecutorAgent(again, tool, emit=no_event).run(result)
        assert again.calls == 0  # Re-entry never replenishes an exhausted budget.


@pytest.mark.asyncio
async def test_repeated_generated_test_rejected_before_validation_or_execution(local_with_style_example, monkeypatch):
    import app.agents.executor as module
    tool, state, _ = local_with_style_example
    invalid = (
        'class Check:\n'
        '    def __init__(self, forbidden): self.forbidden = forbidden\n'
        '    def check(self, text): return type("Result", (), {"passed": False})()\n'
        'def test_bad():\n'
        '    checker = Check(forbidden=["forbidden"])\n'
        '    result = checker.check("contains forbidden")\n'
        '    assert result.passed\n'
    )
    good = 'from app import divide\ndef test_divide():\n    assert divide(6, 2) == 3\n'
    llm = Candidates([{'full_content': invalid}, {'full_content': invalid}, {'full_content': good}])
    validated, ran = [], []
    real = module.validate_test_validity
    def validate(content, requirement):
        validated.append(content)
        return real(content, requirement)
    monkeypatch.setattr(module, 'validate_test_validity', validate)
    def runner(checkout):
        ran.append(checkout)
        return passing(checkout)
    result = await ExecutorAgent(llm, tool, test_runner=runner, emit=no_event).run(state)
    assert result.execution_complete, result.failure_reason
    assert validated == [invalid, good]
    assert len(ran) == 1
    assert result.retry_attempts['1'][1].failure_type == 'DuplicateCandidate'
    assert result.retry_attempts['1'][0].generated_test_fingerprint == fingerprint(invalid)
    assert 'app.py' not in result.file_changes


def test_fingerprint_is_stable_normalized_and_content_complete():
    proposal = EditProposal.model_validate(edit('a\r\nb', 'c\r\nd'))
    other = EditProposal.model_validate(edit('a\nb', 'c\nd'))
    content = proposal_content(' EDIT ', './app.py', proposal)
    assert content == proposal_content('edit', 'app.py', other)
    assert fingerprint(content) == hashlib.sha256(content.encode()).hexdigest()
    assert fingerprint('test\r\nbody') == fingerprint('test\nbody')
    assert fingerprint(' x') != fingerprint('x')  # Preserve meaningful indentation.
    assert fingerprint(content) != fingerprint(proposal_content('create', 'app.py', other))
    assert fingerprint(content) != fingerprint(proposal_content('edit', 'other.py', other))
    assert fingerprint(content) != fingerprint(proposal_content('edit', 'app.py', EditProposal.model_validate(edit('a\nb', 'changed'))))
    large = 'x' * 9000 + 'tail'
    shown, truncated = preview(large)
    assert len(shown) == 8000 and truncated and shown.endswith('tail')
    changed_middle = large[:4500] + 'y' + large[4501:]
    assert preview(changed_middle) == (shown, True)
    assert fingerprint(large) != fingerprint(changed_middle)


def test_duplicate_test_content_check_is_pure_and_accepts_different_content():
    history = [RetryAttempt(attempt_number=1, operation='validate_test_semantics', target='test_x.py',
        candidate_type='generated_test', candidate_fingerprint='different proposal',
        generated_test_fingerprint=fingerprint('assert False\n'), candidate_content='assert False\n',
        failure_type='InvalidGeneratedTestError', failure_reason='always fails',
        corrective_instruction='Write a meaningful assertion')]
    with pytest.raises(DuplicateCandidate):
        reject_duplicate(history, fingerprint('assert False\r\n'), generated_test=True)
    reject_duplicate(history, fingerprint('assert result == 3\n'), generated_test=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['memory', 'postgres'])
async def test_checkpoint_restart_keeps_history_fingerprints_and_remaining_budget(local, backend):
    tool, initial, _ = local
    saver = MemorySaver()
    first_llm = Candidates([BAD])
    def build(llm, saver):
        graph = StateGraph(dict)
        agent = ExecutorAgent(llm, tool, test_runner=passing, emit=no_event, checkpoint_retries=True)
        async def execute(raw):
            state = SubtaskState.model_validate(raw)
            previous = state.model_copy(deep=True)
            updated = await agent.run(state)
            return validate_output(previous, updated, 'execute').model_dump(mode='json')
        graph.add_node('execute', execute)
        graph.add_node('between_attempts', lambda raw: raw)
        graph.set_entry_point('execute')
        graph.add_conditional_edges('execute', lambda raw: END if raw['execution_complete'] or raw['status'] != 'running' else 'between_attempts')
        graph.add_edge('between_attempts', 'execute')
        return graph.compile(checkpointer=saver, interrupt_before=['between_attempts'])
    config = {'configurable': {'thread_id': initial.subtask_id}}
    @asynccontextmanager
    async def connection():
        if backend == 'memory':
            yield saver
        else:
            async with AsyncPostgresSaver.from_conn_string(settings.database_url) as postgres:
                await postgres.setup()
                yield postgres

    async with connection() as first_saver:
        graph = build(first_llm, first_saver)
        await graph.ainvoke(initial.model_dump(mode='json'), config)
        saved = SubtaskState.model_validate((await graph.aget_state(config)).values)
        assert len(saved.retry_attempts['1']) == 1
        assert first_llm.calls == 1
    # New graph, Executor, and (for Postgres) connection: only checkpoint state survives.
    second_llm = Candidates([BAD, GOOD])
    async with connection() as reopened_saver:
        try:
            resumed = build(second_llm, reopened_saver)
            await resumed.ainvoke(None, config)
            snapshot = SubtaskState.model_validate((await resumed.aget_state(config)).values)
            assert snapshot.retry_attempts['1'][1].failure_type == 'DuplicateCandidate'
            assert len(second_llm.prompts[0]['retry_attempts']) == 1
            await resumed.ainvoke(None, config)
            final = SubtaskState.model_validate((await resumed.aget_state(config)).values)
            assert final.execution_complete, final.failure_reason
            assert second_llm.calls == 2
            assert len(second_llm.prompts[1]['retry_attempts']) == 2
        finally:
            if backend == 'postgres':
                await reopened_saver.adelete_thread(config['configurable']['thread_id'])



@pytest.mark.asyncio
async def test_new_step_does_not_inherit_failed_candidates(local):
    tool, state, _ = local
    await ExecutorAgent(Candidates([BAD]), tool, emit=no_event).run(state)
    state.plan[0].step_id = 'new-step'
    state.status = 'running'
    llm = Candidates([BAD, GOOD])
    result = await ExecutorAgent(llm, tool, test_runner=passing, emit=no_event).run(state)
    assert result.execution_complete
    assert llm.prompts[0]['retry_attempts'] == []
    assert result.retry_attempts['new-step'][0].failure_type == 'NoMatch'


@pytest.mark.asyncio
async def test_infrastructure_failure_does_not_enter_retry_evidence(local, monkeypatch):
    tool, state, _ = local
    def crash(*args, **kwargs):
        raise OSError('write service unavailable')
    monkeypatch.setattr(tool, 'write_execution_file', crash)
    llm = Candidates([GOOD])
    result = await ExecutorAgent(llm, tool, emit=no_event).run(state)
    assert llm.calls == 1
    assert result.retry_attempts['1'] == []
    assert result.retry_count == 0
    assert result.failure_contexts[-1].classification == 'infrastructure'


@pytest.mark.asyncio
async def test_malformed_output_has_evidence_but_no_stale_candidate(local):
    tool, state, _ = local
    llm = Candidates([BAD, {}, GOOD])
    result = await ExecutorAgent(llm, tool, test_runner=passing, emit=no_event).run(state)
    assert result.execution_complete
    invalid = result.retry_attempts['1'][1]
    assert invalid.candidate_fingerprint is None
    assert invalid.candidate_content == ''
    assert invalid.failure_type == 'ValidationError'


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [0, 1, 5])
async def test_configured_bound_includes_duplicates(local, monkeypatch, limit):
    tool, state, _ = local
    monkeypatch.setattr(settings, 'max_agent_retries', limit)
    llm = Candidates([BAD])
    result = await ExecutorAgent(llm, tool, emit=no_event).run(state)
    assert llm.calls == limit + 1
    assert len(result.retry_attempts['1']) == limit + 1
    assert result.status == 'needs_human'


@pytest.mark.asyncio
async def test_equivalent_test_bodies_from_different_edit_proposals_are_duplicates(local, monkeypatch):
    import app.agents.executor as module
    tool, state, _ = local
    state.plan[0].target_file = 'test_app.py'
    state.diagnosis = {'files': ['app.py']}
    # Both proposals create the same invalid body, but have different SEARCH/REPLACE hashes.
    before = (tool.remote / 'test_app.py').read_text()
    first = edit('    assert divide(6, 2) == 3', '    assert divide(6, 2) == 4')
    second = edit(before, before.replace('== 3', '== 4'))
    from app.tools.test_validity_validator import InvalidGeneratedTestError
    validations = []
    def invalid(content, requirement):
        validations.append(content)
        raise InvalidGeneratedTestError('Fixture validator rejects this candidate')
    monkeypatch.setattr(module, 'validate_test_validity', invalid)
    llm = Candidates([first, second, second])
    result = await ExecutorAgent(llm, tool, emit=no_event).run(state)
    history = result.retry_attempts['1']
    assert history[0].candidate_fingerprint != history[1].candidate_fingerprint
    assert history[0].generated_test_fingerprint == history[1].generated_test_fingerprint
    assert history[1].failure_type == 'DuplicateCandidate'
    assert len(validations) == 1
    assert result.status == 'needs_human'


@pytest.mark.asyncio
async def test_guard_rejects_unbounded_or_fabricated_retry_progress(local):
    tool, state, _ = local
    previous = state.model_copy(deep=True)
    with pytest.raises(ValueError, match='progress'):
        validate_output(previous, state, 'execute')
    result = await ExecutorAgent(Candidates([BAD]), tool, emit=no_event,
                                 checkpoint_retries=True).run(state)
    changed = result.model_copy(deep=True)
    changed.file_changes['app.py'] = 'unverified'
    with pytest.raises(ValueError, match='artifacts'):
        validate_output(previous, changed, 'execute')
    changed = result.model_copy(deep=True)
    changed.retry_attempts['1'].append(changed.retry_attempts['1'][0])
    with pytest.raises(ValueError, match='progress'):
        validate_output(previous, changed, 'execute')


@pytest.mark.asyncio
async def test_history_archive_is_bounded_without_losing_active_step(local):
    tool, state, _ = local
    state.retry_attempts = {f'old-{i}': [] for i in range(120)}
    llm = Candidates([BAD, GOOD])
    result = await ExecutorAgent(llm, tool, test_runner=passing, emit=no_event).run(state)
    assert result.execution_complete
    assert len(result.retry_attempts) == 100
    assert len(result.retry_attempts['1']) == 1
