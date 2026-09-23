"""Execute one approved step per graph node with bounded repair attempts."""
import asyncio
import hashlib
import json
from typing import Any
from pydantic import ValidationError
from app.agents.execution import EditProposal, StepResult
from app.agents.llm import LLMClient
from app.agents.planning import is_test_path
from app.agents.router import model_tier
from app.agents.state import BudgetUsed, FailedEditAttempt, RetryAttempt, SubtaskState, UnresolvedCheck
from app.agents.test_failure_scope import TestFailureScope, classify_test_failure
from app.config import settings
from app.core.failures import describe_failure, failure_context, failure_summary
from app.agents.constraints import AUTHORITATIVE_SOURCES
from app.core.execution_constraints import ensure_requirement, scoped_constraints, constraint_description
from app.core.retry_evidence import (
    DuplicateCandidate, STEP_HISTORY_LIMIT, fingerprint, preview, proposal_content, reject_duplicate,
)
from app.core.code_impact import impact_warning, inspect_impacts, merge_impacts
from app.events import log_event
from app.tools.edit_applier import Ambiguous, EditMatch, EditReport, NoMatch, apply_edits
from app.tools.edit_guard import GuardFailure, check_edit
from app.tools.repo_tool import RepoTool
from app.tools.code_search import CodeSearchTool
from app.tools.test_import_validator import TestImportValidationError, repair_missing_test_imports
from app.tools.test_interface_validator import (
    TestInterfaceValidationError, UnresolvedTestInterfaceError,
    resolve_test_interfaces, validate_test_interfaces,
)
from app.tools.test_runner import TestResult, run_tests
from app.tools.test_preflight import clear_preflight, predict_test_credentials, remember_preflight
from app.tools.test_validity_validator import InvalidGeneratedTestError, RequirementContradictionError, validate_test_validity


