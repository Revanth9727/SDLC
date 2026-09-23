"""R-59: approved feedback reaches bounded execution through durable state."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
import json
import uuid

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, StateGraph

from app.agents.constraints import ExecutionConstraint
from app.agents.comment_monitor import CommentIntent
from app.agents.executor import ExecutorAgent
from app.agents.critic import CriticAgent
from app.agents.planner import PlannerAgent
from app.agents.planning import ApprovalDecision, Step
from app.agents.state import SubtaskState
from app.agents.step_planner import StepPlannerAgent
from app.config import settings
from app.core import comment_handling, proposals
from app.core.execution_constraints import record_decision, scoped_constraints
from app.orchestrator import graph as module
from app.orchestrator.scheduler import materialize_subtasks
from tests.phase04.test_approval import setup
from tests.phase05.test_execution import local, local_with_style_example, local_with_scenario_check, no_event
from tests.phase05.test_retry_evidence import Candidates, BAD, GOOD, edit, passing
from tests.phase05.test_webhooks import Jira
from tests.phase08.test_multi_subtasks import coordinator


CORRECTION = "Do not treat empty strings as forbidden phrases."


def constraint(state, text=CORRECTION, scope_type="subtask", scope_value=None):
    return ExecutionConstraint(source="human_approval_note", text=text,
        scope_type=scope_type, scope_value=scope_value or state.subtask_id,
        provenance="jira:TEST-1:comment:42:gate:plan-1")


@pytest.mark.parametrize("scope,value,expected", [
    ("ticket", "ticket", True), ("ticket", "another-ticket", False),
    ("subtask", "task", True), ("subtask", "another-task", False),
    ("step", "1", True), ("step", "2", False),
    ("file", "app.py", True), ("file", "README.md", False),
    ("symbol", "check", True), ("symbol", "unrelated", False),
])
def test_exact_scope_matching(scope, value, expected):
    state = SubtaskState(ticket_id="ticket", subtask_id="task", subtask_type="bug",
                         repo="org/repo", description="Fix empty phrase behavior")
    step = Step(step_id="1", intent="Fix check", target_file="app.py", target_symbols=["check"])
    state.execution_constraints = [constraint(state, scope_type=scope, scope_value=value)]
    assert bool(scoped_constraints(state, step)) is expected


def test_rejection_is_pending_until_revised_plan_approved_and_capture_is_idempotent(local):
    _, state, _ = local
    decision = ApprovalDecision(approval_status="rejected", note=CORRECTION,
                                provenance="jira:TEST-1:comment:42")
    record_decision(state, decision)
    record_decision(state, decision)
    assert scoped_constraints(state, state.plan[0]) == []
    assert len(state.pending_execution_constraints) == 1
    state.approval_note = ""  # prepare_approval clears this legacy field
    state = SubtaskState.model_validate_json(state.model_dump_json())
    record_decision(state, ApprovalDecision(approval_status="approved"))
    assert state.pending_execution_constraints == []
    assert [item["text"] for item in scoped_constraints(state)] == [state.description, CORRECTION]
    assert state.execution_constraints[-1].provenance == "jira:TEST-1:comment:42"


@pytest.mark.asyncio
async def test_explicit_constraints_on_every_bounded_attempt_without_history_dependency(local, tmp_path):
    tool, state, git = local
    before = '    return not any(phrase in text for phrase in forbidden)'
    after = '    return not any(phrase in text for phrase in forbidden if phrase)'
    (tool.remote / 'app.py').write_text('def check(text, forbidden):\n' + before + '\n')
    git('add', 'app.py')
    git('commit', '-m', 'empty phrase fixture')
    state.base_commit = git('rev-parse', 'HEAD')
    state.description = 'Empty forbidden phrases should be ignored.'
    state.plan[0].intent = 'Ignore empty forbidden phrases'
    record_decision(state, ApprovalDecision(approval_status="approved", note=CORRECTION))
    state.execution_constraints.append(constraint(state, "Update README formatting later.", "file", "README.md"))
    state.execution_constraints.append(constraint(state, "Follow the explicit prior correction.", "step", "1").model_copy(
        update={"source": "prior_attempt"}))
    llm = Candidates([BAD, edit("another missing search"), edit(before, after)])
    result = await ExecutorAgent(llm, tool, test_runner=passing, emit=no_event).run(state)
    assert result.execution_complete, result.failure_reason
    assert len(llm.prompts) == 3
    for payload in llm.prompts:
        received = payload["execution_constraints"]
        assert CORRECTION in [item["text"] for item in received]
        assert state.description in [item["text"] for item in received]
        assert all(item["scope_value"] != "README.md" for item in received)
        assert any(item["source"] == "prior_attempt" for item in received)
        assert CORRECTION not in payload["repair_feedback"]
    assert llm.prompts[0]["retry_attempts"] == []
    assert llm.prompts[2]["execution_constraints"] == llm.prompts[0]["execution_constraints"]
    (tmp_path / "executor_payload.json").write_text(json.dumps(llm.prompts[0], indent=2))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_restart_retains_constraint_and_retry_budget(local, backend):
    tool, state, _ = local
    record_decision(state, ApprovalDecision(approval_status="approved", note=CORRECTION))
    memory = MemorySaver()

    @asynccontextmanager
    async def connection():
        if backend == "memory":
            yield memory
        else:
            async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
                await saver.setup()
                yield saver

    def build(llm, saver):
        agent = ExecutorAgent(llm, tool, test_runner=passing, emit=no_event, checkpoint_retries=True)
        async def execute(raw):
            return (await agent.run(SubtaskState.model_validate(raw))).model_dump(mode="json")
        graph = StateGraph(dict)
        graph.add_node("execute", execute)
        graph.add_node("between", lambda raw: raw)
        graph.set_entry_point("execute")
        graph.add_conditional_edges("execute", lambda raw: END if raw["execution_complete"] else "between")
        graph.add_edge("between", "execute")
        return graph.compile(checkpointer=saver, interrupt_before=["between"])

    config = {"configurable": {"thread_id": state.subtask_id}}
    first = Candidates([BAD])
    async with connection() as saver:
        await build(first, saver).ainvoke(state.model_dump(mode="json"), config)
    second = Candidates([GOOD])
    async with connection() as saver:
        try:
            graph = build(second, saver)
            await graph.ainvoke(None, config)
            final = SubtaskState.model_validate((await graph.aget_state(config)).values)
            assert final.execution_complete
            assert second.prompts[0]["execution_constraints"] == first.prompts[0]["execution_constraints"]
            assert len(second.prompts[0]["retry_attempts"]) == 1
        finally:
            if backend == "postgres":
                await saver.adelete_thread(state.subtask_id)


@pytest.mark.asyncio
async def test_jira_revision_gate_restart_stepplanner_and_executor(setup, local, monkeypatch):
    """Real routing, gate, StepPlanner, Executor; fake only model/world services."""
    tool, state, _ = local
    state.approval_status = "pending"
    state.confirmed_repos = [state.repo]
    _, diagnosis, _, decomposer, _, _ = setup
    jira = Jira()
    plan_llm = Candidates([{"plan": [state.plan[0].model_dump()], "reasoning": "Apply correction"}])
    executor_llm = Candidates([BAD, GOOD])
    saver = MemorySaver()
    def build():
        return module.build_graph(agent=diagnosis, decomposer=decomposer,
            planner=StepPlannerAgent(plan_llm, tool),
            executor=ExecutorAgent(executor_llm, tool, test_runner=passing, emit=no_event),
            jira=jira, checkpointer=saver, memory_search=lambda *a, **k: [],
            activity_check=lambda s: None, repo_tool=tool)
    graph = build()
    config = module.thread_config(state.ticket_id, state.subtask_id)
    await graph.ainvoke(state.model_dump(mode="json"), config)
    await module.resume_approval(graph, state.ticket_id, state.subtask_id, ApprovalDecision(approval_status="approved"))
    gate_id = "test-gate"
    monkeypatch.setattr(comment_handling, "context_for", lambda key: {"ticket_id": state.ticket_id})
    monkeypatch.setattr(comment_handling, "pending_for_ticket", lambda key: [{"id": gate_id}])
    async def decide(gate, decision):
        return await module.resume_approval(graph, state.ticket_id, state.subtask_id, decision)
    monkeypatch.setattr(comment_handling, "decide_registered", decide)
    async def comment(comment_id, text, intent):
        return await comment_handling.handle_comment("TEST-1", {
            "id": comment_id, "author": {"accountId": "owner"}, "body": text,
        }, jira, SimpleNamespace(run=lambda *args: intent))
    await comment("42", CORRECTION, CommentIntent(intent="REVISE", feedback=CORRECTION))
    saved = SubtaskState.model_validate((await graph.aget_state(config)).values)
    assert saved.approval_note == ""
    assert saved.pending_execution_constraints[0].text == CORRECTION
    assert all(item.text != CORRECTION for item in saved.execution_constraints)
    assert plan_llm.prompts[-1]["human_feedback"] == CORRECTION
    assert plan_llm.prompts[-1]["proposed_execution_constraints"][-1]["text"] == CORRECTION
    graph = build()  # resume with a new graph and StepPlanner/Executor instances
    await comment("43", "Please update README formatting later.", CommentIntent(intent="CHATTER"))
    await comment("44", "approve", CommentIntent(intent="APPROVE"))
    assert len(executor_llm.prompts) == 2
    for payload in executor_llm.prompts:
        human = [item for item in payload["execution_constraints"] if item["source"] == "human_approval_note"]
        assert len(human) == 1 and human[0]["text"] == CORRECTION
        assert human[0]["provenance"] == "jira:TEST-1:comment:42:gate:test-gate"
        assert "README formatting" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_generated_test_rejected_from_human_constraint_only(local_with_scenario_check):
    tool, state, _ = local_with_scenario_check
    state.description = "Cover the empty phrase case"
    record_decision(state, ApprovalDecision(approval_status="approved", note="Empty strings should be allowed."))
    invalid = ('from app import check\ndef test_empty():\n'
               '    result = check("")\n'
               '    assert not result.passed\n')
    valid = invalid.replace("assert not result.passed", "assert result.passed")
    llm = Candidates([{"full_content": invalid}, {"full_content": valid}])
    ran = []
    def runner(checkout):
        ran.append(checkout)
        return passing(checkout)
    result = await ExecutorAgent(llm, tool, test_runner=runner, emit=no_event).run(state)
    assert result.execution_complete, result.failure_reason
    assert len(ran) == 1
    assert result.retry_attempts["1"][0].failure_type == "RequirementContradictionError"
    assert "Empty strings should be allowed" in result.retry_attempts["1"][0].failure_reason
    assert "app.py" not in result.file_changes


def test_only_ticket_constraints_cross_decomposition_boundary(coordinator):
    coordinator.execution_constraints = [
        constraint(coordinator, scope_type="ticket", scope_value=coordinator.ticket_id),
        constraint(coordinator, "Coordinator-only clarification"),
        constraint(coordinator, "Unrelated ticket", "ticket", "wrong-ticket"),
    ]
    states = materialize_subtasks(coordinator)
    assert all([item.text for item in state.execution_constraints] == [CORRECTION] for state in states)
    states[0].execution_constraints[0].text = "local mutation"
    assert states[1].execution_constraints[0].text == CORRECTION


@pytest.mark.asyncio
async def test_constraints_do_not_change_infrastructure_failure_classification(local, monkeypatch):
    tool, state, _ = local
    record_decision(state, ApprovalDecision(approval_status="approved", note=CORRECTION))
    def crash(*args, **kwargs):
        raise OSError("write service unavailable")
    monkeypatch.setattr(tool, "write_execution_file", crash)
    llm = Candidates([GOOD])
    result = await ExecutorAgent(llm, tool, emit=no_event).run(state)
    assert llm.calls == 1 and result.retry_count == 0
    assert result.retry_attempts["1"] == []
    assert result.failure_contexts[-1].classification == "infrastructure"
    assert CORRECTION in [item.text for item in result.execution_constraints]


def test_original_ticket_requirement_survives_planner_rewrite(local):
    _, state, _ = local
    original = state.description
    state.confirmed_repos = [state.repo]
    llm = Candidates([{"subtasks": [{"spec_id": "1", "type": "bug",
        "description": "Implement a guard", "repo": state.repo}], "reasoning": "One task"}])
    state = PlannerAgent(llm).run(state)
    assert state.description != original and state.ticket_requirement == original
    state.approval_payload = {"subtasks": state.subtask_specs}
    record_decision(state, ApprovalDecision(approval_status="approved"))
    record = state.execution_constraints[0]
    assert record.text == original and record.scope_type == "ticket"
    assert record.scope_value == state.ticket_id


def test_approved_replan_preserves_provenance_and_requires_new_scope_approval(db_ticket, local, monkeypatch):
    _, previous, _ = local
    ticket_id, _ = db_ticket
    previous.ticket_id = ticket_id
    prior_constraints = [
        constraint(previous, scope_type="ticket", scope_value=ticket_id),
        constraint(previous, "Subtask correction"),
        constraint(previous, "File correction", "file", "app.py"),
        constraint(previous, "Old step correction", "step", "1"),
    ]
    prior = {"repo": previous.repo, "source_subtask_id": previous.subtask_id,
             "orchestration_role": "legacy", "execution_constraints": [item.model_dump() for item in prior_constraints]}
    replacement_id = str(uuid.uuid4())
    monkeypatch.setattr(proposals, "prepare_subtask", lambda *a, **k:
                        SimpleNamespace(subtask_id=replacement_id, prior_attempt=prior))
    raw = proposals._new_state({"id": "proposal-1", "ticket_id": ticket_id,
        "source_comment_id": "42", "action": {"description": "Replan the empty phrase behavior"},
        "decision": {"approval_status": "approved", "note": "Preserve whitespace behavior"}})
    state = SubtaskState.model_validate(raw)
    assert state.approval_status == "pending"
    assert CORRECTION in [item.text for item in state.execution_constraints]
    assert any(item.provenance == "proposal:proposal-1:jira-comment:42" for item in state.execution_constraints)
    assert [item.text for item in state.pending_execution_constraints] == ["Subtask correction", "File correction"]
    assert state.pending_execution_constraints[0].scope_value == replacement_id
    assert state.pending_execution_constraints[0].provenance == prior_constraints[1].provenance
    record_decision(state, ApprovalDecision(approval_status="approved"))
    assert "Subtask correction" in [item.text for item in state.execution_constraints]
    assert "Old step correction" not in [item.text for item in state.execution_constraints]
    assert state.prior_attempt["execution_constraints"][-1]["text"] == "Old step correction"


def test_critic_receives_approved_constraints_as_explicit_review_input(local):
    tool, state, _ = local
    record_decision(state, ApprovalDecision(approval_status='approved', note=CORRECTION))
    llm = Candidates([{'approved': True, 'test_validity': 'valid', 'implementation_valid': True}])
    CriticAgent(llm, repo_tool=tool).run(state)
    assert CORRECTION in [item['text'] for item in llm.prompts[0]['execution_constraints']]
