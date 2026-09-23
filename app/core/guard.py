"""Deterministic output, retry, budget, and progress contracts."""
from pydantic import ValidationError
from app.agents.state import SubtaskState, BudgetUsed
from app.agents.critic import CriticVerdict
from app.agents.code_intelligence import CodeContext
from app.core.code_impact import SymbolImpact
from app.agents.diagnosis import Diagnosis
from app.agents.planner import DecompositionResult
from app.agents.planning import Plan
from app.config import settings
from app.core.budget import usage, budget_reason


def check(state: SubtaskState) -> SubtaskState:
    state = SubtaskState.model_validate(state.model_dump())
    try:
        saved = usage(state.ticket_id)
    except Exception:
        state.status, state.failure_reason = 'needs_human', 'Cannot read durable ticket budget; no further spending is allowed'
        return state
    if saved:
        state.budget_used = BudgetUsed.model_validate(saved)
    reason = budget_reason(state.budget_used.model_dump(), saved.get('limits') if saved else None)
    state.guard_retry = False
    if reason and state.guard_node != 'publish':
        state.status, state.failure_reason = 'needs_human', reason
    elif state.status == 'needs_human':
        state.failure_reason = state.failure_reason or 'Agent could not safely continue'
    elif state.guard_error:
        if not state.attempt_history or state.attempt_history[-1] != state.guard_error:
            state.attempt_history.append(state.guard_error)
        if state.retry_count >= settings.max_agent_retries:
            history = '; '.join(state.attempt_history)
            state.status, state.failure_reason = 'needs_human', f'Retry limit reached. Attempts: {history}'
        else:
            state.retry_count += 1
            state.guard_retry = True
    elif state.retry_count > settings.max_agent_retries:
        state.status, state.failure_reason = 'needs_human', 'Retry limit reached'
    return state


def validate_output(previous: SubtaskState, output, node: str) -> SubtaskState:
    state = SubtaskState.model_validate(output.model_dump() if isinstance(output, SubtaskState) else output)
    # 'repo' is protected everywhere EXCEPT the Planner, whose job is exactly to
    # assign it (R-26) — every later node must then hold it fixed.
    protected = ('ticket_id', 'subtask_id', 'jira_key') if node == 'planner' else \
        ('ticket_id', 'subtask_id', 'repo', 'jira_key')
    for field in protected:
        if getattr(state, field) != getattr(previous, field):
            raise ValueError(f'Agent changed protected identity: {field}')
    if state.status == 'running':
        if node == 'repo_overview':
            if not state.repo_overview or {item.get('repo') for item in state.repo_overview} != set(state.confirmed_repos):
                raise ValueError('Repository overview does not cover the confirmed repos')
            if any(not isinstance(item.get('files'), list) or not isinstance(item.get('directories'), list)
                   for item in state.repo_overview):
                raise ValueError('Repository overview has an invalid inventory shape')
        elif node == 'planner':
            DecompositionResult.model_validate({'subtasks': state.subtask_specs, 'reasoning': state.decomposition_reasoning})
            if state.repo not in state.confirmed_repos:
                raise ValueError('Planner assigned a repo outside the confirmed list')
        elif node == 'code_intelligence':
            context = CodeContext.model_validate(state.code_context)
            if not context.verified_evidence:
                state.status = 'needs_human'
                state.failure_reason = 'Code-Intelligence could not verify any source evidence'
            elif any(not item.get('verified') for item in context.verified_evidence):
                raise ValueError('Code-Intelligence returned unverified evidence')
        elif node == 'diagnosis':
            diagnosis = Diagnosis.model_validate(state.diagnosis)
            if diagnosis.no_root_cause or not diagnosis.root_cause.strip():
                state.status, state.failure_reason = 'needs_human', f'Diagnosis could not determine a root cause: {diagnosis.reasoning}'
        elif node == 'step_planner':
            Plan.model_validate(state.plan)
        elif node == 'execute':
            for impact in state.code_impacts:
                SymbolImpact.model_validate(impact)
            if state.repair_rewind_from is not None:
                _validate_repair_rewind(previous, state)
                state.repair_rewind_from = None
            elif state.current_step == previous.current_step:
                _validate_retry_checkpoint(previous, state)
            elif state.current_step != previous.current_step + 1 or state.current_step > len(state.plan):
                raise ValueError('Executor made no valid forward progress')
            if state.execution_complete != (state.current_step == len(state.plan)):
                raise ValueError('Executor completion does not match plan progress')
        elif node == 'critic':
            CriticVerdict.model_validate(state.critic_verdict)
            for impact in state.code_impacts:
                SymbolImpact.model_validate(impact)
    state.retry_count = 0
    state.guard_node, state.guard_error = node, None
    return state


def _validate_retry_checkpoint(previous: SubtaskState, state: SubtaskState) -> None:
    """Only one bounded reasoning failure may yield without execution progress."""
    if not 0 <= state.current_step < len(state.plan) or state.plan != previous.plan:
        raise ValueError('Executor made no valid forward progress: retry checkpoint has no unchanged active step')
    step_id = state.plan[state.current_step].step_id
    prior = previous.retry_attempts.get(step_id, [])
    attempts = state.retry_attempts.get(step_id, [])
    if (len(attempts) != len(prior) + 1 or attempts[:-1] != prior
            or len(attempts) > settings.max_agent_retries
            or attempts[-1].attempt_number != len(attempts)
            or state.retry_count != len(attempts)):
        raise ValueError('Executor made no valid forward progress or bounded retry progress')
    if state.file_changes != previous.file_changes or state.steps_done != previous.steps_done:
        raise ValueError('Executor retry checkpoint changed execution artifacts')
    if not state.failure_contexts or state.failure_contexts[-1].classification != 'reasoning':
        raise ValueError('Executor retry checkpoint lacks a reasoning failure')


def _validate_repair_rewind(previous: SubtaskState, state: SubtaskState) -> None:
    """Accept only Executor's bounded valid-test-to-implementation rewind."""
    if state.repair_rewind_from != previous.current_step:
        raise ValueError('Executor repair rewind does not match the failing test step')
    if not 0 <= state.current_step < previous.current_step:
        raise ValueError('Executor repair rewind did not move to an earlier plan step')
    if state.execution_complete:
        raise ValueError('Executor repair rewind cannot be marked complete')
    if not state.pending_valid_tests:
        raise ValueError('Executor repair rewind did not retain a validated failing test')
    if state.test_failure_repair_count != previous.test_failure_repair_count + 1:
        raise ValueError('Executor repair rewind did not consume exactly one repair attempt')
    if state.test_failure_repair_count > settings.max_agent_retries:
        raise ValueError('Executor repair rewind exceeded the retry cap')
    if len(state.attempt_history) <= len(previous.attempt_history):
        raise ValueError('Executor repair rewind omitted the failure that caused it')
