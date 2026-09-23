"""End-to-end integration test (codex_prompts.md Phase 7.3): one ticket runs
through the WHOLE graph — Planner, Diagnosis, Step-Planner, Executor, Critic,
PR — with a stubbed LLM client and mocked GitHub API, so it is deterministic
and free (ai_rules.md R-22): no network call ever happens. Everything else —
every real agent's own logic, the surgical-edit cascade, real pytest, real
git — is the actual code, run against a real local git repo standing in for
GitHub.
"""
import subprocess
import uuid
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agents.critic import CriticAgent
from app.agents.diagnosis import DiagnosisAgent
from app.agents.executor import ExecutorAgent
from app.agents.planner import PlannerAgent
from app.agents.planning import ApprovalDecision
from app.agents.state import SubtaskState
from app.agents.step_planner import StepPlannerAgent
from app.db.connection import SessionLocal, engine
from app.db.models import Subtask, Ticket, TicketBudget
from app.orchestrator import graph as module
from app.tools.github_tool import GitHubTool
from app.tools.repo_tool import RepoTool


class LocalRepo(RepoTool):
    """A real git repo on disk stands in for GitHub — no network involved."""
    def __init__(self, remote, root):
        super().__init__(workspace_root=root)
        self.remote = remote
    def _public_remote_url(self, name):
        return self.remote.as_uri()
    def _stored_token(self, name):
        return 'test-token'


class FakeAPIRepo:
    """Stands in for PyGithub's Repository object — this IS "mock GitHub"."""
    owner = SimpleNamespace(login='owner')
    default_branch = 'main'
    def __init__(self, pr_id):
        self.pr_id = pr_id
        self.prs = []
    def get_pulls(self, **kwargs):
        return self.prs
    def create_pull(self, **kwargs):
        pr = SimpleNamespace(id=self.pr_id, number=1, html_url='https://github.com/owner/repo/pull/1',
                             state='open', merged=False)
        self.prs.append(pr)
        return pr


class FakeLLM:
    """Canned, schema-keyed responses (deterministic and free, R-22) — but
    every REAL agent's own prompt-building, validation, and retry logic still
    runs; only the network call to OpenAI is replaced."""
    def __init__(self):
        self.calls: list[str] = []

    def complete_json(self, system, user, schema, **kwargs):
        import json
        self.calls.append(schema.__name__)
        name = schema.__name__
        if name == 'DecompositionResult':
            return schema.model_validate({'subtasks': [{
                'spec_id': '1', 'type': 'bug',
                'description': 'Guard divide() against a zero denominator',
                'repo': 'owner/repo', 'depends_on': [],
            }], 'reasoning': 'One clear bug fix.'})
        if name == 'Diagnosis':
            return schema.model_validate({
                'root_cause': 'divide() does not guard b == 0',
                'files': ['app.py'], 'reasoning': 'Direct division with no check.',
            })
        if name == 'PlanningResult':
            return schema.model_validate({'plan': [
                {'step_id': '1', 'intent': 'Guard the zero denominator', 'target_file': 'app.py', 'action': 'edit'},
                {'step_id': '2', 'intent': 'Add a regression test', 'target_file': 'test_app.py', 'action': 'create'},
            ], 'reasoning': 'Guard the bug, then prove it with a test.'})
        if name == 'EditProposal':
            step = json.loads(user)['step']
            if step['target_file'] == 'app.py':
                return schema.model_validate({'blocks': [{
                    'search': '    return a / b',
                    'replace': '    if b == 0:\n        raise ValueError("zero denominator")\n    return a / b',
                }]})
            return schema.model_validate({'full_content': (
                'from app import divide\n'
                'import pytest\n\n\n'
                'def test_zero_raises():\n'
                '    with pytest.raises(ValueError):\n'
                '        divide(1, 0)\n\n\n'
                'def test_normal_division():\n'
                '    assert divide(6, 2) == 3\n'
            )})
        if name == 'CriticVerdict':
            return schema.model_validate({'approved': True, 'issues': [], 'verifiability': 'ok',
                                          'test_validity': 'valid', 'test_issues': [],
                                          'implementation_valid': True})
        raise AssertionError(f'unexpected schema requested: {name}')

    def get_usage(self, ticket_id):
        return {'calls': len(self.calls), 'tokens': len(self.calls) * 100, 'est_cost_usd': 0.001 * len(self.calls)}


