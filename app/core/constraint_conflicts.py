"""Deterministic intent decisions. Never choose precedence or ask a model to do so."""
from itertools import combinations
import re

from app.agents.constraints import (
    ConstraintBehavior, ConstraintChange, ConstraintConflict, ConstraintRelationship,
    ConstraintResolution, ExecutionConstraint, AUTHORITATIVE_SOURCES,
)
from app.agents.state import SubtaskState
from app.core.execution_constraints import append_constraints, matches
from app.tools.test_validity_validator import _requirement_expected_outcome


# Same explicit subjects as R-32 scenario association. No arbitrary-text similarity.
_SUBJECTS = {
    'empty string': 'empty_string', 'empty strings': 'empty_string',
    'blank string': 'blank_string', 'blank strings': 'blank_string',
    'empty/blank value': 'blank_string', 'empty/blank values': 'blank_string',
    'empty or blank value': 'blank_string', 'empty or blank values': 'blank_string',
    'empty forbidden phrase': 'empty_forbidden_phrase',
    'empty forbidden phrases': 'empty_forbidden_phrase',
}


def normalized_behaviors(text: str) -> list[ConstraintBehavior]:
    """Only complete supported declarations, not phrases embedded in arbitrary prose.

    Reuse R-32 PASS/FAIL normalization. 'must PASS/FAIL' is accepted only as an
    explicit declaration of those existing normalized values for an exact subject.
    """
    from app.tools.test_validity_validator import _PASS_REQUIREMENT_TOKENS, _FAIL_REQUIREMENT_TOKENS
    behaviors = []
    for clause in re.split(r'[.\n;]+', text.lower()):
        for subject, key in _SUBJECTS.items():
            prefix = subject + ' '
            if not clause.strip().startswith(prefix):
                continue
            predicate = clause.strip()[len(prefix):]
            if predicate in {'must pass', 'must fail'}:
                outcome = predicate.split()[1].upper()
            elif predicate in (*_PASS_REQUIREMENT_TOKENS, *_FAIL_REQUIREMENT_TOKENS):
                outcome = _requirement_expected_outcome(predicate)
            else:
                continue
            behavior = ConstraintBehavior(subject=key, outcome=outcome)
            if behavior not in behaviors:
                behaviors.append(behavior)
    return behaviors


def _overlap(state, left, right):
    if not matches(state, left, None) or not matches(state, right, None):
        return False, [], 'Ticket/subtask identity does not match this isolated work state.'
    if left.scope_type == right.scope_type and left.scope_value != right.scope_value:
        return False, [], 'Different explicit values of the same scope type.'
    targets = [dict(ticket_id=state.ticket_id, subtask_id=state.subtask_id,
                    step_id=step.step_id, file=step.target_file, symbols=step.target_symbols)
               for step in state.plan if matches(state, left, step) and matches(state, right, step)]
    if targets:
        return True, targets, 'Both explicit scopes match the listed approved plan target(s).'
    if state.plan:
        return False, [], 'No approved plan target matches both explicit scopes.'
    if left.scope_type == right.scope_type or (
        left.scope_type in {'ticket', 'subtask'} or right.scope_type in {'ticket', 'subtask'}
    ):
        return True, [dict(ticket_id=state.ticket_id, subtask_id=state.subtask_id)], (
            'Identical explicit scope, or ticket/subtask scope contains the other scope in this work state.'
        )
    # No semantic inference about which file contains a symbol or what a step touches.
    return None, [], 'No explicit plan target establishes overlap between these different scope types.'


