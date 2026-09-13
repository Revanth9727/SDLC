"""Solution-reuse memory (memory.md §4/§5/§8a, ai_rules.md R-29). Unit tests
hit app.tools.memory directly with a fake embedding LLM (deterministic, no
network — the vectors are one-hot so cosine similarity is exact); graph tests
prove the reuse gate's own branching: strong+fresh match skips Diagnosis and
Step-Planner, weak/stale match falls through, and rejecting a reused plan
never re-plans off the synthetic "reused" diagnosis.
"""
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agents.planning import ApprovalDecision, Step
from app.agents.state import SubtaskState
from app.db.connection import SessionLocal, engine
from app.db.models import Subtask, SubtaskMemory, Ticket
from app.orchestrator import graph as module
from app.tools import memory as memory_tools


def _vector(index: int, dim: int = 1536) -> list[float]:
    """A one-hot unit vector: identical index -> cosine similarity 1.0;
    different index -> orthogonal, similarity 0.0. Deterministic and free —
    no real embedding call needed to test similarity math."""
    v = [0.0] * dim
    v[index] = 1.0
    return v


class FakeEmbedLLM:
    def __init__(self, vector):
        self.vector = vector
    def embed(self, text):
        return self.vector


@pytest.fixture(autouse=True)
def memory_table():
    SubtaskMemory.__table__.create(engine, checkfirst=True)
    yield


@pytest.fixture
def resolved():
    """One resolved sub-task's memory row, inserted directly so its embedding
    is exactly controlled (bypassing write_back's own summarisation call)."""
    tid, sid = uuid.uuid4(), uuid.uuid4()
    with SessionLocal() as db:
        db.add(Ticket(id=tid, source='jira', external_key='MEM-1', title='Divide crash',
                      description='divide by zero', status='done'))
        db.flush()
        db.add(Subtask(id=sid, ticket_id=tid, type='bug', description='divide by zero', status='done'))
        db.add(SubtaskMemory(subtask_id=sid, ticket_id=tid, subtask_type='bug',
            problem_summary='App crashes dividing by zero', resolution_summary='Add a zero-check guard in divide()',
            files_touched=['app.py'], embedding=_vector(0)))
        db.commit()
    yield tid, sid
    with SessionLocal() as db:
        db.delete(db.get(Ticket, tid))
        db.commit()


# --- embed / search_similar --------------------------------------------------

def test_embed_delegates_to_the_llm_client():
    llm = FakeEmbedLLM(_vector(1))
    assert memory_tools.embed('some text', llm) == _vector(1)


def test_search_similar_returns_strong_match_above_threshold(resolved):
    tid, sid = resolved
    llm = FakeEmbedLLM(_vector(0))  # identical to the stored embedding -> similarity 1.0
    matches = memory_tools.search_similar('divide by zero crash', threshold=0.9, llm=llm)
    assert len(matches) == 1
    assert matches[0]['ticket_id'] == str(tid)
    assert matches[0]['subtask_id'] == str(sid)
    assert matches[0]['similarity'] == pytest.approx(1.0)
    assert matches[0]['files_touched'] == ['app.py']
    assert matches[0]['resolution_summary'] == 'Add a zero-check guard in divide()'


def test_search_similar_filters_out_weak_matches(resolved):
    llm = FakeEmbedLLM(_vector(1))  # orthogonal to the stored embedding -> similarity 0.0
    assert memory_tools.search_similar('unrelated problem', threshold=0.75, llm=llm) == []


def test_search_similar_scopes_by_subtask_type_when_given(resolved):
    llm = FakeEmbedLLM(_vector(0))
    assert memory_tools.search_similar('x', subtask_type='feature', threshold=0.9, llm=llm) == []
    assert len(memory_tools.search_similar('x', subtask_type='bug', threshold=0.9, llm=llm)) == 1


# --- write_back ---------------------------------------------------------------

def _ticket_and_subtask():
    tid, sid = uuid.uuid4(), uuid.uuid4()
    with SessionLocal() as db:
        db.add(Ticket(id=tid, source='jira', external_key=f'MEM-{tid.hex[:6]}', title='x', description='x', status='done'))
        db.flush()
        db.add(Subtask(id=sid, ticket_id=tid, type='bug', description='x', status='done'))
        db.commit()
    return tid, sid