class ExecutorAgent:
    def __init__(self, llm=None, repo_tool=None, test_runner=run_tests, emit=log_event, code_search=None,
                 test_preflight=predict_test_credentials, failure_scope_classifier=None,
                 checkpoint_retries=False):
        self.llm = llm or LLMClient()
        self.repo_tool = repo_tool or RepoTool()
        self.test_runner = test_runner
        self.emit = emit
        self.code_search = code_search or CodeSearchTool(repo_tool=self.repo_tool)
        self.test_preflight = test_preflight
        self.checkpoint_retries = checkpoint_retries
        self.failure_scope_classifier = failure_scope_classifier or (
            lambda state, step, content, result: classify_test_failure(
                self.llm, state, step, content, result
            )
        )

    async def event(self, state, stage, message):
        await self.emit(ticket_id=state.ticket_id, subtask_id=state.subtask_id,
                        agent='executor', stage=stage, message=message)

    async def run(self, state: SubtaskState) -> SubtaskState:
        if state.approval_status != 'approved' or not state.plan:
            return self.fail(state, 'Execution requires an approved plan')
        ensure_requirement(state)
        from app.core.constraint_conflicts import check_constraint_conflicts
        if check_constraint_conflicts(state):
            await self.event(state, 'constraint_conflict', state.failure_reason)
            return state
        if state.current_step >= len(state.plan):
            state.execution_complete = True
            return state
        step = state.plan[state.current_step]
        constraints = scoped_constraints(state, step)
        pending_test = state.pending_valid_tests.get(step.target_file) if is_test_path(step.target_file) else None
        preparation_operation = 'prepare_execution'
        try:
            checkout = await asyncio.to_thread(self.repo_tool.prepare_execution, state.repo,
                state.subtask_id, state.base_commit, state.file_changes)
            state.execution_branch = self.repo_tool.branch_name(state.subtask_id)
            preparation_operation = 'resolve_execution_file'
            target = self.repo_tool.execution_file(checkout, step.target_file)
            exists = target.exists()
            logical_exists = exists if pending_test is None else pending_test["action"] == "edit"
            # Ground the step in what the repo actually contains (R-46): a plan
            # can only create a file that doesn't exist, or edit/delete one that does.
            if step.action == 'create' and logical_exists:
                return self.fail(state, f"Step {step.step_id} marks {step.target_file!r} as a new file, "
                                         "but it already exists in the repo — plan and repository are out of sync")
            if step.action in ('edit', 'delete') and not logical_exists:
                return self.fail(state, f"Step {step.step_id} would {step.action} {step.target_file!r}, but "
                                         "that file does not exist in the repo — plan and repository are out of sync")
            before = (pending_test["before"] if pending_test is not None else
                      await asyncio.to_thread(target.read_text, encoding='utf-8') if logical_exists else '')
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
            related_files, style_example, symbol_interfaces = ({}, None, {})
            if is_test_path(step.target_file):
                preparation_operation = 'ground_test_interface'
                related_files, style_example, symbol_interfaces = await asyncio.to_thread(
                    self._test_grounding, state, checkout, step
                )
            state.failed_edit_attempt = None
            error = '\n'.join(state.attempt_history)
            history = state.retry_attempts.setdefault(step.step_id, [])
            # Never evict an active plan step (which could still be revisited).
            active_ids = {item.step_id for item in state.plan}
            for old_id in list(state.retry_attempts):
                if len(state.retry_attempts) <= STEP_HISTORY_LIMIT:
                    break
                if old_id not in active_ids:
                    del state.retry_attempts[old_id]
            # Count deltas so usage remains correct after a restart with an empty
            # in-process LLM usage cache.
            for attempt in range(len(history), settings.max_agent_retries + 1):
                from app.core.budget import usage, budget_reason
                saved = usage(state.ticket_id)
                reason = budget_reason(saved or state.budget_used.model_dump(), saved.get('limits') if saved else None)
                if reason:
                    return self.fail(state, reason)
                usage_before = self.llm.get_usage(state.ticket_id)
                operation = 'llm.complete_json'
                candidate = ''
                candidate_hash = test_hash = None
                test_content = None
                try:
                    if pending_test is not None:
                        after = pending_test["content"]
                        report = EditReport(matches=[EditMatch(block=0, start=0, end=0, tier='create')])
                    else:
                        proposal = await asyncio.to_thread(self.llm.complete_json, _SYSTEM,
                            json.dumps({'step': step.model_dump(), 'description': constraint_description(state),
                                        'diagnosis': state.diagnosis, 'file_text': before, 'repair_feedback': error,
                                        'retry_attempts': [item.model_dump(mode='json') for item in history],
                                        'execution_constraints': constraints,
                                        'constraint_resolutions': [item.model_dump(mode='json') for item in state.constraint_resolutions],
                                        'failed_edit_attempt': (state.failed_edit_attempt.model_dump(mode='json')
                                            if state.failed_edit_attempt else None),
                                        'related_files': related_files, 'symbol_interfaces': symbol_interfaces,
                                        'style_example': style_example}),
                            EditProposal, tier=model_tier('executor_apply'), ticket_id=state.ticket_id)
                        operation = 'validate_model_output'
                        proposal = EditProposal.model_validate(proposal)
                        await self.event(state, 'edit_proposed', proposal.model_dump_json())
                        if proposal.unable_reason:
                            return self.fail(state, proposal.unable_reason)
                        candidate = proposal_content(step.action, step.target_file, proposal)
                        candidate_hash = fingerprint(candidate)
                        operation = 'check_duplicate_candidate'
                        reject_duplicate(history, candidate_hash)
                        if step.action == 'create':
                            operation = 'validate_create_output'
                            if proposal.full_content is None:
                                raise ValueError(
                                    f"Creating {step.target_file} requires full_content, not SEARCH/REPLACE blocks"
                                )
                            after = proposal.full_content
                            report = EditReport(matches=[
                                EditMatch(block=0, start=0, end=0, tier='create')
                            ])
                        else:
                            operation = 'validate_edit_output'
                            if not proposal.blocks:
                                raise ValueError(
                                    f"Editing {step.target_file} requires SEARCH/REPLACE blocks, not full_content"
                                )
                            operation = 'apply_edit'
                            after, report = apply_edits(before, proposal.blocks)
                            operation = 'inspect_symbol_impacts'
                            impacts = await asyncio.to_thread(
                                inspect_impacts, state, step.target_file, before,
                                [block.search for block in proposal.blocks], self.code_search,
                                updated_source=after,
                            )
                            merge_impacts(state, impacts)
                            await self.event(state, 'impact_checked',
                                             f"Checked callers/references for {len(impacts)} changed symbol(s)")
                            warning = impact_warning(impacts)
                            if warning:
                                await self.event(state, 'impact_warning', warning)
                    if is_test_path(step.target_file):
                        test_content = after
                        test_hash = fingerprint(after)
                        operation = 'check_duplicate_candidate'
                        reject_duplicate(history, test_hash, generated_test=True)
                        operation = 'validate_test_interface'
                        interface_result = validate_test_interfaces(after, symbol_interfaces)
                        if interface_result.unresolved_types:
                            records = _record_interface_uncertainties(
                                state, interface_result, step.target_file,
                            )
                            await self.event(
                                state, 'test_interfaces_unresolved',
                                json.dumps({
                                    **interface_result.model_dump(mode='json'),
                                    'records': [item.model_dump(mode='json') for item in records],
                                }),
                            )
                    operation = 'validate_edit'
                    check_edit(step.target_file, before, after)
                    operation = 'write_execution_file'
                    await asyncio.to_thread(self.repo_tool.write_execution_file, checkout, step.target_file, after)
                    await self.event(state, 'edit_applied', report.model_dump_json())
                    if is_test_path(step.target_file):
                        operation = 'repair_test_imports'
                        after, repaired = await asyncio.to_thread(
                            repair_missing_test_imports, checkout, step.target_file,
                            list(related_files),
                            lambda symbol: self.code_search.find_symbol(
                                state.repo, state.subtask_id, symbol, limit=20,
                            ),
                        )
                        if repaired:
                            await self.event(state, 'test_imports_repaired',
                                             f"Inserted verified imports for: {', '.join(repaired)}")
                        await self.event(state, 'test_imports_validated', step.target_file)
                        operation = 'validate_test_semantics'
                        requirement = '\n'.join(item['text'] for item in constraints
                                                if item['source'] in AUTHORITATIVE_SOURCES)
                        validity = validate_test_validity(after, requirement)
                        state.test_validity_checks[step.target_file] = validity.model_dump(mode='json')
                        state.test_validity_checks[step.target_file]['execution_constraints'] = constraints
                        await self.event(state, 'test_validity_checked',
                                         f'{step.target_file}: deterministic consistency checks passed')
                    operation = 'run_tests'
                    result = await self._run_tests(state, checkout)
                    await self.event(state, 'tests', result.model_dump_json())
                    # Tests can modify their checkout. Reconstruct the validated
                    # artifacts for the next step/publication, never trust those files.
                    verdict, message = self._test_verdict(state, result)
                    self._record_verification(state, result, deferred=verdict == 'defer')
                    if verdict == 'retry':
                        if is_test_path(step.target_file):
                            operation = 'classify_test_failure_scope'
                            assessment = await asyncio.to_thread(
                                self.failure_scope_classifier, state, step, after, result
                            )
                            assessment = TestFailureScope.model_validate(assessment)
                            if assessment.classification != 'ticket_change':
                                return await self._route_scope_failure(
                                    state, step, after, result, assessment
                                )
                            return await self._route_valid_test_failure(state, step, before, after, message)
                        operation = 'reason_about_test_failure'
                        raise ReasoningFailure(message)
                    if verdict == 'escalate':
                        return self.fail(state, message)
                    record = StepResult(step_id=step.step_id, intent=step.intent, target_file=step.target_file,
                        content=after, before_sha256=hashlib.sha256(before.encode()).hexdigest(), report=report, tests=result)
                    if is_test_path(step.target_file) and state.scope_resolution == 'stay_scope':
                        await self._prove_refined_test(state, step, after)
                    self._record_step(state, step, record, after)
                    if is_test_path(step.target_file):
                        state.pending_valid_tests.pop(step.target_file, None)
                        state.test_failure_repair_count = 0
                    await self.event(state, 'step_done', f'{step.step_id}: applied fix; {result.outcome}')
                    return state
                except Exception as exc:
                    classification, reason = _classify_step_exception(operation, exc)
                    state.failed_edit_attempt = None
                    if operation == 'apply_edit' and isinstance(exc, (NoMatch, Ambiguous)):
                        state.failed_edit_attempt = FailedEditAttempt(
                            attempt=attempt + 1, step_id=step.step_id, target_file=step.target_file,
                            candidate_content=proposal.blocks, failure_type=type(exc).__name__,
                            failure_message=str(exc), evidence=exc.evidence,
                            corrective_instruction=(
                                'This exact SEARCH failed. Ground SEARCH exactly in current file_text; '
                                'do not blindly reuse the failed SEARCH.' if isinstance(exc, NoMatch) else
                                'Produce a NEW source-grounded SEARCH with enough surrounding source '
                                'to identify one unique intended region. Do not reuse the identical '
                                'ambiguous SEARCH unchanged. Locations are evidence only; never select '
                                'a match by number or bypass deterministic validation. Combine overlapping blocks.'
                            ),
                        )
                    context = failure_context(
                        exc,
                        classification=classification,
                        component='executor',
                        operation=operation,
                        reason=reason,
                        identifiers={
                            'ticket_id': state.ticket_id,
                            'subtask_id': state.subtask_id,
                            'step_id': step.step_id,
                            'target_file': step.target_file,
                            'repo': state.repo,
                        },
                    )
                    state.failure_contexts = (state.failure_contexts + [context])[-10:]
                    summary = failure_summary(context)
                    state.attempt_history = (state.attempt_history + [summary])[-100:]
                    error = '\n'.join(state.attempt_history)
                    if classification == 'infrastructure':
                        state.retry_count = attempt
                        await self.event(state, 'needs_human', summary)
                        return self.fail(state, summary)
                    evidence = {'message': context.message}
                    instruction = ('Correct the candidate using the approved requirement and current source. '
                                   'Produce a materially different candidate; do not repeat failed fingerprints.')
                    if state.failed_edit_attempt:
                        evidence = state.failed_edit_attempt.evidence.model_dump(mode='json')
                        instruction = state.failed_edit_attempt.corrective_instruction
                    elif isinstance(exc, DuplicateCandidate):
                        evidence['duplicate_of_attempt'] = exc.previous.attempt_number
                        instruction = str(exc)
                    elif is_test_path(step.target_file):
                        instruction = ('Regenerate the TEST to satisfy the approved requirement and '
                                       'validator evidence; do not modify production code to satisfy an invalid test. '
                                       'Produce a materially different candidate.')
                    contents = candidate
                    if test_content is not None:
                        contents += '\nGenerated test content:\n' + test_content
                    contents, truncated = preview(contents)
                    history.append(RetryAttempt(
                        attempt_number=attempt + 1, operation=operation, target=step.target_file,
                        candidate_type=('generated_test' if is_test_path(step.target_file) else
                                        'source_edit' if candidate_hash else 'invalid_output'),
                        candidate_fingerprint=candidate_hash, generated_test_fingerprint=test_hash,
                        candidate_content=contents, candidate_content_truncated=truncated,
                        failure_type=type(exc).__name__, failure_reason=context.message,
                        failure_evidence=evidence, corrective_instruction=instruction,
                    ))
                    state.retry_count = attempt + 1
                    await self.event(state, 'retry' if attempt < settings.max_agent_retries else 'needs_human',
                                     history[-1].model_dump_json())
                    # Restore the original current-step input for every repair.
                    checkout = await asyncio.to_thread(self.repo_tool.prepare_execution, state.repo,
                        state.subtask_id, state.base_commit, state.file_changes)
                    if self.checkpoint_retries and attempt < settings.max_agent_retries:
                        # The graph checkpoints this failed candidate before the next call.
                        return state
                finally:
                    usage_after = self.llm.get_usage(state.ticket_id)
                    for field in ('calls', 'tokens', 'est_cost_usd'):
                        setattr(state.budget_used, field, getattr(state.budget_used, field) +
                                max(0, usage_after[field] - usage_before[field]))
            return self.fail(state, 'Retry limit reached. Attempts: ' + '; '.join(state.attempt_history))
        except Exception as exc:
            context = failure_context(
                exc,
                classification='infrastructure',
                component='executor',
                operation=preparation_operation,
                reason='deterministic_operation_failed',
                identifiers={
                    'ticket_id': state.ticket_id, 'subtask_id': state.subtask_id,
                    'step_id': step.step_id, 'target_file': step.target_file, 'repo': state.repo,
                },
            )
            state.failure_contexts = (state.failure_contexts + [context])[-10:]
            return self.fail(state, failure_summary(context))

    async def _run_delete(self, state: SubtaskState, step, checkout, before: str) -> SubtaskState:
        await asyncio.to_thread(self.repo_tool.delete_execution_file, checkout, step.target_file)
        await self.event(state, 'edit_applied', json.dumps({'deleted': step.target_file}))
        result = await self._run_tests(state, checkout)
        await self.event(state, 'tests', result.model_dump_json())
        self._record_verification(state, result)
        verdict, message = self._test_verdict(state, result)
        if verdict in ('retry', 'escalate'):
            # Deletion is deterministic — retrying reproduces the same result, so
            # a bad outcome escalates immediately instead of looping.
            return self.fail(state, f'Deleting {step.target_file}: {message}')
        record = StepResult(step_id=step.step_id, intent=step.intent, target_file=step.target_file,
            content='', before_sha256=hashlib.sha256(before.encode()).hexdigest(),
            report=EditReport(matches=[]), tests=result)
        self._record_step(state, step, record, None)
        await self.event(state, 'step_done', f'{step.step_id}: deleted {step.target_file}; {result.outcome}')
        return state

    async def _run_tests(self, state: SubtaskState, checkout) -> TestResult:
        predicted = await asyncio.to_thread(self.test_preflight, checkout)
        remember_preflight(state.subtask_id, predicted)
        result = await asyncio.to_thread(self.test_runner, checkout)
        # Preflight is prompt convenience only. Keep its optional hints solely
        # when THIS runtime result proves the suite was UNVERIFIABLE.
        if result.verification_outcome != "UNVERIFIABLE":
            clear_preflight(state.subtask_id)
        return result

    def _test_verdict(self, state: SubtaskState, result: TestResult) -> tuple[str, str | None]:
        """Classify a pytest run per ai_rules.md R-46: exit 5 (no tests collected)
        is never a failure. 'retry' repairs (LLM path) or escalates once
        (delete path); 'escalate' stops immediately; 'defer'/'continue' proceed."""
        outcome = result.outcome
        if result.verification_outcome == 'PASS':
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
            return 'escalate', ('Unverifiable (UNVERIFIABLE): pytest collected no tests (exit 5) and this plan adds none — '
                                 f'flagging rather than assuming the change is safe (R-32). {result.output}'.strip())
        if result.verification_outcome == 'UNVERIFIABLE':
            needed = (f" Supply {', '.join(result.required_credentials)} in the verification prompt."
                      if result.required_credentials else "")
            return 'escalate', f'UNVERIFIABLE: {result.reason}.{needed}'
        hint = ''
        if any(marker in result.output for marker in ('NameError', 'ImportError', 'ModuleNotFoundError')):
            hint = ('Likely a missing/incorrect import — import the real symbol from its actual module. ')
        return 'retry', f'{hint}pytest failed (exit {result.returncode}): {result.output}'

    async def _route_valid_test_failure(self, state: SubtaskState, step, before: str, content: str,
                                        reason: str) -> SubtaskState:
        """Keep a valid failing test and rewind only to production implementation."""
        prior_code = [index for index, item in enumerate(state.plan[:state.current_step])
                      if not is_test_path(item.target_file)]
        if not prior_code:
            return self.fail(state, f'Valid generated test failed but no production step can repair it: {reason}')
        state.test_failure_repair_count += 1
        failure = f'Valid generated test exposed an implementation failure: {reason}'
        state.attempt_history.append(failure)
        if state.test_failure_repair_count > settings.max_agent_retries:
            return self.fail(state, 'Implementation repair retry limit reached. Attempts: '
                             + '; '.join(state.attempt_history))
        rewind = prior_code[-1]
        state.pending_valid_tests[step.target_file] = {
            "content": content, "before": before, "action": step.action,
        }
        state.file_changes[step.target_file] = content
        for item in state.plan[rewind:state.current_step]:
            if not is_test_path(item.target_file):
                state.file_changes.pop(item.target_file, None)
        retained_ids = {item.step_id for item in state.plan[:rewind]}
        state.steps_done = [item for item in state.steps_done if item['step_id'] in retained_ids]
        state.repair_rewind_from = state.current_step
        state.current_step = rewind
        state.execution_complete = False
        state.retry_count = 0
        await self.event(state, 'implementation_repair', failure)
        return state

    async def _route_scope_failure(self, state: SubtaskState, step, content: str, result: TestResult,
                                   assessment: TestFailureScope) -> SubtaskState:
        """Stop before repair when a valid failure is outside scope or uncertain."""
        label = ('Unrelated discovered defect' if assessment.classification == 'unrelated_defect'
                 else 'Could not safely determine test-failure scope')
        state.discovered_defect = {
            **assessment.model_dump(mode='json'),
            'test_path': step.target_file,
            'test_step_index': state.current_step,
            'test_content': content,
            'test_result': {'reason': result.reason, 'facts': result.facts},
            'options': ['stay_scope', 'expand_scope', 'abort'],
        }
        state.status = 'needs_human'
        state.failure_reason = (
            f"{label}: {assessment.failing_behavior}. Suspected cause: "
            f"{assessment.suspected_root_cause or 'insufficient evidence to name one'}. "
            "Choose: (1) stay in scope and refine the test without weakening it; "
            "(2) expand scope and approve a new plan; or (3) abort / escalate."
        )
        state.attempt_history.append(state.failure_reason)
        await self.event(state, 'unrelated_defect', state.failure_reason)
        return state

    async def _prove_refined_test(self, state: SubtaskState, step, content: str) -> None:
        """Prove an option-1 test still detects the original unfixed ticket bug."""
        baseline = await asyncio.to_thread(
            self.repo_tool.prepare_execution, state.repo, state.subtask_id,
            state.base_commit, {step.target_file: content},
        )
        result = await self._run_tests(state, baseline)
        await self.event(state, 'test_refinement_baseline', result.model_dump_json())
        if result.verification_outcome != 'FAIL':
            raise ValueError(
                'Refined test was weakened: it must fail against the original unfixed code '
                f'but baseline verification was {result.verification_outcome}'
            )
        if state.discovered_defect is not None:
            state.discovered_defect['resolution'] = 'stay_scope'
            state.discovered_defect['baseline_verification'] = result.model_dump(mode='json')
        state.scope_resolution = None

    @staticmethod
    def _record_verification(state: SubtaskState, result: TestResult, *, deferred: bool = False) -> None:
        state.verification_summary = {
            "outcome": result.verification_outcome,
            "reason": result.reason,
            "facts": result.facts,
        }
        state.required_test_credentials = result.required_credentials
        if result.verification_outcome == "PASS":
            state.verifiability = "verified"
        elif result.verification_outcome == "UNVERIFIABLE" and not deferred:
            state.verifiability = "no_tests" if result.outcome == "no_tests_collected" else "unverifiable"

    def _test_grounding(self, state: SubtaskState, checkout, step) -> tuple[
        dict[str, str], dict[str, str] | None, dict[str, dict[str, Any]]
    ]:
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
        if not related_files:
            raise UnresolvedTestInterfaceError(
                "the plan and diagnosis identify no readable Python code-under-test file"
            )
        interfaces = resolve_test_interfaces(
            list(related_files),
            lambda symbol: self.code_search.find_symbol(
                state.repo, state.subtask_id, symbol, limit=20
            ),
            lambda path: self.code_search.get_file(
                state.repo, state.subtask_id, path, start_line=1, end_line=100_000
            ),
        )
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
        return related_files, style_example, interfaces

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


