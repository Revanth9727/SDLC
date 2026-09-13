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
        if state.retry_count >= settings.max_agent_retries:
            state.status, state.failure_reason = 'needs_human', f'Retry limit reached: {state.guard_error}'
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
        if node == 'planner':
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
            if state.current_step != previous.current_step + 1 or state.current_step > len(state.plan):
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