class FakeJira:
    def __init__(self):
        self.statuses: list[str] = []
        self.comments: list[str] = []
    def set_status(self, key, stage):
        self.statuses.append(stage)
        return {'applied': True}
    def comment(self, key, message):
        self.comments.append(message)
        return 'comment-1'


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / 'remote'
    remote.mkdir()
    def git(*args):
        return subprocess.run(['git', *args], cwd=remote, check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.com')
    (remote / 'app.py').write_text('def divide(a, b):\n    return a / b\n')
    git('add', '.')
    git('commit', '-m', 'initial')
    return LocalRepo(remote, tmp_path / 'workspaces'), git


@pytest.mark.asyncio
async def test_full_flow_single_subtask_end_to_end(repo):
    """One ticket -> Planner -> intent gate -> Diagnosis -> Step-Planner ->
    plan gate -> Executor (real surgical edit + real pytest) -> Critic -> PR."""
    TicketBudget.__table__.create(engine, checkfirst=True)
    tool, git = repo
    llm = FakeLLM()
    jira = FakeJira()
    # A random id (not a fixed literal) avoids colliding with any PRLink row
    # left behind by other tests that stub GitHub's PR id the same way.
    pr_id = str(uuid.uuid4())
    api = FakeAPIRepo(pr_id)
    publisher = GitHubTool(repo_tool=tool)
    publisher.get_repo = lambda name=None: api  # the only "mock GitHub" needed

    tid, sid = uuid.uuid4(), uuid.uuid4()
    state = SubtaskState(
        ticket_id=str(tid), subtask_id=str(sid), subtask_type='bug', jira_key='FLOW-1',
        description='App crashes with ZeroDivisionError on divide(x, 0)', repo='owner/repo',
        confirmed_repos=['owner/repo'],
    )
    with SessionLocal() as db:
        db.add(Ticket(id=tid, source='jira', external_key='FLOW-1', title='Divide crash',
                      description=state.description, repos=['owner/repo'], status='processing'))
        db.flush()
        db.add(Subtask(id=sid, ticket_id=tid, type='bug', description=state.description,
                       status='running', state=state.model_dump()))
        db.commit()

    graph = module.build_graph(
        decomposer=PlannerAgent(llm), agent=DiagnosisAgent(llm, tool), planner=StepPlannerAgent(llm, tool),
        executor=ExecutorAgent(llm, tool), critic=CriticAgent(llm), publisher=publisher, jira=jira,
        memory_search=lambda *a, **k: [],  # no prior resolutions to reuse — this test proves the full pipeline
        checkpointer=MemorySaver(), activity_check=lambda _: None,
    )
    config = module.thread_config(state.ticket_id, state.subtask_id)
    approved = ApprovalDecision(approval_status='approved')

    try:
        # 1. The subtask exists, and the run starts by pausing on the
        # Planner's decomposition — before Diagnosis ever touches the repo.
        with SessionLocal() as db:
            assert db.get(Subtask, sid) is not None
        await graph.ainvoke(state.model_dump(), config)
        snapshot = await graph.aget_state(config)
        assert snapshot.next == ('intent_gate',)
        assert snapshot.values['subtask_specs'][0]['repo'] == 'owner/repo'

        # 2. Auto-approve the decomposition -> Diagnosis + Step-Planner run
        # for real (real repo reads, real schema validation) -> plan gate.
        result = await module.resume_approval(graph, state.ticket_id, state.subtask_id, approved)
        assert result.diagnosis['root_cause'] == 'divide() does not guard b == 0'
        assert [step.target_file for step in result.plan] == ['app.py', 'test_app.py']
        assert result.approval_status == 'pending'
        assert (await graph.aget_state(config)).next == ('human_gate',)

        # 3. Auto-approve the plan -> the Executor applies two real surgical
        # edits and runs real pytest -> Critic passes -> a PR is opened.
        result = await module.resume_approval(graph, state.ticket_id, state.subtask_id, approved)
        assert result.execution_complete, result.failure_reason
        assert len(result.steps_done) == 2
        # Step 1 edits app.py before any test exists (deferred, not a failure —
        # R-46); step 2 adds the test, and pytest genuinely passes both.
        assert result.steps_done[0]['tests']['outcome'] == 'no_tests_collected'
        assert result.steps_done[-1]['tests']['outcome'] == 'passed'
        assert result.verifiability == 'verified'
        assert result.critic_verdict == {'approved': True, 'issues': [], 'verifiability': 'ok',
                                         'test_validity': 'valid', 'test_issues': [],
                                         'implementation_valid': True}
        assert result.status == 'in_review'
        assert result.pr_url == 'https://github.com/owner/repo/pull/1'
        assert len(api.prs) == 1

        # The edit actually landed on the sub-task's branch, never on main.
        branch = tool.branch_name(state.subtask_id)
        assert 'if b == 0' in git('show', f'{branch}:app.py')
        assert 'if b == 0' not in git('show', 'main:app.py')
        assert 'test_zero_raises' in git('show', f'{branch}:test_app.py')
        assert llm.calls.count('EditProposal') == 2
        assert set(llm.calls) == {'DecompositionResult', 'Diagnosis', 'PlanningResult', 'EditProposal', 'CriticVerdict'}
    finally:
        with SessionLocal() as db:
            from app.db.models import PRLink
            link = db.get(PRLink, pr_id)
            if link:
                db.delete(link)
            db.delete(db.get(Ticket, tid))
            db.commit()