class ReasoningFailure(ValueError):
    """A failed candidate that another bounded model attempt may correct."""


_REASONING_EXCEPTIONS: dict[str, tuple[type[BaseException], ...]] = {
    'check_duplicate_candidate': (DuplicateCandidate,),
    'llm.complete_json': (ValidationError, ValueError),
    'validate_model_output': (ValidationError,),
    'validate_create_output': (ValueError,),
    'validate_edit_output': (ValueError,),
    'apply_edit': (NoMatch, Ambiguous, ValidationError),
    'validate_test_interface': (TestInterfaceValidationError,),
    'validate_edit': (GuardFailure,),
    'validate_test_semantics': (InvalidGeneratedTestError, RequirementContradictionError),
    'repair_test_imports': (TestImportValidationError,),
    'classify_test_failure_scope': (ValidationError, ValueError),
    'reason_about_test_failure': (ReasoningFailure,),
}


def _classify_step_exception(operation: str, exc: BaseException) -> tuple[str, str]:
    """Classify by the operation contract, never broad exception-name guessing."""
    if isinstance(exc, UnresolvedTestInterfaceError):
        return 'infrastructure', 'interface_could_not_be_resolved'
    expected = _REASONING_EXCEPTIONS.get(operation, ())
    if expected and isinstance(exc, expected):
        return 'reasoning', 'candidate_output_can_be_corrected'
    return 'infrastructure', 'deterministic_operation_failed'


