from pathlib import Path
from types import SimpleNamespace
import json
import subprocess
import uuid

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agents.executor import ExecutorAgent
from app.agents.planning import Step, ApprovalDecision
from app.agents.state import SubtaskState
from app.tools.repo_tool import RepoTool
from app.tools.test_runner import run_tests, TestResult
from app.tools.test_import_validator import TestImportValidationError, validate_test_imports
from app.tools.github_tool import GitHubTool
from app.orchestrator import graph as module


class LocalRepo(RepoTool):
    def __init__(self, remote, root):
        super().__init__(workspace_root=root)
        self.remote = remote
    def _public_remote_url(self, name):
        return self.remote.as_uri()
    def _stored_token(self, name):
        return 'test-token'


@pytest.fixture
def local(tmp_path):
    remote = tmp_path / 'remote'
    remote.mkdir()
    def git(*args):
        return subprocess.run(['git', *args], cwd=remote, check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.com')
    (remote/'app.py').write_text('def divide(a, b):\n    return a / b\n')
    (remote/'test_app.py').write_text('from app import divide\nimport pytest\ndef test_zero():\n    with pytest.raises(ValueError):\n        divide(1, 0)\ndef test_divide():\n    assert divide(6, 2) == 3\n')
    git('add', '.')
    git('commit', '-m', 'fixture')
    tool = LocalRepo(remote, tmp_path/'workspaces')
    state = SubtaskState(ticket_id=str(uuid.uuid4()), subtask_id=str(uuid.uuid4()), subtask_type='bug',
        description='Raise ValueError when divisor is zero', repo='owner/repo', approval_status='approved',
        base_commit=git('rev-parse', 'HEAD'), plan=[Step(step_id='1', intent='Guard b == 0', target_file='app.py')])
    return tool, state, git


@pytest.fixture
def local_no_tests(tmp_path):
    remote = tmp_path / 'remote'
    remote.mkdir()
    def git(*args):
        return subprocess.run(['git', *args], cwd=remote, check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.com')
    (remote/'app.py').write_text('def divide(a, b):\n    return a / b\n')
    git('add', '.')
    git('commit', '-m', 'fixture')
    tool = LocalRepo(remote, tmp_path/'workspaces')
    state = SubtaskState(ticket_id=str(uuid.uuid4()), subtask_id=str(uuid.uuid4()), subtask_type='bug',
        description='Raise ValueError when divisor is zero', repo='owner/repo', approval_status='approved',
        base_commit=git('rev-parse', 'HEAD'), plan=[Step(step_id='1', intent='Guard b == 0', target_file='app.py')])
    return tool, state, git


@pytest.fixture
def local_with_extra_file(tmp_path):
    remote = tmp_path / 'remote'
    remote.mkdir()
    def git(*args):
        return subprocess.run(['git', *args], cwd=remote, check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.com')
    (remote/'app.py').write_text('def divide(a, b):\n    return a / b\n')
    (remote/'legacy.py').write_text('def unused():\n    return 1\n')
    (remote/'test_app.py').write_text('from app import divide\ndef test_divide():\n    assert divide(6, 2) == 3\n')
    git('add', '.')
    git('commit', '-m', 'fixture')
    tool = LocalRepo(remote, tmp_path/'workspaces')
    state = SubtaskState(ticket_id=str(uuid.uuid4()), subtask_id=str(uuid.uuid4()), subtask_type='bug',
        description='Remove dead helper', repo='owner/repo', approval_status='approved',
        base_commit=git('rev-parse', 'HEAD'),
        plan=[Step(step_id='1', intent='Remove unused helper', target_file='legacy.py', action='delete')])
    return tool, state, git


@pytest.fixture
def local_with_style_example(tmp_path):
    remote = tmp_path / 'remote'
    remote.mkdir()
    def git(*args):
        return subprocess.run(['git', *args], cwd=remote, check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.com')
    (remote/'app.py').write_text('def divide(a, b):\n    return a / b\n')
    (remote/'other.py').write_text('def helper():\n    return 42\n')
    (remote/'test_other.py').write_text('from other import helper\ndef test_helper():\n    assert helper() == 42\n')
    git('add', '.')
    git('commit', '-m', 'fixture')
    tool = LocalRepo(remote, tmp_path/'workspaces')
    state = SubtaskState(ticket_id=str(uuid.uuid4()), subtask_id=str(uuid.uuid4()), subtask_type='bug',
        description='Raise ValueError when divisor is zero', repo='owner/repo', approval_status='approved',
        base_commit=git('rev-parse', 'HEAD'), diagnosis={'root_cause': 'no guard', 'files': ['app.py'], 'reasoning': 'x'},
        plan=[Step(step_id='1', intent='Add regression test', target_file='test_app.py', action='create')])
    return tool, state, git


class LLM:
    def __init__(self, invalid=0):
        self.calls, self.invalid, self.prompts = 0, invalid, []
    def get_usage(self, ticket):
        return {'calls': self.calls, 'tokens': self.calls * 10, 'est_cost_usd': 0.0}
    def complete_json(self, system, user, schema, **kwargs):
        self.calls += 1
        self.prompts.append(user)
        search = 'this does not exist at all' if self.calls <= self.invalid else '    return a / b'
        return schema.model_validate({'blocks': [{'search': search,
            'replace': '    if b == 0:\n        raise ValueError("zero denominator")\n    return a / b'}]})


async def no_event(**kwargs):
    return kwargs


@pytest.mark.asyncio
async def test_executor_applies_tests_and_records_artifact(local):
    tool, state, git = local
    result = await ExecutorAgent(LLM(), tool, emit=no_event).run(state)
    assert result.execution_complete, result.failure_reason
    assert result.current_step == 1
    assert result.steps_done[0]['tests']['passed']
    assert 'if b == 0' in result.file_changes['app.py']
    assert result.budget_used.calls == 1
    assert git('show', 'main:app.py') == 'def divide(a, b):\n    return a / b'


@pytest.mark.asyncio
async def test_executor_repairs_and_stops_at_cap(local):
    tool, state, _ = local
    llm = LLM(invalid=1)
    result = await ExecutorAgent(llm, tool, emit=no_event).run(state)
    assert result.execution_complete
    assert llm.calls == 2
    assert 'closest lines' in llm.prompts[1]
    state.current_step, state.steps_done, state.file_changes = 0, [], {}
    state.execution_complete = False
    llm = LLM(invalid=100)
    result = await ExecutorAgent(llm, tool, emit=no_event).run(state)
    assert result.status == 'needs_human'
    assert llm.calls == 3
    assert not result.file_changes


@pytest.mark.asyncio
async def test_refuses_stale_or_unapproved_execution(local):
    tool, state, git = local
    state.approval_status = 'pending'
    llm = LLM()
    assert (await ExecutorAgent(llm, tool, emit=no_event).run(state)).status == 'needs_human'
    state.approval_status, state.status = 'approved', 'running'
    git('commit', '--allow-empty', '-m', 'new upstream commit')
    assert 'changed since diagnosis' in (await ExecutorAgent(llm, tool, emit=no_event).run(state)).failure_reason
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_failed_tests_never_complete_or_publish(local):
    tool, state, _ = local
    def failed(_):
        return TestResult(passed=False, returncode=1, output='regression failed')
    result = await ExecutorAgent(LLM(), tool, test_runner=failed, emit=no_event).run(state)
    assert result.status == 'needs_human'
    assert not result.steps_done and not result.file_changes


def test_test_runner_no_tests_is_not_success(tmp_path):
    assert not run_tests(tmp_path).passed


def test_test_runner_no_tests_outcome_is_distinct_from_failure(tmp_path):
    result = run_tests(tmp_path)
    assert not result.passed
    assert result.outcome == 'no_tests_collected'
    assert result.returncode == 5


@pytest.mark.asyncio
async def test_edit_step_for_missing_file_escalates_without_llm(local):
    tool, state, _ = local
    state.plan = [Step(step_id='1', intent='Fix', target_file='missing.py')]
    llm = LLM()
    result = await ExecutorAgent(llm, tool, emit=no_event).run(state)
    assert result.status == 'needs_human'
    assert 'does not exist in the repo' in result.failure_reason
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_create_step_for_existing_file_escalates_without_llm(local):
    tool, state, _ = local
    state.plan = [Step(step_id='1', intent='Add', target_file='app.py', action='create')]
    llm = LLM()
    result = await ExecutorAgent(llm, tool, emit=no_event).run(state)
    assert result.status == 'needs_human'
    assert 'already exists' in result.failure_reason
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_no_tests_and_none_added_escalates_once_never_loops(local_no_tests):
    tool, state, _ = local_no_tests
    llm = LLM()
    result = await ExecutorAgent(llm, tool, emit=no_event).run(state)
    assert result.status == 'needs_human'
    assert result.verifiability == 'no_tests'
    assert 'Unverifiable' in result.failure_reason
    assert llm.calls == 1  # single-shot escalation, never a retry loop on exit 5
    assert not result.steps_done


@pytest.mark.asyncio
async def test_no_tests_defers_until_plan_adds_a_test_then_verifies(local_no_tests):
    tool, state, _ = local_no_tests
    state.plan = [
        Step(step_id='1', intent='Guard b == 0', target_file='app.py'),
        Step(step_id='2', intent='Add regression test', target_file='test_app.py', action='create'),
    ]

    class TwoStepLLM:
        def __init__(self):
            self.calls = 0
        def get_usage(self, ticket_id):
            return {'calls': self.calls, 'tokens': self.calls * 10, 'est_cost_usd': 0.0}
        def complete_json(self, system, user, schema, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return schema.model_validate({'blocks': [{'search': '    return a / b',
                    'replace': '    if b == 0:\n        raise ValueError("zero denominator")\n    return a / b'}]})
            return schema.model_validate({'blocks': [{'search': '',
                'replace': 'from app import divide\nimport pytest\ndef test_zero():\n'
                           '    with pytest.raises(ValueError):\n        divide(1, 0)\n'}]})

    llm = TwoStepLLM()
    agent = ExecutorAgent(llm, tool, emit=no_event)
    after_step_1 = await agent.run(state)
    assert after_step_1.status == 'running', after_step_1.failure_reason
    assert after_step_1.current_step == 1
    assert after_step_1.steps_done[0]['tests']['outcome'] == 'no_tests_collected'
    assert after_step_1.verifiability is None

    after_step_2 = await agent.run(after_step_1)
    assert after_step_2.execution_complete, after_step_2.failure_reason
    assert after_step_2.verifiability == 'verified'
    assert after_step_2.steps_done[1]['tests']['outcome'] == 'passed'


@pytest.mark.asyncio
async def test_test_step_is_grounded_in_real_source_and_existing_style(local_with_style_example):
    tool, state, _ = local_with_style_example

    class CapturingLLM:
        def __init__(self):
            self.calls, self.last_user = 0, None
        def get_usage(self, ticket_id):
            return {'calls': self.calls, 'tokens': self.calls * 10, 'est_cost_usd': 0.0}
        def complete_json(self, system, user, schema, **kwargs):
            self.calls += 1
            self.last_user = user
            # app.py isn't fixed in this fixture (only grounding data is under
            # test here), so assert behavior that already holds.
            return schema.model_validate({'blocks': [{'search': '',
                'replace': 'from app import divide\ndef test_ok():\n    assert divide(4, 2) == 2\n'}]})

    llm = CapturingLLM()
    result = await ExecutorAgent(llm, tool, emit=no_event).run(state)
    assert result.execution_complete, result.failure_reason
    payload = json.loads(llm.last_user)
    assert 'def divide' in payload['related_files'].get('app.py', '')  # real source, not just diagnosis prose
    assert payload['style_example']['path'] == 'test_other.py'
    assert 'def test_helper' in payload['style_example']['content']  # an existing test's real convention


def test_import_validator_accepts_real_direct_and_qualified_imports(tmp_path):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src' / 'maths.py').write_text('def divide(a, b):\n    return a / b\n')
    (tmp_path / 'test_direct.py').write_text('from maths import divide\ndef test_it():\n    assert divide(4, 2) == 2\n')
    (tmp_path / 'test_qualified.py').write_text('import maths as subject\ndef test_it():\n    assert subject.divide(4, 2) == 2\n')
    validate_test_imports(tmp_path, 'test_direct.py', ['src/maths.py'])
    validate_test_imports(tmp_path, 'test_qualified.py', ['src/maths.py'])


@pytest.mark.parametrize('content,problem', [
    ('def test_it():\n    assert divide(4, 2) == 2\n', "'divide' is referenced but not imported"),
    ('from wrong import divide\ndef test_it():\n    assert divide(4, 2) == 2\n', "'divide' is imported from 'wrong'"),
    ('from wrong import divide as operation\ndef test_it():\n    assert operation(4, 2) == 2\n', "'divide' is imported from 'wrong'"),
    ('import wrong as subject\ndef test_it():\n    assert subject.divide(4, 2) == 2\n', "referenced through module 'wrong'"),
    ('from app import missing\ndef test_it():\n    assert missing()\n', "'missing' is not defined"),
    ('from app import divide\ndef test_it(:\n    pass\n', 'not valid Python'),
])
def test_import_validator_rejects_invalid_generated_tests(tmp_path, content, problem):
    (tmp_path / 'app.py').write_text('def divide(a, b):\n    return a / b\n')
    (tmp_path / 'test_app.py').write_text(content)
    with pytest.raises(TestImportValidationError, match=problem):
        validate_test_imports(tmp_path, 'test_app.py', ['app.py'])


@pytest.mark.asyncio
async def test_executor_repairs_invalid_test_import_before_pytest(local_with_style_example):
    tool, state, _ = local_with_style_example

    class RepairingTestLLM:
        def __init__(self):
            self.calls, self.prompts = 0, []
        def get_usage(self, ticket_id):
            return {'calls': self.calls, 'tokens': self.calls * 10, 'est_cost_usd': 0.0}
        def complete_json(self, system, user, schema, **kwargs):
            self.calls += 1
            self.prompts.append(user)
            content = ('def test_ok():\n    assert divide(4, 2) == 2\n' if self.calls == 1 else
                       'from app import divide\ndef test_ok():\n    assert divide(4, 2) == 2\n')
            return schema.model_validate({'blocks': [{'search': '', 'replace': content}]})

    pytest_calls = []
    def passing(checkout):
        pytest_calls.append(checkout)
        return TestResult(passed=True, returncode=0, output='1 passed')

    llm = RepairingTestLLM()
    result = await ExecutorAgent(llm, tool, test_runner=passing, emit=no_event).run(state)
    assert result.execution_complete, result.failure_reason
    assert llm.calls == 2
    assert len(pytest_calls) == 1
    assert 'not imported from app.py' in json.loads(llm.prompts[1])['repair_feedback']


@pytest.mark.asyncio
async def test_import_error_gets_a_targeted_repair_hint(local):
    tool, state, _ = local

    def broken_then_fixed(_checkout):
        broken_then_fixed.calls += 1
        if broken_then_fixed.calls == 1:
            return TestResult(passed=False, returncode=1,
                output='NameError: name \'divide\' is not defined')
        return TestResult(passed=True, returncode=0, output='2 passed')
    broken_then_fixed.calls = 0

    llm = LLM()
    result = await ExecutorAgent(llm, tool, test_runner=broken_then_fixed, emit=no_event).run(state)
    assert result.execution_complete, result.failure_reason
    assert llm.calls == 2
    assert 'missing/incorrect import' in llm.prompts[1]  # repair_feedback carried the targeted hint


@pytest.mark.asyncio
async def test_delete_step_removes_file_deterministically_no_llm(local_with_extra_file):
    tool, state, _ = local_with_extra_file
    llm = LLM()
    result = await ExecutorAgent(llm, tool, emit=no_event).run(state)
    assert result.execution_complete, result.failure_reason
    assert llm.calls == 0
    assert result.file_changes['legacy.py'] is None
    assert result.steps_done[0]['tests']['outcome'] == 'passed'
    checkout = tool.prepare_execution(state.repo, state.subtask_id, state.base_commit, result.file_changes)
    assert not (checkout / 'legacy.py').exists()
    assert (checkout / 'app.py').exists()


def test_symlinks_and_metadata_are_rejected(local):
    tool, state, _ = local
    checkout = tool.prepare_execution(state.repo, state.subtask_id, state.base_commit, {})
    (checkout/'link.py').symlink_to(checkout/'app.py')
    for path in ['../x', '.git/config', '.env', 'link.py']:
        with pytest.raises(ValueError):
            tool.write_execution_file(checkout, path, 'x')


class FakeAPIRepo:
    owner = SimpleNamespace(login='owner')
    default_branch = 'main'
    def __init__(self):
        self.prs = []
    def get_pulls(self, **kwargs):
        return self.prs
    def create_pull(self, **kwargs):
        pr = SimpleNamespace(id=123, number=1, html_url='https://github.com/owner/repo/pull/1', state='open', merged=False)
        self.prs.append(pr)
        return pr


@pytest.mark.asyncio
async def test_full_graph_real_git_and_pytest_idempotent_pr(local, monkeypatch):
    tool, state, git = local
    api = FakeAPIRepo()
    github = GitHubTool(repo_tool=tool)
    monkeypatch.setattr(github, 'get_repo', lambda name: api)
    monkeypatch.setattr(module, 'log_event', no_event)
    class Diagnosis:
        def run(self, state):
            state.diagnosis = {'root_cause': 'No zero guard', 'files': ['app.py'], 'reasoning': 'Direct division'}
            return state
    class Planner:
        def run(self, state):
            return state
    state.approval_status = 'pending'
    saver = MemorySaver()
    graph = module.build_graph(agent=Diagnosis(), planner=Planner(), executor=ExecutorAgent(LLM(), tool, emit=no_event),
                               publisher=github, checkpointer=saver, activity_check=lambda state: None)
    config = module.thread_config(state.ticket_id, state.subtask_id)
    await graph.ainvoke(state.model_dump(), config)
    assert (await graph.aget_state(config)).next == ('human_gate',)
    answer = ApprovalDecision(approval_status='approved')
    result = await module.resume_approval(graph, state.ticket_id, state.subtask_id, answer)
    assert result.pr_url == 'https://github.com/owner/repo/pull/1', result.failure_reason
    assert result.status == 'in_review'
    assert len(api.prs) == 1
    branch = tool.branch_name(state.subtask_id)
    assert 'if b == 0' in git('show', f'{branch}:app.py')
    assert 'if b == 0' not in git('show', 'main:app.py')
    await module.resume_approval(graph, state.ticket_id, state.subtask_id, answer)
    github.publish_changes(result)
    assert len(api.prs) == 1


def test_runner_timeout_is_failure(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'test_timeout_seconds', 1)
    (tmp_path/'test_slow.py').write_text('import time\ndef test_slow():\n    time.sleep(60)\n')
    result = run_tests(tmp_path)
    assert result.timed_out and not result.passed


def test_push_recovery_reuses_matching_branch_and_refuses_overwrite(local):
    tool, state, git = local
    changes = {'app.py': 'def divide(a, b):\n    return 0 if b == 0 else a / b\n'}
    branch = tool.push_changes(state.repo, state.subtask_id, state.base_commit, changes)
    head = git('rev-parse', branch)
    assert tool.push_changes(state.repo, state.subtask_id, state.base_commit, changes) == branch
    assert git('rev-parse', branch) == head
    with pytest.raises(ValueError, match='differs'):
        tool.push_changes(state.repo, state.subtask_id, state.base_commit, {'app.py': 'different code\n'})
