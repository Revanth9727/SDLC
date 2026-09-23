/* Existing escalation endpoint; every change is an explicit human decision. */
window.constraintConflictPanel = function(ticket, subtask, state) {
  const panel = document.createElement('section');
  panel.className = 'card constraint-conflict-panel';
  const add = (tag, text, parent = panel) => {
    const element = document.createElement(tag); element.textContent = text;
    parent.appendChild(element); return element;
  };
  add('h3', 'Resolve conflicting requirements');
  add('p', 'Execution is blocked. Choose what to keep, withdraw, or replace. No requirement takes automatic precedence.');
  const choices = new Map();
  for (const conflict of (state.constraint_conflicts || []).filter(c => c.status === 'active')) {
    add('p', conflict.reason);
    add('pre', JSON.stringify(conflict.affected_targets, null, 2));
    conflict.constraints.forEach((constraint, i) => {
      const id = conflict.constraint_ids[i];
      if (choices.has(id)) return;
      const block = add('fieldset', '');
      add('legend', constraint.text, block);
      add('p', `Source: ${constraint.source} · ${constraint.provenance}`, block);
      add('p', `Scope: ${constraint.scope_type} = ${constraint.scope_value}`, block);
      const behaviors = conflict.normalized_behaviors.map(b => `${b.subject}: ${b.outcome}`);
      add('p', `Incompatible behavior: ${behaviors.join(' versus ')}`, block);
      const label = add('label', 'Decision ', block);
      const select = add('select', '', label);
      for (const [value, text] of [['keep', 'Keep'], ['withdraw', 'Withdraw'], ['replace', 'Replace / clarify']]) {
        const option = add('option', text, select); option.value = value;
      }
      const replacementLabel = add('label', 'Replacement requirement ', block);
      const replacement = add('textarea', '', replacementLabel);
      replacement.maxLength = 4000; replacementLabel.hidden = true;
      select.addEventListener('change', () => replacementLabel.hidden = select.value !== 'replace');
      choices.set(id, {select, replacement});
    });
  }
  const noteLabel = add('label', 'Explain the intended behavior ');
  const note = add('textarea', '', noteLabel); note.maxLength = 4000;
  const replanLabel = add('label', '');
  const replan = add('input', '', replanLabel); replan.type = 'checkbox';
  replanLabel.appendChild(document.createTextNode(' Re-plan after resolving, before approval'));
  const submit = add('button', 'Apply resolution and review plan'); submit.type = 'button';
  submit.className = 'primary-button';
  const status = add('p', ''); status.setAttribute('role', 'status');
  submit.addEventListener('click', async () => {
    const changes = [];
    for (const [constraint_id, choice] of choices) {
      if (choice.select.value === 'keep') continue;
      const replacement_text = choice.select.value === 'replace' ? choice.replacement.value.trim() : null;
      if (replacement_text === '') { status.textContent = 'Enter replacement text or choose Withdraw.'; return; }
      changes.push({constraint_id, replacement_text});
    }
    if (!changes.length || !note.value.trim()) {
      status.textContent = 'Choose at least one withdrawal or replacement and explain the intended behavior.'; return;
    }
    submit.disabled = true;
    try {
      const response = await fetch(`/tickets/${ticket}/subtasks/${subtask}/escalation/resolve_constraints`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
          escalation_id: state.escalation_id, constraint_changes: changes, note: note.value, replan: replan.checked,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : 'Resolution could not be applied.');
      location.assign(`/tickets/${ticket}`);
    } catch (error) { status.textContent = error.message; submit.disabled = false; }
  });
  return panel;
};
