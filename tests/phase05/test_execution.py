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
from app.tools.test_interface_validator import (
    TestInterfaceValidationError, resolve_test_interfaces, validate_test_interfaces,
)
from app.tools.test_validity_validator import InvalidGeneratedTestError, validate_test_validity
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


@pytest.fixture
def local_with_object_interface(tmp_path):
    remote = tmp_path / 'remote'
    remote.mkdir()
    def git(*args):
        return subprocess.run(['git', *args], cwd=remote, check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.com')
    (remote / 'widgets.py').write_text(
        'class Widget:\n'
        '    def __init__(self, name, count):\n'
        '        self.name = name\n'
        '        self.count = count\n\n'
        '    def label(self, prefix):\n'
        '        return f"{prefix}:{self.name}"\n\n'
        'def build_widget(name) -> Widget:\n'
        '    return Widget(name, 1)\n'
    )
    git('add', '.')
    git('commit', '-m', 'fixture')
    tool = LocalRepo(remote, tmp_path / 'workspaces')
    state = SubtaskState(
        ticket_id=str(uuid.uuid4()), subtask_id=str(uuid.uuid4()), subtask_type='bug',
        description='Cover widget construction and returned fields', repo='owner/repo',
        approval_status='approved', base_commit=git('rev-parse', 'HEAD'),
        diagnosis={'root_cause': 'missing coverage', 'files': ['widgets.py'], 'reasoning': 'x'},
        plan=[Step(step_id='1', intent='Add widget regression test',
                   target_file='test_widgets.py', action='create')],
    )
    return tool, state


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
    # Re-entering the same step retains its one prior failure and remaining budget.
    assert llm.calls == 2
    assert not result.file_changes


@pytest.mark.asyncio
@pytest.mark.parametrize(('navigation_result', 'expected_status', 'expected_calls', 'expected_error'), [
    ([{'path': 'consumer.py', 'line': 4, 'verified': True}], 'running', 1, None),
    ([], 'running', 1, None),
    (None, 'needs_human', 1, 'get_callers returned no result (None)'),
    ({'path': 'consumer.py'}, 'needs_human', 1, 'get_callers returned a malformed result'),
], ids=['normal', 'empty', 'none', 'malformed'])
async def test_executor_validates_navigation_tool_contract(
    local, navigation_result, expected_status, expected_calls, expected_error, monkeypatch,
):
    monkeypatch.setattr('app.core.budget.usage', lambda _ticket_id: None)
    tool, state, _ = local

    class Navigation:
        def get_callers(self, repo, subtask_id, symbol, limit=25):
            return navigation_result

        def get_references(self, repo, subtask_id, symbol, limit=25):
            return []

    llm = LLM()
    result = await ExecutorAgent(
        llm, tool, emit=no_event, code_search=Navigation(),
    ).run(state)

    assert result.status == expected_status
    assert llm.calls == expected_calls
    if expected_error:
        assert expected_error in result.failure_reason
        assert "'NoneType' object has no attribute 'get'" not in result.failure_reason
        assert len(result.attempt_history) == 1
        context = result.failure_contexts[-1]
        assert context.classification == 'infrastructure'
        assert context.component == 'executor'
        assert context.operation == 'inspect_symbol_impacts'
        assert context.function
        assert context.file.endswith('.py')
        assert context.line > 0
        assert context.reason == 'deterministic_operation_failed'
    else:
        assert result.execution_complete
        assert result.failure_reason is None


@pytest.mark.asyncio
async def test_executor_tool_exception_escalates_without_llm_retry_and_records_context(local, monkeypatch):
    tool, state, _ = local
    from app.config import settings
    monkeypatch.setattr(settings, 'github_token', 'tool-secret-value')

    class BrokenNavigation:
        def get_callers(self, repo, subtask_id, symbol, limit=25):
            raise OSError('symbol index is unavailable: tool-secret-value')

        def get_references(self, repo, subtask_id, symbol, limit=25):
            return []

    llm = LLM()
    result = await ExecutorAgent(llm, tool, emit=no_event, code_search=BrokenNavigation()).run(state)

    assert result.status == 'needs_human'
    assert llm.calls == 1
    assert result.retry_count == 0
    context = result.failure_contexts[-1]
    assert context.classification == 'infrastructure'
    assert context.operation == 'inspect_symbol_impacts'
    assert context.exception_type == 'OSError'
    assert context.message == 'symbol index is unavailable: <redacted>'
    assert 'tool-secret-value' not in context.model_dump_json()
    assert context.identifiers['subtask_id'] == state.subtask_id
    assert context.function == 'get_callers'
    assert context.line > 0


@pytest.mark.asyncio
async def test_executor_reasoning_failure_still_retries_with_structured_context(local):
    tool, state, _ = local
    llm = LLM(invalid=1)

    result = await ExecutorAgent(llm, tool, emit=no_event).run(state)

    assert result.execution_complete, result.failure_reason
    assert llm.calls == 2
    assert result.failure_contexts[0].classification == 'reasoning'
    assert result.failure_contexts[0].operation == 'apply_edit'
    assert result.failure_contexts[0].reason == 'candidate_output_can_be_corrected'


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


@pytest.mark.asyncio
async def test_unverifiable_environment_does_not_enter_llm_repair_loop(local):
    tool, state, _ = local
    llm = LLM()
    def unavailable(_):
        return TestResult(passed=False, returncode=1,
                          output='environment variable DB_PASSWORD is required')
    result = await ExecutorAgent(llm, tool, test_runner=unavailable, emit=no_event).run(state)
    assert result.status == 'needs_human'
    assert result.verifiability == 'unverifiable'
    assert result.required_test_credentials == ['DB_PASSWORD']
    assert result.verification_summary['outcome'] == 'UNVERIFIABLE'
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_executor_treats_executed_assertion_failure_as_fail_despite_error_words(local):
    tool, state, _ = local
    llm = LLM()

    def assertion_failure(_):
        return TestResult(
            passed=False,
            returncode=1,
            output=(
                "F                                                                        [100%]\n"
                "=================================== FAILURES ===================================\n"
                "____________________________ test_access_message ____________________________\n"
                "    def test_access_message():\n"
                "        message = \"ImportError KeyError: 'API_KEY' unauthorized\"\n"
                ">       assert message == \"allowed\"\n"
                "E       assert \"ImportError KeyError: 'API_KEY' unauthorized\" == \"allowed\"\n"
                "=========================== short test summary info ============================\n"
                "FAILED test_app.py::test_access_message - assert ...\n"
                "1 failed in 0.01s\n"
            ),
        )

    result = await ExecutorAgent(llm, tool, test_runner=assertion_failure, emit=no_event).run(state)

    assert result.status == 'needs_human'
    assert result.verification_summary['outcome'] == 'FAIL'
    assert 'pytest failed (exit 1)' in result.failure_reason
    assert 'UNVERIFIABLE' not in result.failure_reason
    assert llm.calls == 3


@pytest.mark.asyncio
async def test_executor_treats_collection_import_error_as_unverifiable(local):
    tool, state, _ = local
    llm = LLM()

    def collection_failure(_):
        return TestResult(
            passed=False,
            returncode=2,
            output=(
                "ERROR collecting tests/test_app.py\n"
                "ModuleNotFoundError: No module named 'app'\n"
                "!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!\n"
                "1 error in 0.04s\n"
            ),
        )

    result = await ExecutorAgent(llm, tool, test_runner=collection_failure, emit=no_event).run(state)

    assert result.status == 'needs_human'
    assert result.verification_summary['outcome'] == 'UNVERIFIABLE'
    assert result.verifiability == 'unverifiable'
    assert 'UNVERIFIABLE' in result.failure_reason
    assert llm.calls == 1


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
            return schema.model_validate({'full_content':
                'from app import divide\nimport pytest\ndef test_zero():\n'
                '    with pytest.raises(ValueError):\n        divide(1, 0)\n'})

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
            return schema.model_validate({'full_content':
                'from app import divide\ndef test_ok():\n    assert divide(4, 2) == 2\n'})

    llm = CapturingLLM()
    result = await ExecutorAgent(llm, tool, emit=no_event).run(state)
    assert result.execution_complete, result.failure_reason
    payload = json.loads(llm.last_user)
    assert 'def divide' in payload['related_files'].get('app.py', '')  # real source, not just diagnosis prose
    assert payload['style_example']['path'] == 'test_other.py'
    assert 'def test_helper' in payload['style_example']['content']  # an existing test's real convention


@pytest.mark.asyncio
async def test_generated_test_uses_verified_constructor_and_returned_object_interface(local_with_object_interface):
    tool, state = local_with_object_interface

    class InterfaceAwareLLM:
        def __init__(self):
            self.calls = 0
            self.payload = None
        def get_usage(self, ticket_id):
            return {'calls': self.calls, 'tokens': self.calls * 10, 'est_cost_usd': 0.0}
        def complete_json(self, system, user, schema, **kwargs):
            self.calls += 1
            self.payload = json.loads(user)
            widget = self.payload['symbol_interfaces']['Widget']
            factory = self.payload['symbol_interfaces']['build_widget']
            assert widget['constructor']['required_positional'] == ['name', 'count']
            assert widget['attributes'] == ['count', 'name']
            assert factory['required_positional'] == ['name']
            assert factory['returns'] == 'Widget'
            return schema.model_validate({'full_content': (
                'from widgets import Widget, build_widget\n\n'
                'def test_widget_interface():\n'
                '    direct = Widget("direct", 2)\n'
                '    built = build_widget("made")\n'
                '    assert direct.count == 2\n'
                '    assert built.name == "made"\n'
                '    assert built.label("kind") == "kind:made"\n'
            )})

    llm = InterfaceAwareLLM()
    result = await ExecutorAgent(llm, tool, emit=no_event).run(state)

    assert result.execution_complete, result.failure_reason
    generated = result.file_changes['test_widgets.py']
    assert 'Widget("direct", 2)' in generated
    assert 'built.name' in generated
    assert 'built.nonexistent' not in generated
    assert llm.calls == 1


def test_interface_validator_rejects_nonexistent_returned_object_attribute(tmp_path):
    source = (
        'class Result:\n'
        '    def __init__(self, value):\n'
        '        self.value = value\n\n'
        'def build() -> Result:\n'
        '    return Result(1)\n'
    )
    (tmp_path / 'service.py').write_text(source)
    interfaces = resolve_test_interfaces(
        ['service.py'],
        lambda symbol: [{'path': 'service.py', 'line': 1}] if symbol in {'Result', 'build'} else [],
        lambda path: {'path': path, 'content': source},
    )

    with pytest.raises(TestInterfaceValidationError, match="has no attribute 'missing'"):
        validate_test_interfaces(
            'from service import build\ndef test_it():\n    assert build().missing == 1\n',
            interfaces,
        )


def test_interface_validator_resolves_imported_return_type(tmp_path):
    sources = {
        'service.py': (
            'from models import ServiceResult\n\n'
            'class Service:\n'
            '    def run(self) -> ServiceResult:\n'
            '        return ServiceResult(True, None)\n'
        ),
        'models.py': (
            'from dataclasses import dataclass\n\n'
            '@dataclass\n'
            'class ServiceResult:\n'
            '    passed: bool\n'
            '    reason: str | None\n'
            '    details: dict | None = None\n'
        ),
    }
    definitions = {
        'Service': [{'path': 'service.py', 'line': 3}],
        'ServiceResult': [{'path': 'models.py', 'line': 4}],
    }
    interfaces = resolve_test_interfaces(
        ['service.py'], definitions.get, lambda path: {'path': path, 'content': sources[path]},
    )

    result = validate_test_interfaces(
        'from service import Service\ndef test_it():\n'
        '    result = Service().run()\n'
        '    assert result.passed\n'
        '    assert result.reason is None\n'
        '    assert result.details is None\n',
        interfaces,
    )

    assert interfaces['ServiceResult']['attributes'] == ['details', 'passed', 'reason']
    assert result.unresolved_types == []
    assert result.skipped_checks == []


def test_interface_validator_skips_unresolvable_return_type():
    source = (
        'class Service:\n'
        '    def run(self) -> ExternalResult:\n'
        '        raise NotImplementedError\n'
    )
    interfaces = resolve_test_interfaces(
        ['service.py'],
        lambda symbol: [{'path': 'service.py', 'line': 1}] if symbol == 'Service' else [],
        lambda path: {'path': path, 'content': source},
    )

    result = validate_test_interfaces(
        'from service import Service\ndef test_it():\n'
        '    result = Service().run()\n'
        '    assert result.any_external_attribute\n',
        interfaces,
    )

    assert result.unresolved_types == ['ExternalResult']
    assert 'ExternalResult.any_external_attribute' in result.skipped_checks


def test_interface_validator_rejects_wrong_attribute_on_imported_return_type():
    sources = {
        'service.py': (
            'from models import ServiceResult\n\n'
            'class Service:\n'
            '    def run(self) -> ServiceResult:\n'
            '        return ServiceResult(True)\n'
        ),
        'models.py': (
            'from dataclasses import dataclass\n\n'
            '@dataclass\n'
            'class ServiceResult:\n'
            '    passed: bool\n'
        ),
    }
    definitions = {
        'Service': [{'path': 'service.py', 'line': 3}],
        'ServiceResult': [{'path': 'models.py', 'line': 4}],
    }
    interfaces = resolve_test_interfaces(
        ['service.py'], definitions.get, lambda path: {'path': path, 'content': sources[path]},
    )

    with pytest.raises(TestInterfaceValidationError, match="has no attribute 'invented'"):
        validate_test_interfaces(
            'from service import Service\ndef test_it():\n'
            '    result = Service().run()\n'
            '    assert result.invented\n',
            interfaces,
        )


@pytest.mark.asyncio
async def test_create_rejects_search_replace_then_writes_full_file(local_with_style_example):
    tool, state, _ = local_with_style_example

    class CreateLLM:
        def __init__(self):
            self.calls, self.prompts = 0, []
        def get_usage(self, ticket_id):
            return {'calls': self.calls, 'tokens': self.calls * 10, 'est_cost_usd': 0.0}
        def complete_json(self, system, user, schema, **kwargs):
            self.calls += 1
            self.prompts.append(json.loads(user))
            content = 'from app import divide\ndef test_ok():\n    assert divide(4, 2) == 2\n'
            if self.calls == 1:
                return schema.model_validate({'blocks': [{'search': 'anything', 'replace': content}]})
            return schema.model_validate({'full_content': content})

    pytest_calls = []
    def passing(checkout):
        pytest_calls.append(checkout)
        return TestResult(passed=True, returncode=0, output='1 passed')

    llm = CreateLLM()
    result = await ExecutorAgent(llm, tool, test_runner=passing, emit=no_event).run(state)
    assert result.execution_complete, result.failure_reason
    assert llm.calls == 2
    assert len(pytest_calls) == 1
    assert 'requires full_content, not SEARCH/REPLACE' in llm.prompts[1]['repair_feedback']
    assert result.steps_done[0]['report']['matches'][0]['tier'] == 'create'
    assert result.file_changes['test_app.py'].startswith('from app import divide')


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
async def test_executor_auto_inserts_verified_test_import_before_pytest_without_retry(local_with_style_example):
    tool, state, _ = local_with_style_example

    class RepairingTestLLM:
        def __init__(self):
            self.calls, self.prompts = 0, []
        def get_usage(self, ticket_id):
            return {'calls': self.calls, 'tokens': self.calls * 10, 'est_cost_usd': 0.0}
        def complete_json(self, system, user, schema, **kwargs):
            self.calls += 1
            self.prompts.append(user)
            content = 'def test_ok():\n    assert divide(4, 2) == 2\n'
            return schema.model_validate({'full_content': content})

    pytest_calls = []
    def passing(checkout):
        pytest_calls.append(checkout)
        return TestResult(passed=True, returncode=0, output='1 passed')

    llm = RepairingTestLLM()
    result = await ExecutorAgent(llm, tool, test_runner=passing, emit=no_event).run(state)
    assert result.execution_complete, result.failure_reason
    assert llm.calls == 1
    assert len(pytest_calls) == 1
    assert result.retry_count == 0
    assert result.file_changes['test_app.py'].startswith('from app import divide\n')


def test_validity_validator_rejects_self_contradictory_success_assertion():
    content = (
        'class Check:\n'
        '    def __init__(self, forbidden): self.forbidden = forbidden\n'
        '    def check(self, text): return type("Result", (), {"passed": False})()\n'
        'checker = Check(forbidden=["forbidden"])\n'
        'result = checker.check("contains forbidden")\n'
        'assert result.passed\n'
    )
    with pytest.raises(InvalidGeneratedTestError, match='configured forbidden'):
        validate_test_validity(content, 'Configured forbidden phrases must fail')


@pytest.mark.asyncio
async def test_invalid_generated_test_is_regenerated_without_touching_production(local_with_style_example):
    tool, state, _ = local_with_style_example

    class ContradictoryThenValidLLM:
        def __init__(self):
            self.calls, self.prompts = 0, []
        def get_usage(self, ticket_id):
            return {'calls': self.calls, 'tokens': self.calls * 10, 'est_cost_usd': 0.0}
        def complete_json(self, system, user, schema, **kwargs):
            self.calls += 1
            self.prompts.append(json.loads(user))
            assertion = 'assert result.passed' if self.calls == 1 else 'assert not result.passed'
            return schema.model_validate({'full_content': (
                'from app import divide\n\n'
                'class Check:\n'
                '    def __init__(self, forbidden): self.forbidden = forbidden\n'
                '    def check(self, text): return type("Result", (), {"passed": False})()\n\n'
                'def test_forbidden_value():\n'
                '    checker = Check(forbidden=["forbidden"])\n'
                '    result = checker.check("contains forbidden")\n'
                f'    {assertion}\n'
                '    assert divide(4, 2) == 2\n'
            )})

    pytest_calls = []
    def passing(checkout):
        pytest_calls.append(checkout)
        return TestResult(passed=True, returncode=0, output='1 passed')

    llm = ContradictoryThenValidLLM()
    result = await ExecutorAgent(llm, tool, test_runner=passing, emit=no_event).run(state)

    assert result.execution_complete, result.failure_reason
    assert llm.calls == 2
    assert len(pytest_calls) == 1
    assert 'configured forbidden' in llm.prompts[1]['repair_feedback']
    assert result.file_changes['test_app.py'].count('assert not result.passed') == 1
    assert 'app.py' not in result.file_changes


@pytest.mark.asyncio
async def test_valid_failing_generated_test_routes_to_production_repair(local_no_tests):
    tool, state, _ = local_no_tests
    state.plan = [
        Step(step_id='1', intent='Raise ValueError for zero divisor', target_file='app.py'),
        Step(step_id='2', intent='Add zero-divisor regression test', target_file='test_app.py', action='create'),
    ]

    class RepairImplementationLLM:
        def __init__(self):
            self.calls, self.prompts = 0, []
        def get_usage(self, ticket_id):
            return {'calls': self.calls, 'tokens': self.calls * 10, 'est_cost_usd': 0.0}
        def complete_json(self, system, user, schema, **kwargs):
            self.calls += 1
            payload = json.loads(user)
            self.prompts.append(payload)
            if payload['step']['target_file'] == 'test_app.py':
                return schema.model_validate({'full_content': (
                    'from app import divide\nimport pytest\n\n'
                    'def test_zero_divisor():\n'
                    '    with pytest.raises(ValueError):\n'
                    '        divide(1, 0)\n'
                )})
            replacement = ('    return 0 if b == 0 else a / b' if self.calls == 1 else
                           '    if b == 0:\n        raise ValueError("zero denominator")\n    return a / b')
            return schema.model_validate({'blocks': [{'search': '    return a / b', 'replace': replacement}]})

    def runtime(checkout):
        test_exists = (checkout / 'test_app.py').exists()
        implementation = (checkout / 'app.py').read_text()
        if not test_exists:
            return TestResult(passed=False, returncode=5, output='no tests ran')
        if 'raise ValueError' in implementation:
            return TestResult(passed=True, returncode=0, output='1 passed')
        return TestResult(passed=False, returncode=1, output='1 failed: DID NOT RAISE ValueError')

    llm = RepairImplementationLLM()
    agent = ExecutorAgent(
        llm, tool, test_runner=runtime, emit=no_event,
        failure_scope_classifier=lambda *_: {
            'classification': 'ticket_change',
            'failing_behavior': 'zero divisor still does not raise ValueError',
            'evidence': ['the failing assertion directly exercises the approved zero-divisor requirement'],
            'suspected_root_cause': 'the approved production guard is incomplete',
        },
    )
    after_code = await agent.run(state)
    before_test = after_code.model_copy(deep=True)
    after_test_failure = await agent.run(after_code)
    from app.core.guard import validate_output
    after_test_failure = validate_output(before_test, after_test_failure, 'execute')
    failure_snapshot = after_test_failure.model_copy(deep=True)
    after_repair = await agent.run(after_test_failure)
    result = await agent.run(after_repair)

    assert failure_snapshot.current_step == 0
    assert 'app.py' not in failure_snapshot.file_changes
    assert 'test_app.py' in failure_snapshot.pending_valid_tests
    assert failure_snapshot.repair_rewind_from is None
    assert failure_snapshot.test_failure_repair_count == 1
    assert 'Valid generated test exposed an implementation failure' in failure_snapshot.attempt_history[-1]
    assert result.execution_complete, result.failure_reason
    assert 'raise ValueError' in result.file_changes['app.py']
    assert result.pending_valid_tests == {}
    assert llm.calls == 3
    assert 'Valid generated test exposed an implementation failure' in llm.prompts[2]['repair_feedback']


@pytest.mark.asyncio
async def test_valid_test_exposing_unrelated_defect_stops_without_repair(local_no_tests):
    tool, state, git = local_no_tests
    (tool.remote / 'matcher.py').write_text(
        'def contains_phrase(text, phrase):\n    return phrase in text\n'
    )
    git('add', 'matcher.py')
    git('commit', '-m', 'add unrelated matcher behavior')
    state.base_commit = git('rev-parse', 'HEAD')
    state.plan = [
        Step(step_id='1', intent='Ignore an empty configured phrase', target_file='app.py'),
        Step(step_id='2', intent='Add empty-phrase regression test', target_file='test_app.py', action='create'),
    ]

    class TicketLLM:
        def __init__(self): self.calls = 0
        def get_usage(self, _): return {'calls': self.calls, 'tokens': self.calls * 10, 'est_cost_usd': 0.0}
        def complete_json(self, system, user, schema, **kwargs):
            self.calls += 1
            payload = json.loads(user)
            if payload['step']['target_file'] == 'test_app.py':
                return schema.model_validate({'full_content': (
                    'from matcher import contains_phrase\n\n'
                    'def test_empty_phrase_config():\n'
                    '    assert not contains_phrase("forbidden phrases", "forbidden phrase")\n'
                )})
            return schema.model_validate({'blocks': [{
                'search': '    return a / b', 'replace': '    return 0 if b == 0 else a / b',
            }]})

    def runtime(checkout):
        if not (checkout / 'test_app.py').exists():
            return TestResult(passed=False, returncode=5, output='no tests ran')
        return TestResult(passed=False, returncode=1,
                          output='FAILED test_app.py::test_empty_phrase_config - AssertionError\n1 failed')

    llm = TicketLLM()
    classify_calls = []
    def classify(*_):
        classify_calls.append(True)
        return {
            'classification': 'unrelated_defect',
            'failing_behavior': "'forbidden phrase' matches inside 'forbidden phrases'",
            'evidence': ['the assertion exercises non-empty substring boundaries, not empty-entry handling'],
            'suspected_root_cause': 'matcher uses raw substring semantics',
        }

    agent = ExecutorAgent(llm, tool, test_runner=runtime, emit=no_event,
                          failure_scope_classifier=classify)
    after_code = await agent.run(state)
    result = await agent.run(after_code)

    assert result.status == 'needs_human'
    assert llm.calls == 2  # code + test; no production-repair call
    assert len(classify_calls) == 1
    assert result.test_failure_repair_count == 0
    assert result.discovered_defect['classification'] == 'unrelated_defect'
    assert result.discovered_defect['options'] == ['stay_scope', 'expand_scope', 'abort']
    assert 'raw substring semantics' in result.failure_reason
    assert 'matcher.py' not in result.file_changes


@pytest.mark.asyncio
async def test_stay_scope_refined_test_still_fails_against_unfixed_baseline(local_no_tests):
    tool, state, _ = local_no_tests
    state.description = 'Raise ValueError when divisor is zero'
    state.diagnosis = {'root_cause': 'zero divisor is not guarded', 'files': ['app.py'], 'reasoning': 'source'}
    state.plan = [
        Step(step_id='1', intent='Guard zero divisor', target_file='app.py'),
        Step(step_id='2', intent='Add isolated zero-divisor test', target_file='test_app.py', action='create'),
    ]
    state.current_step = 1
    state.file_changes = {
        'app.py': 'def divide(a, b):\n    if b == 0:\n        raise ValueError("zero denominator")\n    return a / b\n'
    }
    state.scope_resolution = 'stay_scope'
    state.discovered_defect = {
        'classification': 'unrelated_defect', 'failing_behavior': 'separate behavior',
        'evidence': ['prior valid test'], 'suspected_root_cause': 'separate matcher bug',
        'test_path': 'test_app.py', 'test_step_index': 1,
    }

    class RefinedTestLLM:
        def __init__(self): self.calls = 0
        def get_usage(self, _): return {'calls': self.calls, 'tokens': 10, 'est_cost_usd': 0.0}
        def complete_json(self, system, user, schema, **kwargs):
            self.calls += 1
            return schema.model_validate({'full_content': (
                'from app import divide\nimport pytest\n\n'
                'def test_zero_only():\n'
                '    with pytest.raises(ValueError):\n'
                '        divide(1, 0)\n'
            )})

    observed = []
    def runtime(checkout):
        fixed = 'raise ValueError' in (checkout / 'app.py').read_text()
        observed.append(fixed)
        if fixed:
            return TestResult(passed=True, returncode=0, output='1 passed')
        return TestResult(passed=False, returncode=1,
                          output='FAILED test_app.py::test_zero_only - ZeroDivisionError\n1 failed')

    result = await ExecutorAgent(
        RefinedTestLLM(), tool, test_runner=runtime, emit=no_event,
        failure_scope_classifier=lambda *_: pytest.fail('scope classification is not needed for a passing refinement'),
    ).run(state)

    assert result.execution_complete, result.failure_reason
    assert observed == [True, False]  # fixed artifact passes; original unfixed code fails
    assert result.discovered_defect['baseline_verification']['verification_outcome'] == 'FAIL'
    assert result.scope_resolution is None


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

    class RepairLLM(LLM):
        def complete_json(self, *args, **kwargs):
            proposal = super().complete_json(*args, **kwargs)
            if self.calls > 1:
                proposal.blocks[0].replace = proposal.blocks[0].replace.replace(
                    '"zero denominator"', '"divisor must be nonzero"'
                )
            return proposal

    llm = RepairLLM()
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
    class Decomposer:
        def run(self, state):
            state.subtask_specs = [{'spec_id': '1', 'type': state.subtask_type, 'description': state.description,
                                    'repo': state.repo, 'depends_on': []}]
            state.decomposition_reasoning = 'One clear bug fix.'
            return state
    class ApprovingCritic:
        def run(self, state):
            state.critic_verdict = {'approved': True, 'issues': [], 'verifiability': 'ok',
                                    'test_validity': 'valid', 'test_issues': [],
                                    'implementation_valid': True}
            return state
    state.approval_status = 'pending'
    state.confirmed_repos = [state.repo]
    saver = MemorySaver()
    graph = module.build_graph(agent=Diagnosis(), planner=Planner(), decomposer=Decomposer(),
                               executor=ExecutorAgent(LLM(), tool, emit=no_event), memory_search=lambda *a, **k: [],
                               critic=ApprovingCritic(), publisher=github, checkpointer=saver, activity_check=lambda state: None)
    config = module.thread_config(state.ticket_id, state.subtask_id)
    answer = ApprovalDecision(approval_status='approved')
    await graph.ainvoke(state.model_dump(), config)
    await module.resume_approval(graph, state.ticket_id, state.subtask_id, answer)
    assert (await graph.aget_state(config)).next == ('human_gate',)
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


# ── Tests 8 & 9: requirement alignment integration ────────────────────────────

@pytest.fixture
def local_with_scenario_check(local_with_style_example):
    tool, state, git = local_with_style_example
    path = tool.remote / 'app.py'
    path.write_text(path.read_text() + (
        '\nfrom types import SimpleNamespace\n'
        'def check(value):\n'
        '    return SimpleNamespace(passed=value != "forbidden")\n'
    ))
    git('add', 'app.py')
    git('commit', '-m', 'Add scenario checker fixture')
    state.base_commit = git('rev-parse', 'HEAD')
    return tool, state, git


def _make_alignment_llm(calls_store, prompts_store):
    """LLM mock: call 1 returns FAIL assertion (contradicts PASS requirement), call 2 returns PASS."""
    class _LLM:
        def get_usage(self, ticket_id):
            return {'calls': len(calls_store), 'tokens': len(calls_store) * 10, 'est_cost_usd': 0.0}
        def complete_json(self, system, user, schema, **kwargs):
            calls_store.append(1)
            prompts_store.append(json.loads(user))
            assertion = 'assert not result.passed' if len(calls_store) == 1 else 'assert result.passed'
            return schema.model_validate({'full_content': (
                'from app import check\n\n'
                'def test_empty_phrase():\n'
                '    result = check("")\n'
                f'    {assertion}\n'
            )})
    return _LLM()


@pytest.mark.asyncio
async def test_requirement_contradicting_test_is_regenerated_not_production_modified(
    local_with_scenario_check,
):
    """Test 8: A requirement-contradicting generated test retries test generation only.

    Critical invariant: production code (app.py) must not appear in file_changes after
    a test that asserts the opposite of the approved requirement is rejected.
    """
    tool, state, _ = local_with_scenario_check
    state.description = 'Empty strings should be allowed'

    before = (tool.remote / 'app.py').read_bytes()
    calls, prompts = [], []
    llm = _make_alignment_llm(calls, prompts)

    pytest_calls = []

    def passing(checkout):
        pytest_calls.append(checkout)
        assert (checkout / 'app.py').read_bytes() == before
        return run_tests(checkout)

    result = await ExecutorAgent(llm, tool, test_runner=passing, emit=no_event).run(state)

    assert result.execution_complete, result.failure_reason
    assert len(calls) == 2                       # two test-generation attempts
    assert len(pytest_calls) == 1                # pytest ran only for the accepted (second) test
    assert 'app.py' not in result.file_changes   # production code never modified
    assert (tool.remote / 'app.py').read_bytes() == before
    assert result.test_failure_repair_count == 0
    assert result.retry_attempts['1'][0].failure_type == 'RequirementContradictionError'


@pytest.mark.asyncio
async def test_requirement_contradiction_retry_carries_explicit_evidence(
    local_with_scenario_check,
):
    """Test 9: The retry prompt after a requirement contradiction includes specific evidence.

    The second LLM call's repair_feedback must name:
    - the approved outcome (PASS)
    - the rejected test's outcome (FAIL)
    - an explicit instruction to regenerate the TEST (not the implementation)
    """
    tool, state, _ = local_with_scenario_check
    state.description = 'Empty strings should be allowed'

    before = (tool.remote / 'app.py').read_bytes()
    calls, prompts = [], []
    llm = _make_alignment_llm(calls, prompts)

    def passing(checkout):
        assert (checkout / 'app.py').read_bytes() == before
        return run_tests(checkout)

    await ExecutorAgent(llm, tool, test_runner=passing, emit=no_event).run(state)

    assert len(calls) == 2
    repair = prompts[1].get('repair_feedback', '')
    assert 'PASS' in repair      # approved requirement behavior is named
    assert 'FAIL' in repair      # rejected test behavior is named
    assert 'Regenerate' in repair  # explicit instruction to regenerate


@pytest.mark.asyncio
@pytest.mark.parametrize('ambiguous', [False, True], ids=['nomatch', 'ambiguous'])
async def test_failed_edit_evidence_reaches_attempt_two(local, ambiguous, tmp_path):
    tool, state, git = local
    before = 'def divide(a, b):\n    return a / b\n'
    if ambiguous:
        before += '\ndef other(a, b):\n    return a / b\n'
        (tool.remote / 'app.py').write_text(before)
        git('add', 'app.py')
        git('commit', '-m', 'duplicate region')
        state.base_commit = git('rev-parse', 'HEAD')
    failed_search = '    return a / b' if ambiguous else 'this does not exist at all'
    replacement = '    if b == 0:\n        raise ValueError("zero denominator")\n    return a / b'

    class EvidenceLLM(LLM):
        def complete_json(self, system, user, schema, **kwargs):
            self.calls += 1
            self.prompts.append(user)
            if self.calls == 2:
                assert tool.execution_file(
                    tool._checkout_path(state.repo, state.subtask_id), 'app.py'
                ).read_text() == before
            search = failed_search if self.calls == 1 else 'def divide(a, b):\n    return a / b'
            replace = replacement if self.calls == 1 else 'def divide(a, b):\n' + replacement
            return schema.model_validate({'blocks': [{'search': search, 'replace': replace}]})

    llm = EvidenceLLM()
    result = await ExecutorAgent(llm, tool, emit=no_event).run(state)
    assert result.execution_complete, result.failure_reason
    assert llm.calls == 2
    payload = json.loads(llm.prompts[1])
    assert payload['file_text'] == before
    failed = payload['failed_edit_attempt']
    assert failed['attempt'] == 1
    assert failed['target_file'] == 'app.py'
    assert failed['candidate_content'] == [{'search': failed_search, 'replace': replacement}]
    assert failed['evidence']['search'] == failed_search
    assert failed['failure_type'] == ('Ambiguous' if ambiguous else 'NoMatch')
    assert failed['failure_message']
    if ambiguous:
        assert failed['evidence']['match_count'] == 2
        assert [loc['start_line'] for loc in failed['evidence']['locations']] == [2, 5]
        assert all(loc['context'] for loc in failed['evidence']['locations'])
        assert 'unique intended region' in failed['corrective_instruction']
        assert 'never select a match' in failed['corrective_instruction']
    else:
        assert failed['evidence']['closest_source']['context'] == before
        assert 0 <= failed['evidence']['closest_source']['similarity'] < .8
        assert 'Ground SEARCH exactly in current file_text' in failed['corrective_instruction']
        assert 'do not blindly reuse' in failed['corrective_instruction']
    assert SubtaskState.model_validate_json(result.model_dump_json()).failed_edit_attempt == result.failed_edit_attempt
    # Capture the actual model input for the phase verification report.
    (tmp_path / 'attempt-two.json').write_text(
        json.dumps(payload, indent=2) + '\n'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['apply_edit', 'write_execution_file', 'prepare_execution'])
async def test_edit_infrastructure_failure_never_retries(local, monkeypatch, operation):
    import app.agents.executor as executor
    tool, state, _ = local
    def crash(*args, **kwargs):
        raise OSError('deterministic tool unavailable')
    if operation == 'apply_edit':
        monkeypatch.setattr(executor, 'apply_edits', crash)
    else:
        monkeypatch.setattr(tool, operation, crash)
    llm = LLM()
    result = await ExecutorAgent(llm, tool, emit=no_event).run(state)
    assert llm.calls == (0 if operation == 'prepare_execution' else 1)
    assert result.retry_count == 0
    assert result.failed_edit_attempt is None
    assert result.failure_contexts[-1].classification == 'infrastructure'
    assert result.failure_contexts[-1].operation == operation


@pytest.mark.asyncio
@pytest.mark.parametrize('requirement', ['Empty strings should pass.', 'Handle empty strings.'])
async def test_contradictory_assertions_regenerate_only_test(local_with_style_example, monkeypatch, requirement):
    from tests.phase05.test_retry_evidence import Candidates
    tool, state, _ = local_with_style_example
    state.description = requirement
    before = (tool.remote / 'app.py').read_bytes()
    good = ('from app import divide\nfrom types import SimpleNamespace\n'
            'def test_result():\n    result = SimpleNamespace(passed=divide(4, 2) == 2)\n'
            '    assert result.passed\n')
    bad = good + '    assert not result.passed\n'
    llm = Candidates([{'full_content': bad}, {'full_content': good}])
    writes, tested = [], []
    write = tool.write_execution_file

    def track_write(checkout, path, content):
        writes.append(path)
        assert path != 'app.py'
        return write(checkout, path, content)

    def passing(checkout):
        tested.append(tool.execution_file(checkout, state.plan[0].target_file).read_text())
        assert tested[-1] == good
        assert (checkout / 'app.py').read_bytes() == before
        return run_tests(checkout)

    monkeypatch.setattr(tool, 'write_execution_file', track_write)
    result = await ExecutorAgent(llm, tool, test_runner=passing, emit=no_event).run(state)
    assert result.execution_complete, result.failure_reason
    assert len(llm.prompts) == 2
    assert tested == [good]
    assert writes and set(writes) == {state.plan[0].target_file}
    assert 'app.py' not in result.file_changes
    assert (tool.remote / 'app.py').read_bytes() == before
    checkout = tool._checkout_path(state.repo, state.subtask_id)
    assert (checkout / 'app.py').read_bytes() == before
    feedback = llm.prompts[1]['repair_feedback']
    assert 'contradictory expected outcomes: PASS and FAIL' in feedback
    assert 'Regenerate the TEST' in feedback
    attempt = result.retry_attempts[state.plan[0].step_id][0]
    assert attempt.failure_type == 'InvalidGeneratedTestError'
    assert attempt.operation == 'validate_test_semantics'
    assert attempt.candidate_type == 'generated_test'
    assert not result.pending_valid_tests
    assert result.test_failure_repair_count == 0


@pytest.mark.asyncio
async def test_semantic_validator_crash_stops_without_reasoning_retry(local_with_style_example, monkeypatch):
    from app.agents import executor
    from tests.phase05.test_retry_evidence import Candidates
    tool, state, _ = local_with_style_example
    llm = Candidates([{'full_content': 'from app import divide\ndef test_result():\n    assert divide(4, 2) == 2\n'}])
    tested = []

    def crash(*args, **kwargs):
        raise RuntimeError('semantic validator crashed')

    monkeypatch.setattr(executor, 'validate_test_validity', crash)
    result = await ExecutorAgent(llm, tool, test_runner=lambda checkout: tested.append(checkout), emit=no_event).run(state)
    assert result.status == 'needs_human'
    assert len(llm.prompts) == 1
    assert result.retry_count == 0
    assert not tested
    assert not result.retry_attempts.get(state.plan[0].step_id)
    context = result.failure_contexts[-1]
    assert context.classification == 'infrastructure'
    assert context.operation == 'validate_test_semantics'
    assert context.exception_type == 'RuntimeError'
    assert context.function == 'crash'
    assert context.line > 0 and context.stack
    assert 'app.py' not in result.file_changes


@pytest.mark.asyncio
@pytest.mark.parametrize('reverse', [False, True])
async def test_unrelated_regression_consumes_no_reasoning_retry(local_with_scenario_check, reverse):
    from tests.phase05.test_retry_evidence import Candidates
    tool, state, _ = local_with_scenario_check
    state.description = 'Empty strings should be allowed.'
    before = (tool.remote / 'app.py').read_bytes()
    parts = [
        'def test_empty():\n    result = check("")\n    assert result.passed\n',
        'def test_forbidden():\n    result = check("forbidden")\n    assert not result.passed\n',
    ]
    content = 'from app import check\n' + ''.join(reversed(parts) if reverse else parts)
    llm = Candidates([{'full_content': content}])
    tested = []

    def runner(checkout):
        tested.append(checkout)
        assert (checkout / 'app.py').read_bytes() == before
        return run_tests(checkout)

    result = await ExecutorAgent(llm, tool, test_runner=runner, emit=no_event).run(state)
    assert result.execution_complete, result.failure_reason
    assert len(llm.prompts) == len(tested) == 1
    assert result.retry_count == 0
    assert not result.retry_attempts.get('1')
    assert result.test_failure_repair_count == 0
    assert 'app.py' not in result.file_changes
    assert (tool.remote / 'app.py').read_bytes() == before
    alignment = result.test_validity_checks['test_app.py']['alignment']
    assert alignment['test_behavior'] == 'MIXED'
    assert sorted(s['alignment'] for s in alignment['scenarios']) == ['ALIGNED', 'UNKNOWN']
