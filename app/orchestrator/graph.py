"""Checkpointed Diagnosis → Step-Planner → human approval → surgical execution → PR."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from app.agents.executor import ExecutorAgent
from app.tools.github_tool import GitHubTool
from app.tools.repo_tool import RepoTool
from app.agents.diagnosis import Diagnosis, DiagnosisAgent
from app.agents.planning import ApprovalDecision, Plan
from app.agents.state import SubtaskState
from app.agents.step_planner import StepPlannerAgent
from app.config import settings
from app.core.failures import describe_failure
from app.core.run_control import require_active
from app.events import log_event
from app.tools.jira_tool import JiraTool

logger = logging.getLogger(__name__)


class ApprovalConflict(ValueError):
    """No matching pending gate, or another request is resuming this thread."""


def thread_config(ticket_id: str, subtask_id: str) -> dict:
    return {"configurable": {"thread_id": f"{ticket_id}:{subtask_id}"}, "recursion_limit": 100}


@asynccontextmanager
async def open_graph(ticket_id: str, subtask_id: str, *, lock: bool = False):
    # Never fall back to memory: an approval must survive process restarts.
    async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
        await saver.setup()
        if lock:
            cursor = await saver.conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked",
                (f"{ticket_id}:{subtask_id}",),
            )
            row = await cursor.fetchone()
            if not row["locked"]:
                raise ApprovalConflict("This subtask is already resuming")
        # Closing the connection releases the session-level advisory lock even
        # when a request fails. It serializes decisions across server workers.
        yield build_graph(checkpointer=saver)


async def _event(state: SubtaskState, agent: str, stage: str, message: str):
    await log_event(ticket_id=state.ticket_id, subtask_id=state.subtask_id,
                    agent=agent, stage=stage, message=message)


async def _jira_status(state: SubtaskState, stage: str, jira):
    if not state.jira_key:
        return
    try:
        result = await asyncio.to_thread(jira.set_status, state.jira_key, stage)
    except Exception as exc:
        result = {"applied": False, "reason": describe_failure(f"Setting Jira status to {stage}", exc)}
    await _event(state, "jira", "status", json.dumps({"key": state.jira_key, "stage": stage, "result": result}))


async def _jira_comment(state: SubtaskState, message: str, jira):
    if not state.jira_key:
        return
    try:
        await asyncio.to_thread(jira.comment, state.jira_key, message)
    except Exception as exc:
        await _event(state, "jira", "warning", describe_failure("Posting Jira comment", exc))
    else:
        await _event(state, "jira", "comment", message)


def _guard(state: SubtaskState) -> SubtaskState:
    state = SubtaskState.model_validate(state.model_dump())
    if state.retry_count > settings.max_agent_retries:
        state.status = "needs_human"
        state.failure_reason = state.failure_reason or "Retry limit reached"
    elif (state.budget_used.calls >= settings.ticket_call_budget or
          state.budget_used.est_cost_usd >= settings.ticket_cost_budget_usd):
        state.status, state.failure_reason = "needs_human", "Ticket budget exhausted"
    return state


def build_graph(agent=None, *, planner=None, checkpointer, jira=None, executor=None, publisher=None, gate_store=None, activity_check=require_active):
    async def run_agent(raw, name, supplied, factory):
        state = SubtaskState.model_validate(raw)
        await _event(state, name, "started", f"{name} started for {state.repo}")
        try:
            state = _guard(state)
            if state.status == "running":
                updated = await asyncio.to_thread((supplied or factory()).run, state.model_copy(deep=True))
                state = SubtaskState.model_validate(updated.model_dump())
                if state.status == "running":
                    if name == "diagnosis":
                        diagnosis = Diagnosis.model_validate(state.diagnosis)
                        if diagnosis.no_root_cause or not diagnosis.root_cause.strip():
                            reason = diagnosis.reasoning.strip() or "the available evidence was insufficient"
                            state.status = "needs_human"
                            state.failure_reason = f"Diagnosis could not determine a root cause: {reason}"
                    else:
                        Plan.model_validate(state.plan)
                state = _guard(state)
        except Exception as exc:
            state.status = "needs_human"
            operation = "Validating or running Step-Planner" if name == "step_planner" else "Running diagnosis"
            state.failure_reason = describe_failure(operation, exc)
        message = state.failure_reason if state.status == "needs_human" else (
            json.dumps({"plan": state.model_dump(mode="json")["plan"], "reasoning": state.plan_reasoning})
            if name == "step_planner" else f"Found: {(state.diagnosis or {}).get('root_cause', '')}"
        )
        await _event(state, name, "needs_human" if state.status == "needs_human" else "done", message)
        if name == "diagnosis" and state.status == "running":
            await _jira_comment(state, message, jira or JiraTool())
        return state.model_dump(mode="json")

    async def diagnose(raw):
        return await run_agent(raw, "diagnosis", agent, DiagnosisAgent)

    async def plan(raw):
        return await run_agent(raw, "step_planner", planner, StepPlannerAgent)

    async def prepare_approval(raw):
        state = SubtaskState.model_validate(raw)
        Plan.model_validate(state.plan)
        state.approval_status = "pending"
        state.approval_payload = {
            "plan": state.model_dump(mode="json")["plan"],
            "reasoning": state.plan_reasoning,
        }
        message = "Needs approval — proposed plan:\n" + "\n".join(
            f"{step.step_id}. {step.intent} ({step.target_file})" for step in state.plan
        ) + f"\nReasoning: {state.plan_reasoning}"
        tool = jira or JiraTool()
        await _jira_status(state, "awaiting_approval", tool)
        from app.core.approvals import register_plan_gate
        await (gate_store or register_plan_gate)(state, tool, message)
        await _event(state, "approval", "needs_approval", message)
        return state.model_dump(mode="json")

    def human_gate(raw):
        state = SubtaskState.model_validate(raw)
        # This node restarts on resume: keep all world actions in other nodes.
        decision = ApprovalDecision.model_validate(interrupt(state.approval_payload))
        state.approval_status = decision.approval_status
        state.approval_note = decision.note
        if decision.approval_status == "rejected":
            state.status = "needs_human"
            state.failure_reason = f"Plan rejected: {decision.note}"
        return state.model_dump(mode="json")

    async def apply_decision(raw):
        state = SubtaskState.model_validate(raw)
        approved = state.approval_status == "approved"
        tool = jira or JiraTool()
        await _jira_status(state, "in_progress" if approved else "blocked", tool)
        message = "Plan approved; work resuming" if approved else state.failure_reason
        await _jira_comment(state, message, tool)
        await _event(state, "approval", state.approval_status, message)
        if not approved:
            await asyncio.to_thread(RepoTool().cleanup_workspace, state.subtask_id)
        return state.model_dump(mode="json")

    async def execute(raw):
        state = SubtaskState.model_validate(raw)
        try:
            await asyncio.to_thread(activity_check, state)
            updated = await (executor or ExecutorAgent()).run(state)
            updated = _guard(updated)
        except Exception as exc:
            updated = state
            updated.status, updated.failure_reason = 'needs_human', describe_failure('Executing approved plan', exc)
        if updated.status == 'running' and updated.steps_done:
            outcome = updated.steps_done[-1]['tests']['outcome']
            note = {'passed': 'pytest passed', 'no_tests_collected': 'no tests to verify yet'}.get(outcome, outcome)
            await _jira_comment(updated, f"Applied step {updated.current_step}; {note}", jira or JiraTool())
        return updated.model_dump(mode="json")

    async def publish_pr(raw):
        state = SubtaskState.model_validate(raw)
        try:
            await asyncio.to_thread(activity_check, state)
            result = await asyncio.to_thread((publisher or GitHubTool()).publish_changes, state)
            state.pr_url = result['url']
            state.status = 'in_review'
            if state.jira_key:
                from app.core.pr_sync import record_pr
                await asyncio.to_thread(record_pr, state.jira_key, result)
            await _event(state, 'github', 'pr_opened', f"PR opened: {state.pr_url}")
        except Exception as exc:
            state.status, state.failure_reason = 'needs_human', describe_failure('Publishing pull request', exc)
        return state.model_dump(mode="json")

    async def notify_pr(raw):
        state = SubtaskState.model_validate(raw)
        await _jira_status(state, 'in_review', jira or JiraTool())
        message = f"PR ready for review: {state.pr_url}"
        if state.verifiability == 'no_tests':
            message += "\nNote: this repo has no tests — the change could not be verified automatically."
        await _jira_comment(state, message, jira or JiraTool())
        await asyncio.to_thread(RepoTool().cleanup_workspace, state.subtask_id)
        return state.model_dump(mode='json')

    async def escalate(raw):
        state = SubtaskState.model_validate(raw)
        tool = jira or JiraTool()
        await _jira_status(state, "blocked", tool)
        await _jira_comment(state, f"Blocked: {state.failure_reason}", tool)
        await asyncio.to_thread(RepoTool().cleanup_workspace, state.subtask_id)
        return state.model_dump(mode="json")

    def next_after_agent(raw):
        return "escalate" if raw["status"] != "running" else "continue"

    graph = StateGraph(dict)
    for name, node in [("diagnosis", diagnose), ("step_planner", plan),
                       ("prepare_approval", prepare_approval), ("human_gate", human_gate),
                       ("apply_decision", apply_decision), ("execute", execute), ("publish_pr", publish_pr), ("notify_pr", notify_pr), ("escalate", escalate)]:
        graph.add_node(name, node)
    graph.set_entry_point("diagnosis")
    graph.add_conditional_edges("diagnosis", next_after_agent, {"continue": "step_planner", "escalate": "escalate"})
    graph.add_conditional_edges("step_planner", next_after_agent, {"continue": "prepare_approval", "escalate": "escalate"})
    graph.add_edge("prepare_approval", "human_gate")
    graph.add_edge("human_gate", "apply_decision")
    graph.add_conditional_edges("apply_decision", lambda raw: raw["approval_status"], {"approved": "execute", "rejected": END})
    graph.add_conditional_edges("execute", lambda raw: 'escalate' if raw['status'] != 'running' else
                                'publish_pr' if raw['execution_complete'] else 'execute')
    graph.add_conditional_edges("publish_pr", lambda raw: 'notify_pr' if raw['status'] == 'in_review' else 'escalate')
    graph.add_edge("notify_pr", END)
    graph.add_edge("escalate", END)
    return graph.compile(checkpointer=checkpointer)


async def run_diagnosis_graph(state: SubtaskState) -> SubtaskState:
    async with open_graph(state.ticket_id, state.subtask_id, lock=True) as graph:
        config = thread_config(state.ticket_id, state.subtask_id)
        snapshot = await graph.aget_state(config)
        # Retry a crashed request from its durable state, never rerun diagnosis.
        if snapshot.values:
            if snapshot.next and not any(task.interrupts for task in snapshot.tasks):
                await graph.ainvoke(None, config=config)
        else:
            await graph.ainvoke(state.model_dump(mode="json"), config=config)
        return SubtaskState.model_validate((await graph.aget_state(config)).values)


async def resume_approval(graph, ticket_id: str, subtask_id: str, decision: ApprovalDecision) -> SubtaskState:
    config = thread_config(ticket_id, subtask_id)
    snapshot = await graph.aget_state(config)
    if not snapshot.values:
        raise ApprovalConflict("No checkpoint exists for this subtask")
    state = SubtaskState.model_validate(snapshot.values)
    if state.ticket_id != ticket_id or state.subtask_id != subtask_id:
        raise ApprovalConflict("Checkpoint identity mismatch")
    if state.approval_status != "pending":
        # Retrying the same decision recovers any nodes left after a crash.
        if state.approval_status != decision.approval_status or state.approval_note != decision.note:
            raise ApprovalConflict("This plan already has a different decision")
        if snapshot.next:
            await graph.ainvoke(None, config=config)
    else:
        if snapshot.next != ("human_gate",) or not any(task.interrupts for task in snapshot.tasks):
            raise ApprovalConflict("This subtask is not waiting for approval")
        await graph.ainvoke(Command(resume=decision.model_dump()), config=config)
    return SubtaskState.model_validate((await graph.aget_state(config)).values)