def test_write_back_summarises_embeds_and_inserts():
    tid, sid = _ticket_and_subtask()
    state = SubtaskState(ticket_id=str(tid), subtask_id=str(sid), subtask_type='bug',
        description='App crashes on divide by zero', repo='owner/repo',
        diagnosis={'root_cause': 'no guard', 'files': ['app.py'], 'reasoning': 'x'},
        file_changes={'app.py': 'guarded'}, pr_url='https://github.com/owner/repo/pull/1')
    llm = SimpleNamespace(
        complete_json=Mock(return_value=memory_tools.ResolutionSummary(
            problem_summary='Division crashes without a zero check',
            resolution_summary='Added a zero-denominator guard')),
        embed=Mock(return_value=_vector(2)),
    )
    try:
        memory_tools.write_back(state, llm)
        with SessionLocal() as db:
            row = db.query(SubtaskMemory).filter(SubtaskMemory.subtask_id == sid).one()
            assert row.problem_summary == 'Division crashes without a zero check'
            assert row.resolution_summary == 'Added a zero-denominator guard'
            assert row.files_touched == ['app.py']
            assert row.subtask_type == 'bug'
    finally:
        with SessionLocal() as db:
            db.delete(db.get(Ticket, tid))
            db.commit()


def test_write_back_upserts_on_repeat_call_not_duplicate():
    tid, sid = _ticket_and_subtask()
    state = SubtaskState(ticket_id=str(tid), subtask_id=str(sid), subtask_type='bug',
        description='x', repo='owner/repo', file_changes={'app.py': 'v1'})
    llm = SimpleNamespace(
        complete_json=Mock(return_value=memory_tools.ResolutionSummary(problem_summary='p1', resolution_summary='r1')),
        embed=Mock(return_value=_vector(3)),
    )
    try:
        memory_tools.write_back(state, llm)
        llm.complete_json.return_value = memory_tools.ResolutionSummary(problem_summary='p2', resolution_summary='r2')
        memory_tools.write_back(state, llm)
        with SessionLocal() as db:
            rows = db.query(SubtaskMemory).filter(SubtaskMemory.subtask_id == sid).all()
            assert len(rows) == 1
            assert rows[0].resolution_summary == 'r2'
    finally:
        with SessionLocal() as db:
            db.delete(db.get(Ticket, tid))
            db.commit()


def test_write_back_failure_never_raises():
    state = SubtaskState(ticket_id=str(uuid.uuid4()), subtask_id=str(uuid.uuid4()), subtask_type='bug',
        description='x', repo='owner/repo')
    llm = SimpleNamespace(complete_json=Mock(side_effect=RuntimeError('LLM down')), embed=Mock())
    memory_tools.write_back(state, llm)  # must not raise (M-4)


# --- Graph integration: the reuse gate ---------------------------------------

@pytest.fixture
def gstate():
    tid, sid = uuid.uuid4(), uuid.uuid4()
    state = SubtaskState(ticket_id=str(tid), subtask_id=str(sid), subtask_type='bug', jira_key='TEST-75',
                         repo='owner/repo', confirmed_repos=['owner/repo'], description='Guard division by zero')
    with SessionLocal() as db:
        db.add(Ticket(id=tid, source='jira', title='Reuse test', description='Test', status='processing'))
        db.flush()
        db.add(Subtask(id=sid, ticket_id=tid, type='bug', description='Guard division by zero',
                       status='running', state=state.model_dump()))
        db.commit()
    yield state
    with SessionLocal() as db:
        db.delete(db.get(Ticket, tid))
        db.commit()


def _decomposer():
    def run(state):
        state.subtask_specs = [{'spec_id': '1', 'type': 'bug', 'description': state.description,
                                'repo': state.repo, 'depends_on': []}]
        state.decomposition_reasoning = 'One clear bug fix.'
        return state
    return SimpleNamespace(run=Mock(side_effect=run))


def _repo_stub(monkeypatch, files):
    monkeypatch.setattr(module, 'RepoTool', lambda *a, **k: SimpleNamespace(
        list_files=lambda repo, sid: files, cleanup_workspace=lambda sid: None,
        revision=lambda repo, sid: 'a' * 40,
        file_fingerprints=lambda repo, sid, paths: {path: 'fingerprint' for path in paths},
        verify_freshness=lambda repo, sid, expected: ('a' * 40, [])))


def _jira():
    return SimpleNamespace(set_status=Mock(return_value={'applied': True}), comment=Mock())


async def _confirm_intent(g, state):
    config = module.thread_config(state.ticket_id, state.subtask_id)
    await g.ainvoke(state.model_dump(), config)
    return await module.resume_approval(g, state.ticket_id, state.subtask_id,
                                        ApprovalDecision(approval_status='approved'))