def check_constraint_conflicts(state: SubtaskState) -> bool:
    """Refresh durable evidence; return True only for proven active conflicts."""
    records = []
    for item in state.execution_constraints:
        if item.source in AUTHORITATIVE_SOURCES and item.constraint_id not in state.withdrawn_constraint_ids:
            append_constraints(records, [item])
    state.constraint_relationships = []
    active = []
    # Include a record paired with itself to catch opposing explicit clauses in one note.
    pairs = list(combinations(records, 2)) + [(item, item) for item in records]
    for left, right in pairs:
        overlap, targets, reason = _overlap(state, left, right)
        ids = [left.constraint_id, right.constraint_id]
        lb, rb = normalized_behaviors(left.text), normalized_behaviors(right.text)
        incompatible = [(a, b) for a in lb for b in rb if a.subject == b.subject and a.outcome != b.outcome]
        if left == right and not incompatible:
            continue
        if overlap is False:
            status = 'non_overlapping'
        elif overlap is None:
            status = 'unknown'
        elif incompatible:
            status = 'conflict'
            for a, b in incompatible:
                if left == right and a.outcome == 'FAIL':
                    continue  # One stable pair for contradictory clauses in the same record.
                active.append(ConstraintConflict(
                    constraint_ids=ids, constraints=[left, right], normalized_behaviors=[a, b],
                    reason=reason + f' {a.subject}: {a.outcome} and {b.outcome} are incompatible; no precedence is defined.',
                    affected_targets=targets,
                ))
        elif (left.text, left.scope_type, left.scope_value) == (right.text, right.scope_type, right.scope_value):
            status = 'duplicate'
        elif lb and rb and lb == rb:
            status = 'compatible'
        else:
            status = 'unknown'
            reason += ' No deterministically incompatible normalized behavior was established.'
        state.constraint_relationships.append(ConstraintRelationship(constraint_ids=ids, status=status, reason=reason))
    history = [item for item in state.constraint_conflicts if item.status == 'resolved']
    state.constraint_conflicts = history + active
    if active:
        state.status = 'needs_human'
        state.guard_error, state.guard_retry = None, False
        state.failure_reason = 'Conflicting authoritative execution constraints require a human decision. ' + ' '.join(
            f'{item.constraints[0].text!r} ({item.constraints[0].provenance}) versus '
            f'{item.constraints[1].text!r} ({item.constraints[1].provenance}). {item.reason}' for item in active
        )
    return bool(active)


def resolve_constraint_conflicts(state: SubtaskState, changes: list[ConstraintChange], note: str, provenance: str) -> None:
    """Atomic explicit human decision; caller authenticates/locks the existing interrupt."""
    trial = state.model_copy(deep=True)
    if not check_constraint_conflicts(trial):
        raise ValueError('No active authoritative constraint conflict remains; reload before deciding')
    if not changes or not note.strip():
        raise ValueError('Select constraints to withdraw or replace and explain the intended behavior')
    ids = [change.constraint_id for change in changes]
    if len(set(ids)) != len(ids):
        raise ValueError('Each constraint may be changed only once per decision')
    affected_ids = {identifier for item in trial.constraint_conflicts if item.status == 'active' for identifier in item.constraint_ids}
    if not set(ids) <= affected_ids:
        raise ValueError('Choose only active conflicting constraints from this checkpoint')
    originals = {item.constraint_id: item for item in trial.execution_constraints}
    previous = [item.model_copy(deep=True) for item in trial.constraint_conflicts if item.status == 'active']
    for change in changes:
        trial.withdrawn_constraint_ids.append(change.constraint_id)
        original = originals[change.constraint_id]
        if change.replacement_text is not None:
            append_constraints(trial.execution_constraints, [ExecutionConstraint(
                source='human_approval_note', text=change.replacement_text,
                scope_type=original.scope_type, scope_value=original.scope_value,
                provenance=f'{provenance}:replaces:{change.constraint_id}',
            )])
    if check_constraint_conflicts(trial):
        raise ValueError('The proposed resolution still contains authoritative conflicts; no changes were applied')
    for item in previous:
        item.status = 'resolved'
        item.resolution_provenance = provenance
    trial.constraint_conflicts.extend(previous)
    trial.constraint_resolutions.append(ConstraintResolution(changes=changes, note=note.strip(), provenance=provenance))
    for field in ('execution_constraints', 'withdrawn_constraint_ids', 'constraint_conflicts',
                  'constraint_relationships', 'constraint_resolutions'):
        setattr(state, field, getattr(trial, field))
