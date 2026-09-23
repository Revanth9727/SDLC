"""Approval-boundary capture and deterministic R-59 scope selection.

No comment history, semantic inference, or error strings are promoted here.
"""
from app.agents.constraints import ExecutionConstraint, AUTHORITATIVE_SOURCES
from app.agents.planning import ApprovalDecision, Step
from app.agents.state import SubtaskState


def append_constraints(target: list[ExecutionConstraint], items: list[ExecutionConstraint]) -> None:
    for item in items:
        if item not in target:
            target.append(item.model_copy(deep=True))


def record_decision(state: SubtaskState, decision: ApprovalDecision) -> None:
    """Called only after the existing human gate validates a decision."""
    ticket_gate = bool(state.approval_payload and "subtasks" in state.approval_payload)
    scope_type = "ticket" if ticket_gate else "subtask"
    scope_value = state.ticket_id if ticket_gate else state.subtask_id
    if decision.note:
        append_constraints(state.pending_execution_constraints, [ExecutionConstraint(
            source="human_approval_note", text=decision.note,
            scope_type=scope_type, scope_value=scope_value,
            provenance=decision.provenance or (
                f"approval:{state.subtask_id}:{'intent' if ticket_gate else 'plan'}:"
                f"{state.replan_count}:{len(state.constraint_resolutions)}:{decision.approval_status}"
            ),
        )])
    if decision.approval_status == "approved":
        ensure_requirement(state, ticket_gate=ticket_gate)
        append_constraints(state.execution_constraints, state.pending_execution_constraints)
        state.pending_execution_constraints = []
        from app.core.constraint_conflicts import check_constraint_conflicts
        check_constraint_conflicts(state)


def ensure_requirement(state: SubtaskState, *, ticket_gate: bool = False) -> None:
    """Also supports approved checkpoints created before this field existed."""
    text = state.ticket_requirement if ticket_gate else state.description
    if not text:
        text = state.description
    append_constraints(state.execution_constraints, [ExecutionConstraint(
        source="ticket_requirement", text=text,
        scope_type="ticket" if ticket_gate else "subtask",
        scope_value=state.ticket_id if ticket_gate else state.subtask_id,
        provenance=f"approved {'ticket' if ticket_gate else 'subtask'}:{state.ticket_id if ticket_gate else state.subtask_id}",
    )])


def matches(state: SubtaskState, constraint: ExecutionConstraint, step: Step | None) -> bool:
    if constraint.scope_type == "ticket":
        return constraint.scope_value == state.ticket_id
    if constraint.scope_type == "subtask":
        return constraint.scope_value == state.subtask_id
    if step is None:
        # Planning must retain explicit targets even when the new plan does not
        # exist yet. These records live only in this isolated work state.
        return True
    if constraint.scope_type == "step":
        return constraint.scope_value == step.step_id
    if constraint.scope_type == "file":
        return constraint.scope_value == step.target_file
    return constraint.scope_value in step.target_symbols


def scoped_constraints(state: SubtaskState, step: Step | None = None, *, pending: bool = False) -> list[dict]:
    records: list[ExecutionConstraint] = []
    append_constraints(records, state.execution_constraints)
    if pending:
        append_constraints(records, state.pending_execution_constraints)
    return [item.model_dump(mode="json") for item in records if item.constraint_id not in state.withdrawn_constraint_ids and matches(state, item, step)]


def inherited_ticket_constraints(state: SubtaskState) -> list[ExecutionConstraint]:
    """Only ticket intent crosses into a newly decomposed work identity."""
    return [item.model_copy(deep=True) for item in state.execution_constraints
            if item.scope_type == "ticket" and item.scope_value == state.ticket_id
            and item.constraint_id not in state.withdrawn_constraint_ids]


def constraint_description(state: SubtaskState) -> str:
    """Do not re-deliver explicitly withdrawn intent as an authoritative description.

    Keep the original checkpoint text for audit. Exact record identity/text only;
    no rewriting or semantic removal of fragments from arbitrary descriptions.
    """
    matching = [item for item in state.execution_constraints
                if item.source in AUTHORITATIVE_SOURCES
                and item.text == state.description and matches(state, item, None)]
    if matching and all(item.constraint_id in state.withdrawn_constraint_ids for item in matching):
        return ('The previous description was explicitly withdrawn by the human. '
                'Use the active scoped execution_constraints, recorded constraint_resolutions, '
                'and the approved plan.')
    return state.description
