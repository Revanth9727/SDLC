"""Planner: ticket -> isolated sub-tasks with repo assignment (architecture.md
§7a, ai_rules.md R-10/R-26/R-30). Unit tests hit PlannerAgent directly; graph
tests prove the intent-confirmation gate actually pauses and resumes."""
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agents.planner import PlannerAgent, DecompositionResult, SubtaskSpec
from app.agents.planning import ApprovalDecision
from app.agents.state import SubtaskState
from app.db.connection import SessionLocal
from app.db.models import Ticket, Subtask
from app.orchestrator import graph as module


class FakeLLM:
    def __init__(self, result: dict):
        self.result = result
        self.calls: list[tuple] = []

    def complete_json(self, system, user, schema, **kwargs):
        self.calls.append((system, user, kwargs))
        return schema.model_validate(self.result)

    def get_usage(self, ticket_id):
        return {'calls': len(self.calls), 'tokens': len(self.calls) * 50, 'est_cost_usd': 0.001 * len(self.calls)}


def _state(**overrides):
    base = dict(ticket_id=str(uuid.uuid4()), subtask_id=str(uuid.uuid4()), subtask_type='bug',
               description='Fix the divide-by-zero crash in app.py', repo='owner/placeholder',
               confirmed_repos=['owner/repo'])
    base.update(overrides)
    return SubtaskState(**base)


# --- PlannerAgent unit tests -------------------------------------------------

def test_single_confirmed_repo_is_force_assigned_even_if_model_says_otherwise():
    # The model hallucinating a different repo must never win when there was
    # nothing to decide (R-26) — the code overrides it deterministically.
    llm = FakeLLM({'subtasks': [{'spec_id': '1', 'type': 'bug', 'description': 'Guard b == 0',
                                 'repo': 'someone/else', 'depends_on': []}], 'reasoning': 'One fix.'})
    result = PlannerAgent(llm).run(_state())
    assert result.repo == 'owner/repo'
    assert result.subtask_specs[0]['repo'] == 'owner/repo'
    assert result.status == 'running'


def test_first_subtask_overwrites_description_repo_and_type():
    llm = FakeLLM({'subtasks': [
        {'spec_id': '1', 'type': 'bug', 'description': 'Guard the zero-division branch', 'repo': 'owner/repo', 'depends_on': []},
        {'spec_id': '2', 'type': 'feature', 'description': 'Add a subtract() helper', 'repo': 'owner/repo', 'depends_on': []},
    ], 'reasoning': 'Two independent asks in one ticket.'})
    result = PlannerAgent(llm).run(_state())
    assert result.subtask_type == 'bug'
    assert result.description == 'Guard the zero-division branch'
    assert len(result.subtask_specs) == 2  # full decomposition recorded even though only the first runs
    assert result.decomposition_reasoning == 'Two independent asks in one ticket.'


def test_multi_repo_ticket_grounds_repo_assignment_to_the_confirmed_list():
    llm = FakeLLM({'subtasks': [{'spec_id': '1', 'type': 'bug', 'description': 'Fix frontend bug',
                                 'repo': 'org/frontend', 'depends_on': []}], 'reasoning': 'Frontend-only fix.'})
    result = PlannerAgent(llm).run(_state(confirmed_repos=['org/frontend', 'org/backend']))
    assert result.repo == 'org/frontend'
    assert result.status == 'running'


def test_multi_repo_ticket_rejects_a_repo_outside_the_confirmed_list():
    # Never trust the model's self-report about which repos exist (R-46 style
    # grounding) — a hallucinated repo becomes CannotDecompose, not a guess.
    llm = FakeLLM({'subtasks': [{'spec_id': '1', 'type': 'bug', 'description': 'Fix it',
                                 'repo': 'org/nonexistent', 'depends_on': []}], 'reasoning': 'x'})
    result = PlannerAgent(llm).run(_state(confirmed_repos=['org/frontend', 'org/backend']))
    assert result.status == 'needs_human'
    assert 'not in the confirmed list' in result.failure_reason
    assert 'org/nonexistent' in result.failure_reason


def test_ambiguous_repo_returns_cannot_decompose_instead_of_guessing():
    llm = FakeLLM({'CannotDecompose': {'reason': 'Unclear whether this belongs to frontend or backend'},
                   'reasoning': 'Ambiguous.'})
    result = PlannerAgent(llm).run(_state(confirmed_repos=['org/frontend', 'org/backend']))
    assert result.status == 'needs_human'
    assert result.failure_reason == ('CannotDecompose: Unclear whether this belongs to frontend or backend')


def test_no_confirmed_repos_short_circuits_without_an_llm_call():
    llm = FakeLLM({'subtasks': [], 'reasoning': 'unused'})
    result = PlannerAgent(llm).run(_state(confirmed_repos=[]))
    assert result.status == 'needs_human'
    assert 'No confirmed repo' in result.failure_reason
    assert llm.calls == []


def test_budget_is_tracked_from_the_llm_client():
    llm = FakeLLM({'subtasks': [{'spec_id': '1', 'type': 'bug', 'description': 'Fix it', 'repo': 'owner/repo',
                                 'depends_on': []}], 'reasoning': 'x'})
    result = PlannerAgent(llm).run(_state())
    assert result.budget_used.calls == 1


def test_decomposition_result_requires_unique_spec_ids():
    with pytest.raises(Exception):
        DecompositionResult.model_validate({'subtasks': [
            {'spec_id': '1', 'type': 'bug', 'description': 'a', 'repo': 'owner/repo', 'depends_on': []},
            {'spec_id': '1', 'type': 'bug', 'description': 'b', 'repo': 'owner/repo', 'depends_on': []},
        ], 'reasoning': 'x'})