@pytest.mark.asyncio
async def test_strong_fresh_match_skips_diagnosis_and_step_planner(gstate, monkeypatch):
    _repo_stub(monkeypatch, ['app.py'])
    diagnosis = SimpleNamespace(run=Mock(side_effect=AssertionError('Diagnosis must be skipped on strong reuse')))
    planner = SimpleNamespace(run=Mock(side_effect=AssertionError('Step-Planner must be skipped on strong reuse')))
    match = [{'ticket_id': 'SCRUM-1', 'subtask_id': str(uuid.uuid4()), 'problem_summary': 'x',
             'resolution_summary': 'Add a zero-check guard in divide()', 'files_touched': ['app.py'], 'similarity': 0.93}]
    g = module.build_graph(decomposer=_decomposer(), agent=diagnosis, planner=planner,
                           memory_search=lambda *a, **k: match, jira=_jira(),
                           checkpointer=MemorySaver(), activity_check=lambda _: None)
    result = await _confirm_intent(g, gstate)
    assert not diagnosis.run.called
    assert not planner.run.called
    assert result.reuse_source == 'SCRUM-1'
    assert result.reuse_similarity == 0.93
    assert result.plan[0].target_file == 'app.py' and result.plan[0].action == 'edit'
    assert 'Reused from ticket SCRUM-1' in result.plan_reasoning
    assert result.approval_status == 'pending'  # paused at the (reused) plan gate — never blind-applied
    assert result.diagnosis['root_cause'].startswith('Reused from ticket SCRUM-1')


@pytest.mark.asyncio
async def test_weak_match_falls_through_to_full_pipeline(gstate, monkeypatch):
    _repo_stub(monkeypatch, ['app.py'])
    diagnosis = SimpleNamespace(run=Mock(side_effect=lambda s: (
        setattr(s, 'diagnosis', {'root_cause': 'real diagnosis', 'files': [], 'reasoning': 'x'}), s)[1]))
    g = module.build_graph(decomposer=_decomposer(), agent=diagnosis, memory_search=lambda *a, **k: [],
                           jira=_jira(), checkpointer=MemorySaver(), activity_check=lambda _: None)
    result = await _confirm_intent(g, gstate)
    assert diagnosis.run.called
    assert result.reuse_source is None
    assert result.diagnosis['root_cause'] == 'real diagnosis'


@pytest.mark.asyncio
async def test_stale_match_with_missing_files_falls_through(gstate, monkeypatch):
    _repo_stub(monkeypatch, ['other.py'])  # app.py (the match's touched file) no longer exists
    diagnosis = SimpleNamespace(run=Mock(side_effect=lambda s: (
        setattr(s, 'diagnosis', {'root_cause': 'real diagnosis', 'files': [], 'reasoning': 'x'}), s)[1]))
    match = [{'ticket_id': 'SCRUM-1', 'subtask_id': str(uuid.uuid4()), 'problem_summary': 'x',
             'resolution_summary': 'x', 'files_touched': ['app.py'], 'similarity': 0.95}]
    g = module.build_graph(decomposer=_decomposer(), agent=diagnosis, memory_search=lambda *a, **k: match,
                           jira=_jira(), checkpointer=MemorySaver(), activity_check=lambda _: None)
    result = await _confirm_intent(g, gstate)
    assert diagnosis.run.called
    assert result.reuse_source is None


@pytest.mark.asyncio
async def test_rejecting_a_reused_plan_falls_through_to_real_diagnosis(gstate, monkeypatch):
    _repo_stub(monkeypatch, ['app.py'])
    diagnosis = SimpleNamespace(run=Mock(side_effect=lambda s: (
        setattr(s, 'diagnosis', {'root_cause': 'real diagnosis', 'files': [], 'reasoning': 'x'}), s)[1]))
    planner = SimpleNamespace(run=Mock(side_effect=lambda s: (
        setattr(s, 'plan', [Step(step_id='1', intent='real plan', target_file='app.py')]),
        setattr(s, 'plan_reasoning', 'real reasoning'), s)[2]))
    match = [{'ticket_id': 'SCRUM-1', 'subtask_id': str(uuid.uuid4()), 'problem_summary': 'x',
             'resolution_summary': 'Add a guard', 'files_touched': ['app.py'], 'similarity': 0.95}]
    g = module.build_graph(decomposer=_decomposer(), agent=diagnosis, planner=planner,
                           memory_search=lambda *a, **k: match, jira=_jira(),
                           checkpointer=MemorySaver(), activity_check=lambda _: None)
    config = module.thread_config(gstate.ticket_id, gstate.subtask_id)
    await g.ainvoke(gstate.model_dump(), config)
    await module.resume_approval(g, gstate.ticket_id, gstate.subtask_id, ApprovalDecision(approval_status='approved'))
    assert not diagnosis.run.called  # reused first — diagnosis skipped, as proven above

    result = await module.resume_approval(g, gstate.ticket_id, gstate.subtask_id,
                                          ApprovalDecision(approval_status='rejected', note='Not the same bug'))
    assert diagnosis.run.called  # rejection fell through to a REAL diagnosis
    assert planner.run.called
    assert result.reuse_source is None
    assert result.diagnosis['root_cause'] == 'real diagnosis'
    assert result.plan[0].intent == 'real plan'