def _record_interface_uncertainties(
    state: SubtaskState, result, source: str,
) -> list[UnresolvedCheck]:
    """Translate interface-validator skips into the shared R-32d contract."""
    records: list[UnresolvedCheck] = []
    covered: set[str] = set()
    for skipped in result.skipped_checks:
        owner, _, attribute = skipped.partition('.')
        attribute = attribute.removesuffix('()') or '*'
        covered.add(owner)
        records.append(UnresolvedCheck(
            owner=owner or 'unknown',
            attribute=attribute,
            reason=f"Interface manifest entry for {owner or 'the return type'} could not be resolved",
            source=source,
            impact=f"Use of {skipped} in the generated test was not statically verified",
        ))
    for owner in result.unresolved_types:
        if owner not in covered:
            records.append(UnresolvedCheck(
                owner=owner,
                attribute='*',
                reason=f"Return type {owner} could not be resolved from repository source",
                source=source,
                impact="The returned object's interface was not statically verified",
            ))
    existing = {item.model_dump_json() for item in state.unresolved_checks}
    state.unresolved_checks.extend(
        item for item in records if item.model_dump_json() not in existing
    )
    return records


_SYSTEM = """Apply only this approved step to the supplied file. The step's action
is authoritative and was checked against the real repository immediately before
this call:
- For action="create", return the complete new file in full_content. Return no
  SEARCH/REPLACE blocks because the target does not exist.
- For action="edit", return surgical SEARCH/REPLACE blocks quoting existing code.
  Return no full_content and do not rewrite the whole file.
Never return shell commands.
Use repair_feedback to correct a failed match, syntax error or test failure.
retry_attempts contains ALL failed candidates in this step's current retry cycle.
Read every failure, its deterministic evidence, and corrective_instruction. Never
repeat a failed candidate fingerprint: duplicates are rejected without execution
and consume the same bounded retry budget. Produce a materially different candidate.
Within execution_constraints, ticket_requirement and human_approval_note records are
approved intent. critic_correction and prior_attempt records are advisory until explicitly
captured by human approval. constraint_resolutions records explicit human decisions;
never restore withdrawn intent from historical descriptions or diagnostic context.
Never choose precedence between conflicting authoritative requirements; a human must decide.
Code and tests must not contradict them. Every retry must continue to respect them.
Obey these constraints and the approved step alongside
the current file_text and existing diagnosis. Failure feedback cannot override them.
When failed_edit_attempt is present, inspect its exact candidate and deterministic
evidence and follow its corrective_instruction. Only new SEARCH/REPLACE blocks
are accepted; source locations are diagnostic evidence, never edit selectors.
Do not change the step's scope or invent requirements. If you cannot do this safely,
return no blocks and a specific unable_reason. Otherwise unable_reason is null.

When target_file is a test file: `related_files` holds the REAL, current content of
the file(s) this test covers — import and call the ACTUAL functions/classes/names
exactly as spelled and defined there, never a name recalled only from the
diagnosis/description (that causes NameError). Write one correct import statement
for the real module the code lives in. Before writing the test body, identify every
code-under-test symbol you will use and its exact import from `related_files`; include
those imports in `full_content`. If `style_example` is present, it's an
existing test file from this same repo — match its framework (plain pytest
functions vs unittest.TestCase, fixtures used, naming) instead of inventing a
different style; if absent, use plain pytest functions. Write a complete,
self-contained, importable test with meaningful assertions covering BOTH the
previously-broken case (from diagnosis/description) and a normal case that should
still pass — a test that can't fail is not proof of the fix.
`symbol_interfaces` is the deterministic, source-verified contract. Use ONLY its
constructor/function arguments, methods, returned object types, and attributes. Never
invent an argument, method, or attribute. If the contract cannot express an assertion
the step needs, return a specific unable_reason instead of guessing."""
