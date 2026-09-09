"""Execute one approved step per graph node with bounded repair attempts."""
import asyncio
import hashlib
import json
from app.agents.execution import EditProposal, StepResult
from app.agents.llm import LLMClient
from app.agents.planning import is_test_path
from app.agents.router import model_tier
from app.agents.state import BudgetUsed, SubtaskState
from app.config import settings
from app.core.failures import describe_failure
from app.events import log_event
from app.tools.edit_applier import EditReport, apply_edits
from app.tools.edit_guard import check_edit
from app.tools.repo_tool import RepoTool
from app.tools.test_import_validator import validate_test_imports
from app.tools.test_runner import TestResult, run_tests


class ExecutorAgent:
    def __init__(self, llm=None, repo_tool=None, test_runner=run_tests, emit=log_event):
        self.llm = llm or LLMClient()
        self.repo_tool = repo_tool or RepoTool()
        self.test_runner = test_runner
        self.emit = emit

    async def event(self, state, stage, message):
        await self.emit(ticket_id=state.ticket_id, subtask_id=state.subtask_id,
                        agent='executor', stage=stage, message=message)

    async def run(self, state: SubtaskState) -> SubtaskState:
        if state.approval_status != 'approved' or not state.plan:
            return self.fail(state, 'Execution requires an approved plan')
        if state.current_step >= len(state.plan):
            state.execution_complete = True
            return state
        step = state.plan[state.current_step]
        try:
            checkout = await asyncio.to_thread(self.repo_tool.prepare_execution, state.repo,
                state.subtask_id, state.base_commit, state.file_changes)
            state.execution_branch = self.repo_tool.branch_name(state.subtask_id)
            target = self.repo_tool.execution_file(checkout, step.target_file)
            exists = target.exists()
            # Ground the step in what the repo actually contains (R-46): a plan
            # can only create a file that doesn't exist, or edit/delete one that does.
            if step.action == 'create' and exists:
                return self.fail(state, f"Step {step.step_id} marks {step.target_file!r} as a new file, "
                                         "but it already exists in the repo — plan and repository are out of sync")
            if step.action in ('edit', 'delete') and not exists:
                return self.fail(state, f"Step {step.step_id} would {step.action} {step.target_file!r}, but "
                                         "that file does not exist in the repo — plan and repository are out of sync")
            before = await asyncio.to_thread(target.read_text, encoding='utf-8') if exists else ''
            if step.action == 'delete':
                await self.event(state, 'step_started', f'{step.step_id}: {step.intent} ({step.target_file}, delete)')
                return await self._run_delete(state, step, checkout, before)
            if len(before) > settings.max_edit_file_chars:
                return self.fail(state, f'{step.target_file} exceeds the edit context limit')
            await self.event(state, 'step_started', f'{step.step_id}: {step.intent} ({step.target_file}, {step.action})')
            # Ground test-writing in the REAL code under test (R-32/R-46): a test
            # step gets the actual current content of the file(s) it covers, and a
            # sample of an existing test for style/framework — never just the
            # diagnosis's prose description, which is how a step invents a symbol
            # name that was never imported (NameError).
            related_files, style_example = ({}, None)
            if is_test_path(step.target_file):
                related_files, style_example = await asyncio.to_thread(self._test_grounding, state, checkout, step)
            error = ''
            # Count deltas so usage remains correct after a restart with an empty
            # in-process LLM usage cache.
            for attempt in range(settings.max_agent_retries + 1):
                if state.budget_used.calls >= settings.ticket_call_budget or state.budget_used.est_cost_usd >= settings.ticket_cost_budget_usd:
                    return self.fail(state, 'Ticket budget exhausted before execution')
                usage_before = self.llm.get_usage(state.ticket_id)
                try:
                    proposal = await asyncio.to_thread(self.llm.complete_json, _SYSTEM,
                        json.dumps({'step': step.model_dump(), 'description': state.description,
                                    'diagnosis': state.diagnosis, 'file_text': before, 'repair_feedback': error,
                                    'related_files': related_files, 'style_example': style_example}),
                        EditProposal, tier=model_tier('executor_apply'), ticket_id=state.ticket_id)
                    proposal = EditProposal.model_validate(proposal)
                    await self.event(state, 'edit_proposed', proposal.model_dump_json())
                    if proposal.unable_reason:
                        return self.fail(state, proposal.unable_reason)
                    after, report = apply_edits(before, proposal.blocks)
                    check_edit(step.target_file, before, after)
                    await asyncio.to_thread(self.repo_tool.write_execution_file, checkout, step.target_file, after)
                    await self.event(state, 'edit_applied', report.model_dump_json())
                    if is_test_path(step.target_file):
                        await asyncio.to_thread(
                            validate_test_imports,
                            checkout,
                            step.target_file,
                            list(related_files),
                        )
                        await self.event(state, 'test_imports_validated', step.target_file)
                    result = await asyncio.to_thread(self.test_runner, checkout)
                    await self.event(state, 'tests', result.model_dump_json())
                    # Tests can modify their checkout. Reconstruct the validated
                    # artifacts for the next step/publication, never trust those files.
                    verdict, message = self._test_verdict(state, result)
                    if verdict == 'retry':
                        raise ValueError(message)
                    if verdict == 'escalate':
                        state.verifiability = 'no_tests'
                        return self.fail(state, message)
                    record = StepResult(step_id=step.step_id, intent=step.intent, target_file=step.target_file,
                        content=after, before_sha256=hashlib.sha256(before.encode()).hexdigest(), report=report, tests=result)
                    self._record_step(state, step, record, after)
                    await self.event(state, 'step_done', f'{step.step_id}: applied fix; {result.outcome}')
                    return state
                except Exception as exc:
                    error = describe_failure(f'Executing step {step.step_id}', exc)
                    state.retry_count = attempt + 1
                    await self.event(state, 'retry' if attempt < settings.max_agent_retries else 'needs_human', error)
                    # Restore the original current-step input for every repair.
                    checkout = await asyncio.to_thread(self.repo_tool.prepare_execution, state.repo,
                        state.subtask_id, state.base_commit, state.file_changes)
                finally:
                    usage_after = self.llm.get_usage(state.ticket_id)
                    for field in ('calls', 'tokens', 'est_cost_usd'):
                        setattr(state.budget_used, field, getattr(state.budget_used, field) +
                                max(0, usage_after[field] - usage_before[field]))
            return self.fail(state, f'Retry limit reached: {error}')
        except Exception as exc:
            return self.fail(state, describe_failure('Preparing approved execution', exc))

    async def _run_delete(self, state: SubtaskState, step, checkout, before: str) -> SubtaskState:
        await asyncio.to_thread(self.repo_tool.delete_execution_file, checkout, step.target_file)
        await self.event(state, 'edit_applied', json.dumps({'deleted': step.target_file}))
        result = await asyncio.to_thread(self.test_runner, checkout)
        await self.event(state, 'tests', result.model_dump_json())
        verdict, message = self._test_verdict(state, result)
        if verdict in ('retry', 'escalate'):
            # Deletion is deterministic — retrying reproduces the same result, so
            # a bad outcome escalates immediately instead of looping.
            if verdict == 'escalate':
                state.verifiability = 'no_tests'
            return self.fail(state, f'Deleting {step.target_file}: {message}')
        record = StepResult(step_id=step.step_id, intent=step.intent, target_file=step.target_file,
            content='', before_sha256=hashlib.sha256(before.encode()).hexdigest(),
            report=EditReport(matches=[]), tests=result)
        self._record_step(state, step, record, None)
        await self.event(state, 'step_done', f'{step.step_id}: deleted {step.target_file}; {result.outcome}')
        return state

    def _test_verdict(self, state: SubtaskState, result: TestResult) -> tuple[str, str | None]:
        """Classify a pytest run per ai_rules.md R-46: exit 5 (no tests collected)
        is never a failure. 'retry' repairs (LLM path) or escalates once
        (delete path); 'escalate' stops immediately; 'defer'/'continue' proceed."""
        outcome = result.outcome
        if outcome == 'passed':
            return 'continue', None
        if outcome == 'no_tests_collected':
            already_added_test = any(
                s.action == 'create' and is_test_path(s.target_file)
                for s in state.plan[: state.current_step + 1]
            )
            if already_added_test:
                return 'retry', f'pytest still collected no tests after adding a test file (exit 5): {result.output}'
            if any(s.action == 'create' and is_test_path(s.target_file) for s in state.plan):
                # A later step still adds tests; nothing to verify against yet.
                return 'defer', None
            return 'escalate', ('Unverifiable: pytest collected no tests (exit 5) and this plan adds none — '
                                 f'flagging rather than assuming the change is safe (R-32). {result.output}'.strip())
        if outcome == 'timed_out':
            return 'retry', f'pytest timed out: {result.output}'
        hint = ''
        if any(marker in result.output for marker in ('NameError', 'ImportError', 'ModuleNotFoundError')):
            hint = ('Likely a missing/incorrect import — import the real symbol from its actual module. ')
        return 'retry', f'{hint}pytest failed (exit {result.returncode}): {result.output}'

    def _test_grounding(self, state: SubtaskState, checkout, step) -> tuple[dict[str, str], dict[str, str] | None]:
        """Real source content + an existing test's style — generic across any
        repo, never a hardcoded file/framework (R-32/R-46)."""
        candidates = list(dict.fromkeys(
            [s.target_file for s in state.plan if s.target_file != step.target_file and not is_test_path(s.target_file)]
            + list((state.diagnosis or {}).get('files', []))
        ))
        related_files: dict[str, str] = {}
        for path in candidates[:5]:
            try:
                target = self.repo_tool.execution_file(checkout, path)
                if target.exists():
                    related_files[path] = target.read_text(encoding='utf-8')[:settings.max_edit_file_chars]
            except Exception:
                continue
        style_example = None
        try:
            existing_tests = [f for f in self.repo_tool.list_files(state.repo, state.subtask_id)
                              if is_test_path(f) and f != step.target_file]
        except Exception:
            existing_tests = []
        if existing_tests:
            try:
                target = self.repo_tool.execution_file(checkout, existing_tests[0])
                style_example = {'path': existing_tests[0], 'content': target.read_text(encoding='utf-8')[:2000]}
            except Exception:
                style_example = None
        return related_files, style_example

    @staticmethod
    def _record_step(state: SubtaskState, step, record: StepResult, content: str | None) -> None:
        state.steps_done.append(record.model_dump(mode='json'))
        state.file_changes[step.target_file] = content
        state.current_step += 1
        state.retry_count = 0
        state.execution_complete = state.current_step == len(state.plan)
        if record.tests.outcome == 'passed':
            state.verifiability = 'verified'

    @staticmethod
    def fail(state, reason):
        state.status, state.failure_reason = 'needs_human', reason
        return state


_SYSTEM = """Apply only this approved step to the supplied file. Return surgical
SEARCH/REPLACE blocks quoting existing code, not a full-file rewrite or shell
commands. For a missing empty file only, a single empty search may create it.
Use repair_feedback to correct a failed match, syntax error or test failure.
Do not change the step's scope or invent requirements. If you cannot do this safely,
return no blocks and a specific unable_reason. Otherwise unable_reason is null.

When target_file is a test file: `related_files` holds the REAL, current content of
the file(s) this test covers — import and call the ACTUAL functions/classes/names
exactly as spelled and defined there, never a name recalled only from the
diagnosis/description (that causes NameError). Write one correct import statement
for the real module the code lives in. If `style_example` is present, it's an
existing test file from this same repo — match its framework (plain pytest
functions vs unittest.TestCase, fixtures used, naming) instead of inventing a
different style; if absent, use plain pytest functions. Write a complete,
self-contained, importable test with meaningful assertions covering BOTH the
previously-broken case (from diagnosis/description) and a normal case that should
still pass — a test that can't fail is not proof of the fix."""