def test_subtask_spec_rejects_unknown_type():
    with pytest.raises(Exception):
        SubtaskSpec.model_validate({'spec_id': '1', 'type': 'nonsense', 'description': 'a', 'repo': 'owner/repo'})


# --- Graph integration: the intent-confirmation gate -------------------------

@pytest.fixture
def gstate():
    tid, sid = uuid.uuid4(), uuid.uuid4()
    state = SubtaskState(ticket_id=str(tid), subtask_id=str(sid), subtask_type='bug',
                         jira_key='TEST-72', repo='owner/repo', confirmed_repos=['owner/repo'],
                         description='Fix the divide-by-zero crash')
    with SessionLocal() as db:
        db.add(Ticket(id=tid, source='jira', title='Planner test', description='Test', status='processing'))
        db.flush()
        db.add(Subtask(id=sid, ticket_id=tid, type='bug', description='Test', status='running', state=state.model_dump()))
        db.commit()
    yield state
    with SessionLocal() as db:
        db.delete(db.get(Ticket, tid))
        db.commit()


def _decomposer(specs):
    def run(state):
        state.subtask_specs = specs
        state.decomposition_reasoning = 'Because the ticket says so.'
        return state
    return SimpleNamespace(run=Mock(side_effect=run))


def _diagnosis_never_reached():
    return SimpleNamespace(run=Mock(side_effect=AssertionError('diagnosis must not run before intent is confirmed')))


@pytest.mark.asyncio
async def test_decomposition_pauses_for_confirmation_before_diagnosis(gstate):
    jira = SimpleNamespace(set_status=Mock(return_value={'applied': True}), comment=Mock())
    specs = [{'spec_id': '1', 'type': 'bug', 'description': 'Guard b == 0', 'repo': 'owner/repo', 'depends_on': []}]
    g = module.build_graph(decomposer=_decomposer(specs), agent=_diagnosis_never_reached(),
                           jira=jira, checkpointer=MemorySaver(), activity_check=lambda _: None)
    config = module.thread_config(gstate.ticket_id, gstate.subtask_id)
    await g.ainvoke(gstate.model_dump(), config)
    snapshot = await g.aget_state(config)
    assert snapshot.next == ('intent_gate',)
    assert snapshot.values['approval_payload']['subtasks'] == specs
    assert any('Correct?' in str(c) for c in jira.comment.call_args_list)
    jira.set_status.assert_called_with('TEST-72', 'awaiting_approval')


@pytest.mark.asyncio
async def test_confirming_intent_proceeds_to_diagnosis(gstate):
    jira = SimpleNamespace(set_status=Mock(return_value={'applied': True}), comment=Mock())
    specs = [{'spec_id': '1', 'type': 'bug', 'description': 'Guard b == 0', 'repo': 'owner/repo', 'depends_on': []}]
    diagnosis = SimpleNamespace(run=Mock(side_effect=lambda s: (
        setattr(s, 'diagnosis', {'root_cause': 'x', 'files': [], 'reasoning': 'x'}), s)[1]))
    g = module.build_graph(decomposer=_decomposer(specs), agent=diagnosis, jira=jira,
                           memory_search=lambda *a, **k: [], checkpointer=MemorySaver(), activity_check=lambda _: None)
    config = module.thread_config(gstate.ticket_id, gstate.subtask_id)
    await g.ainvoke(gstate.model_dump(), config)
    await module.resume_approval(g, gstate.ticket_id, gstate.subtask_id, ApprovalDecision(approval_status='approved'))
    diagnosis.run.assert_called_once()
    assert jira.set_status.call_args_list[-2].args[1] == 'in_progress'  # confirmed, then step_planner's own pause


@pytest.mark.asyncio
async def test_rejecting_intent_escalates_with_the_note(gstate):
    jira = SimpleNamespace(set_status=Mock(return_value={'applied': True}), comment=Mock())
    specs = [{'spec_id': '1', 'type': 'bug', 'description': 'Guard b == 0', 'repo': 'owner/repo', 'depends_on': []}]
    g = module.build_graph(decomposer=_decomposer(specs), agent=_diagnosis_never_reached(), jira=jira,
                           checkpointer=MemorySaver(), activity_check=lambda _: None)
    config = module.thread_config(gstate.ticket_id, gstate.subtask_id)
    await g.ainvoke(gstate.model_dump(), config)
    result = await module.resume_approval(g, gstate.ticket_id, gstate.subtask_id,
                                          ApprovalDecision(approval_status='rejected', note='Wrong split'))
    assert result.status == 'needs_human'
    assert 'Decomposition rejected: Wrong split' in result.failure_reason
    jira.set_status.assert_called_with('TEST-72', 'blocked')


@pytest.mark.asyncio
async def test_cannot_decompose_escalates_without_reaching_the_gate(gstate):
    def run(state):
        state.status, state.failure_reason = 'needs_human', 'CannotDecompose: too vague'
        return state
    g = module.build_graph(decomposer=SimpleNamespace(run=Mock(side_effect=run)),
                           agent=_diagnosis_never_reached(), checkpointer=MemorySaver(),
                           jira=SimpleNamespace(set_status=Mock(return_value={'applied': True}), comment=Mock()),
                           activity_check=lambda _: None)
    config = module.thread_config(gstate.ticket_id, gstate.subtask_id)
    result = await g.ainvoke(gstate.model_dump(), config)
    assert result['status'] == 'needs_human'
    assert 'CannotDecompose' in result['failure_reason']
    assert (await g.aget_state(config)).next == ('human_resolution',)
