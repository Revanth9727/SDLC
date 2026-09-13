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

from app.agents.critic import CriticAgent, CriticVerdict
from app.agents.code_intelligence import CodeContext, CodeIntelligenceAgent
from app.agents.executor import ExecutorAgent
from app.tools.github_tool import GitHubTool
from app.tools.repo_tool import RepoTool
from app.agents.diagnosis import Diagnosis, DiagnosisAgent
from app.agents.planner import PlannerAgent
from app.agents.planning import ApprovalDecision, Plan, Step
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


# Every checkpointed human-decision pause point in the graph, in flow order:
# intent_gate confirms the Planner's decomposition (R-30), human_gate approves
# the Step-Planner's plan (R-48). Both use the identical interrupt+ApprovalDecision
# mechanism, so callers generic over "which gate is this" check membership here.
GATE_NODES = ("intent_gate", "human_gate")


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
    from app.core.guard import check
    return check(state)


def build_graph(agent=None, *, planner=None, decomposer=None, investigator=None, checkpointer, jira=None, executor=None, critic=None,
                publisher=None, gate_store=None, memory_search=None, memory_writer=None,
                activity_check=require_active, repo_tool=None, overview_builder=None):
    workflow_repo_tool = (
        repo_tool
        or getattr(agent, "repo_tool", None)
        or getattr(executor, "repo_tool", None)
        or RepoTool()
    )
    async def run_agent(raw, name, supplied, factory):
        state = SubtaskState.model_validate(raw)
        await _event(state, name, "started", f"{name} started for {state.repo}")
        state.guard_node, state.guard_error = name, None
        try:
            state = await asyncio.to_thread(_guard, state)
            if state.status == 'running':
                from app.core.guard import validate_output
                updated = await asyncio.to_thread((supplied or factory()).run, state.model_copy(deep=True))
                state = validate_output(state, updated, name)
        except Exception as exc:
            from app.core.budget import BudgetExceeded
            if isinstance(exc, BudgetExceeded):
                state.status, state.failure_reason = 'needs_human', str(exc)
            else:
                state.guard_error = describe_failure(f'Running {name}', exc)
        if state.status == "needs_human":
            message = state.failure_reason
        elif name == "planner":
            message = json.dumps({"subtasks": state.subtask_specs, "reasoning": state.decomposition_reasoning})
        elif name == "step_planner":
            message = json.dumps({"plan": state.model_dump(mode="json")["plan"], "reasoning": state.plan_reasoning})
        elif name == "code_intelligence":
            message = json.dumps({
                "hypothesis": state.code_context.get("hypothesis"),
                "confidence": state.code_context.get("confidence"),
                "relevant_files": state.code_context.get("relevant_files", []),
                "verified_evidence": len(state.code_context.get("verified_evidence", [])),
            })
        else:
            message = f"Found: {(state.diagnosis or {}).get('root_cause', '')}"
        await _event(state, name, "needs_human" if state.status == "needs_human" else "done", message)
        if name == "diagnosis" and state.status == "running":
            await _jira_comment(state, message, jira or JiraTool())
        return state.model_dump(mode="json")

    async def decompose(raw):
        return await run_agent(raw, "planner", decomposer, PlannerAgent)

    async def repo_overview(raw):
        state = SubtaskState.model_validate(raw)
        state.guard_node, state.guard_error = "repo_overview", None
        await _event(state, "repo_overview", "started", "Understanding the repository...")
        try:
            if overview_builder is not None:
                build = overview_builder
            elif decomposer is not None:
                # Dependency-injected graph tests do not own real GitHub checkouts.
                build = lambda repos, _workspace: [
                    {"repo": repo, "ref": "test", "commit_sha": "test", "source": "injected",
                     "snapshot_id": None, "file_count": 0, "excluded_count": 0,
                     "files": [], "directories": [], "modules": []}
                    for repo in repos
                ]
            else:
                from app.repo_intelligence.overview import build_repo_overviews
                build = lambda repos, workspace: build_repo_overviews(
                    repos, workspace, repo_tool=workflow_repo_tool,
                )
            state.repo_overview = await asyncio.to_thread(build, state.confirmed_repos, state.subtask_id)
            summary = [{"repo": item["repo"], "files": item["file_count"],
                        "modules": item["modules"], "source": item["source"]}
                       for item in state.repo_overview]
            await _event(state, "repo_overview", "done", json.dumps(summary))
        except Exception as exc:
            state.guard_error = describe_failure("Understanding the repository", exc)
            await _event(state, "repo_overview", "failed", state.guard_error)
        return state.model_dump(mode="json")

    async def diagnose(raw):
        if agent is not None:
            return await run_agent(raw, "diagnosis", agent, DiagnosisAgent)
        loop = asyncio.get_running_loop()

        def stream_tool(record):
            state = SubtaskState.model_validate(raw)
            future = asyncio.run_coroutine_threadsafe(
                _event(state, "code_search", record["tool"], json.dumps(record)), loop,
            )
            future.result()

        return await run_agent(raw, "diagnosis", DiagnosisAgent(event_sink=stream_tool), DiagnosisAgent)

    async def investigate(raw):
        loop = asyncio.get_running_loop()

        def stream_tool(record):
            state = SubtaskState.model_validate(raw)
            future = asyncio.run_coroutine_threadsafe(
                _event(state, "code_intelligence", record.get("tool", "tool"), json.dumps(record)), loop,
            )
            future.result()

        if investigator is not None:
            selected = investigator
        elif agent is not None:
            class InjectedFlowContext:
                def run(self, state):
                    if not state.code_context:
                        state.code_context = CodeContext(
                            relevant_files=[], relevant_functions=[], execution_path=[],
                            confidence=.1, hypothesis="Injected test flow",
                            verified_evidence=[{"verified": True, "path": "injected"}],
                            repo_snapshot_id=state.repo_snapshot_id or "injected",
                            commit_sha=state.base_commit or "injected",
                        ).model_dump(mode="json")
                    return state
            selected = InjectedFlowContext()
        else:
            selected = CodeIntelligenceAgent(repo_tool=workflow_repo_tool, event_sink=stream_tool)
        return await run_agent(raw, "code_intelligence", selected, CodeIntelligenceAgent)

    async def plan(raw):
        return await run_agent(raw, "step_planner", planner, StepPlannerAgent)

    async def prepare_intent_confirmation(raw):
        state = SubtaskState.model_validate(raw)
        state.approval_status = "pending"
        state.approval_payload = {
            "subtasks": state.subtask_specs,
            "reasoning": state.decomposition_reasoning,
        }
        lines = "\n".join(
            f"{spec['spec_id']}. ({spec['type']}) {spec['description']} [{spec['repo']}]"
            for spec in state.subtask_specs
        )
        message = f"I read this as:\n{lines}\nReasoning: {state.decomposition_reasoning}\nCorrect?"
        tool = jira or JiraTool()
        await _jira_status(state, "awaiting_approval", tool)
        from app.core.approvals import register_plan_gate
        await (gate_store or register_plan_gate)(state, tool, message, kind="intent")
        state.approval_note = ""
        await _event(state, "planner", "needs_confirmation", message)
        return state.model_dump(mode="json")

    async def apply_intent_decision(raw):
        state = SubtaskState.model_validate(raw)
        approved = state.approval_status == "approved"
        tool = jira or JiraTool()
        if approved:
            await _jira_status(state, "in_progress", tool)
            message = "Decomposition confirmed; proceeding"
        else:
            state.status = "needs_human"
            state.failure_reason = f"Decomposition rejected: {state.approval_note or 'no reason given'}"
            await _jira_status(state, "blocked", tool)
            message = state.failure_reason
        await _jira_comment(state, message, tool)
        await _event(state, "planner", state.approval_status, message)
        return state.model_dump(mode="json")

    async def reuse_check(raw):
        state = SubtaskState.model_validate(raw)
        try:
            # Read-only reasoning step, pre-approval — like diagnosis/step_planner
            # (not execute/critic/publish), it doesn't gate on activity_check.
            state.guard_node, state.guard_error = 'reuse_check', None
            state = await asyncio.to_thread(_guard, state)
            if state.status != 'running':
                return state.model_dump(mode='json')
            from app.memory.store import search_similar
            matches = await asyncio.to_thread(
                memory_search or search_similar, state.description, state.subtask_type,
                threshold=settings.memory_search_threshold,
            )
        except Exception as exc:
            state.guard_node = 'reuse_check'
            state.guard_error = describe_failure('Checking for a reusable past resolution', exc)
            return state.model_dump(mode='json')
        state.retry_count = 0
        from app.memory.store import advisory_refs
        state.prior_resolutions = advisory_refs(matches)
        state.memory_refs = state.prior_resolutions
        if matches:
            await _event(
                state, "memory", "recalled",
                f"Recalled {len(state.prior_resolutions)} advisory resolution(s); "
                f"best similarity {matches[0]['similarity']:.2f}",
            )
        top = next(
            (match for match in matches
             if match["similarity"] >= settings.memory_reuse_similarity_threshold),
            None,
        )
        if not top:
            await _event(state, 'memory', 'no_reuse', 'No strong past match; running full diagnosis.')
            return state.model_dump(mode='json')
        # Freshness/applicability check (memory.md §8a): a similar past ticket is
        # not a proven-correct fix for THIS one — if the files it touched are
        # gone, the resolution can no longer even apply; fall through rather
        # than propose something stale.
        try:
            repo_files = set(await asyncio.to_thread(workflow_repo_tool.list_files, state.repo, state.subtask_id))
        except Exception as exc:
            await _event(state, 'memory', 'reuse_unavailable',
                        describe_failure('Checking repo freshness for reuse', exc))
            return state.model_dump(mode='json')
        missing = [f for f in top['files_touched'] if f not in repo_files]
        if missing or not top['files_touched']:
            reason = f"missing {', '.join(missing)}" if missing else "it named no files to reapply"
            await _event(state, 'memory', 'reuse_stale',
                        f"Past fix from ticket {top['ticket_id']} no longer applies: {reason}")
            return state.model_dump(mode='json')
        # Strong, fresh match: propose it and skip Diagnosis + Step-Planner —
        # but NEVER blind-apply (R-29): the human gate, Executor, and Critic
        # below all still run exactly as they would for a fresh diagnosis.
        state.reuse_source = top['ticket_id']
        state.reuse_similarity = top['similarity']
        state.diagnosis = {
            'root_cause': f"Reused from ticket {top['ticket_id']} (similarity {top['similarity']:.2f})",
            'files': top['files_touched'], 'reasoning': top['resolution_summary'],
        }
        state.plan = [
            Step(step_id=str(i + 1), intent=f"Reapply the known fix: {top['resolution_summary']}",
                target_file=path, action='edit')
            for i, path in enumerate(top['files_touched'])
        ]
        state.plan_reasoning = (f"Reused from ticket {top['ticket_id']} "
                                f"(similarity {top['similarity']:.2f}): {top['resolution_summary']}")
        reuse_tool = workflow_repo_tool
        state.base_commit = await asyncio.to_thread(reuse_tool.revision, state.repo, state.subtask_id)
        state.diagnosed_file_hashes = await asyncio.to_thread(
            reuse_tool.file_fingerprints, state.repo, state.subtask_id, top['files_touched'])
        state.freshness_recorded = True
        await _event(state, 'memory', 'reuse',
                    f"Reusing resolution from ticket {top['ticket_id']} (similarity {top['similarity']:.2f})")
        return state.model_dump(mode='json')

    async def prepare_approval(raw):
        state = SubtaskState.model_validate(raw)
        Plan.model_validate(state.plan)
        if state.freshness_recorded:
            targets = list(dict.fromkeys(step.target_file for step in state.plan))
            missing = [path for path in targets if path not in state.diagnosed_file_hashes]
            if missing:
                fingerprints = await asyncio.to_thread(
                    workflow_repo_tool.file_fingerprints, state.repo, state.subtask_id, missing)
                state.diagnosed_file_hashes.update(fingerprints)
        state.approval_status = "pending"
        state.approval_payload = {
            "plan": state.model_dump(mode="json")["plan"],
            "reasoning": state.plan_reasoning,
            "revision": state.replan_count,
        }
        message = "Needs approval — proposed plan:\n" + "\n".join(
            f"{step.step_id}. {step.intent} ({step.target_file})" for step in state.plan
        ) + f"\nReasoning: {state.plan_reasoning}"
        tool = jira or JiraTool()
        await _jira_status(state, "awaiting_approval", tool)
        from app.core.approvals import register_plan_gate
        await (gate_store or register_plan_gate)(state, tool, message)
        state.approval_note = ""
        await _event(state, "approval", "needs_approval", message)
        return state.model_dump(mode="json")

    def human_gate(raw):
        state = SubtaskState.model_validate(raw)
        # This node restarts on resume: keep all world actions in other nodes.
        decision = ApprovalDecision.model_validate(interrupt(state.approval_payload))
        state.approval_status = decision.approval_status
        state.approval_note = decision.note
        return state.model_dump(mode="json")

    async def apply_decision(raw):
        state = SubtaskState.model_validate(raw)
        approved = state.approval_status == "approved"
        tool = jira or JiraTool()
        await _jira_status(state, "in_progress" if approved else "awaiting_approval", tool)
        message = "Plan approved; work resuming" if approved else f"Plan feedback received; re-planning: {state.approval_note}"
        await _jira_comment(state, message, tool)
        await _event(state, "approval", state.approval_status, message)
        if not approved:
            state.last_rejection_note = state.approval_note
            state.replan_count += 1
            if state.replan_count >= settings.max_agent_retries:
                state.status = "needs_human"
                state.failure_reason = "Re-planning retry limit reached"
            else:
                state.status = "running"
                state.failure_reason = None
                state.approval_status = "pending"
                state.approval_payload = None
                if state.reuse_source:
                    # The human rejected the reused fix (R-29: never blind-apply) —
                    # don't keep re-planning off the synthetic "reused" diagnosis;
                    # fall through to a real one instead.
                    state.reuse_source, state.reuse_similarity, state.diagnosis = None, None, None
        return state.model_dump(mode="json")

    async def execute(raw):
        state = SubtaskState.model_validate(raw)
        loop = asyncio.get_running_loop()

        def stream_execution_tool(record):
            future = asyncio.run_coroutine_threadsafe(
                _event(state, "executor", record.get("tool", "tool"), json.dumps(record)), loop,
            )
            future.result()
        try:
            await asyncio.to_thread(activity_check, state)
            from app.core.guard import validate_output
            state.guard_node, state.guard_error = 'execute', None
            state = await asyncio.to_thread(_guard, state)
            if state.status != 'running':
                return state.model_dump(mode='json')
            if executor is not None:
                selected_executor = executor
            else:
                from app.tools.code_search import CodeSearchTool
                selected_executor = ExecutorAgent(
                    repo_tool=workflow_repo_tool,
                    code_search=CodeSearchTool(repo_tool=workflow_repo_tool, event_sink=stream_execution_tool),
                )
            updated = await selected_executor.run(state.model_copy(deep=True))
            updated = validate_output(state, updated, 'execute')
        except Exception as exc:
            updated = state
            updated.guard_node = 'execute'
            updated.guard_error = describe_failure('Executing approved plan', exc)
        if updated.status == 'running' and updated.steps_done:
            outcome = updated.steps_done[-1]['tests']['outcome']
            note = {'passed': 'pytest passed', 'no_tests_collected': 'no tests to verify yet'}.get(outcome, outcome)
            await _jira_comment(updated, f"Applied step {updated.current_step}; {note}", jira or JiraTool())
        return updated.model_dump(mode="json")

    async def verify_freshness(raw):
        state = SubtaskState.model_validate(raw)
        state.guard_node, state.guard_error = 'freshness', None
        try:
            from app.core.freshness import check_freshness
            state = await asyncio.to_thread(check_freshness, state, workflow_repo_tool)
        except Exception as exc:
            state.status = 'needs_human'
            state.failure_reason = describe_failure('Re-verifying repository freshness', exc)
            await _event(state, 'freshness', 'failed', state.failure_reason)
            return state.model_dump(mode='json')
        targets = list(dict.fromkeys(step.target_file for step in state.plan))
        if state.status == 'needs_human':
            await _event(state, 'freshness', 'drifted', state.failure_reason)
        else:
            await _event(state, 'freshness', 'verified',
                         f"Verified {len(targets)} target file(s) at {(state.base_commit or '')[:12]}")
        return state.model_dump(mode='json')

    async def critic_review(raw):
        state = SubtaskState.model_validate(raw)
        loop = asyncio.get_running_loop()

        def stream_critic_tool(record):
            future = asyncio.run_coroutine_threadsafe(
                _event(state, "critic", record.get("tool", "tool"), json.dumps(record)), loop,
            )
            future.result()
        try:
            await asyncio.to_thread(activity_check, state)
            from app.core.guard import validate_output
            state.guard_node, state.guard_error = 'critic', None
            state = await asyncio.to_thread(_guard, state)
            if state.status != 'running':
                return state.model_dump(mode='json')
            if critic is not None:
                selected_critic = critic
                # Injected agents must inspect the same isolated checkout as the
                # rest of this graph. Otherwise a default RepoTool can fail before
                # the Critic returns its otherwise-valid verdict.
                from app.tools.code_search import CodeSearchTool
                selected_critic.repo_tool = workflow_repo_tool
                selected_critic.code_search = CodeSearchTool(
                    repo_tool=workflow_repo_tool, event_sink=stream_critic_tool
                )
            else:
                from app.tools.code_search import CodeSearchTool
                selected_critic = CriticAgent(
                    repo_tool=workflow_repo_tool,
                    code_search=CodeSearchTool(repo_tool=workflow_repo_tool, event_sink=stream_critic_tool),
                )
            updated = await asyncio.to_thread(selected_critic.run, state.model_copy(deep=True))
            updated = validate_output(state, updated, 'critic')
        except Exception as exc:
            updated = state
            updated.guard_node = 'critic'
            updated.guard_error = describe_failure('Running critic review', exc)
            return updated.model_dump(mode='json')
        verdict = CriticVerdict.model_validate(updated.critic_verdict)
        await _event(updated, 'critic', 'approved' if verdict.approved else 'rejected', verdict.summary)
        tool = jira or JiraTool()
        if verdict.approved:
            updated.critic_feedback = []
            if updated.orchestration_role == "work":
                updated.status = "integration_pending"
            await _jira_comment(updated, f"Critic: {verdict.summary}", tool)
        else:
            updated.critic_retry_count += 1
            if updated.critic_retry_count > settings.max_agent_retries:
                updated.status = 'needs_human'
                updated.failure_reason = f"Critic rejected the change: {'; '.join(verdict.issues)}"
            else:
                # Full redo, informed by the issues (architecture.md §4): reset
                # execution to the approved plan's start, not a partial patch —
                # the Executor rebuilds the checkout from base_commit each time
                # anyway (R-20 freshness), so a clean redo is cheap and correct.
                updated.critic_feedback = verdict.issues
                updated.current_step = 0
                updated.steps_done = []
                updated.file_changes = {}
                updated.execution_complete = False
            await _jira_comment(updated, f"Critic requested changes: {'; '.join(verdict.issues)}", tool)
        return updated.model_dump(mode='json')

    async def publish(raw):
        state = SubtaskState.model_validate(raw)
        state.guard_node = 'publish'
        try:
            await asyncio.to_thread(activity_check, state)
            from sqlalchemy import select
            from app.db.connection import SessionLocal
            from app.db.models import Ticket
            with SessionLocal() as db:
                ticket = db.get(Ticket, state.ticket_id)
                if ticket is not None:
                    db.expunge(ticket)
            if ticket is None:
                ticket = Ticket(id=state.ticket_id, source="graph", external_key=state.jira_key,
                                title=state.description.splitlines()[0], description=state.description)
            from app.core.publish import publish as shared_publish
            urls = await shared_publish(ticket, [state], publisher=publisher or GitHubTool(),
                                        jira=jira, repo_tool=workflow_repo_tool)
            state.pr_url = urls[0]
            state.status = 'in_review'
        except Exception as exc:
            state.status, state.failure_reason = 'needs_human', describe_failure('Publishing pull request', exc)
        return state.model_dump(mode="json")

    async def escalate(raw):
        from uuid import uuid4
        state = SubtaskState.model_validate(raw)
        state.escalation_id = str(uuid4())
        state.resolution = None
        state.approval_payload = None
        tool = jira or JiraTool()
        await _jira_status(state, "blocked", tool)
        await _jira_comment(state, f"Blocked: {state.failure_reason}", tool)
        try:
            await asyncio.to_thread(workflow_repo_tool.cleanup_workspace, state.subtask_id)
        except Exception as exc:
            await _event(state, 'workspace', 'warning', describe_failure('Cleaning workspace', exc))
        await _event(state, 'guard', 'needs_human', state.failure_reason or 'Human help required')
        from app.web.approval import persist_state
        try:
            await asyncio.to_thread(persist_state, state)
        except Exception:
            logger.warning('Could not mirror escalation; checkpoint remains authoritative', exc_info=True)
        try:
            from app.memory.store import write_resolution
            await asyncio.to_thread(memory_writer or write_resolution, state, "escalated")
        except Exception as exc:
            await _event(state, 'memory', 'warning', describe_failure('Writing escalation memory', exc))
        return state.model_dump(mode="json")

    async def guard(raw):
        state = await asyncio.to_thread(_guard, SubtaskState.model_validate(raw))
        await _event(state, 'guard', 'retry' if state.guard_retry else state.status,
                     state.guard_error or state.failure_reason or 'Output and limits validated')
        return state.model_dump(mode='json')

    def human_resolution(raw):
        state = SubtaskState.model_validate(raw)
        decision = interrupt({'kind': 'needs_human', 'reason': state.failure_reason,
                              'budget': state.budget_used.model_dump(), 'actions': ['retry', 'reject']})
        if decision.get('action') not in {'retry', 'reject'}:
            raise ValueError('Unknown human resolution')
        state.resolution = decision['action']
        state.approval_note = decision.get('note', '')
        return state.model_dump(mode='json')

    async def apply_resolution(raw):
        state = SubtaskState.model_validate(raw)
        if state.resolution == 'reject':
            state.status = 'failed'
            state.failure_reason = 'Closed by human: ' + state.approval_note
        else:
            from app.core.budget import extend
            await asyncio.to_thread(extend, state.ticket_id)
            state.status, state.failure_reason = 'running', None
            state.retry_count, state.replan_count, state.critic_retry_count = 0, 0, 0
            state.guard_error, state.guard_retry = None, False
            await _jira_status(state, 'in_progress', jira or JiraTool())
        from app.web.approval import persist_state
        await asyncio.to_thread(persist_state, state)
        await _jira_comment(state, 'Human resolution: ' + state.resolution + '. ' + state.approval_note, jira or JiraTool())
        await _event(state, 'guard', 'resolution', state.resolution)
        if state.status == 'failed':
            try:
                from app.memory.store import write_resolution
                await asyncio.to_thread(memory_writer or write_resolution, state, "failed")
            except Exception as exc:
                await _event(state, 'memory', 'warning', describe_failure('Writing failure memory', exc))
        return state.model_dump(mode='json')

    def next_after_agent(raw):
        return "escalate" if raw["status"] != "running" else "continue"

    def safe_node(name, node):
        async def wrapped(raw):
            try:
                updated = await node(raw)
                result = SubtaskState.model_validate(updated)
                result.guard_node = name
                return result.model_dump(mode='json')
            except Exception as exc:
                state = SubtaskState.model_validate(raw)
                state.guard_node = name
                state.status, state.failure_reason = 'needs_human', describe_failure(name, exc)
                return state.model_dump(mode='json')
        return wrapped

    graph = StateGraph(dict)
    for name, node in [("repo_overview", repo_overview), ("planner", decompose), ("reuse_check", reuse_check),
                       ("code_intelligence", investigate), ("diagnosis", diagnose),
                       ("step_planner", plan),
                       ("guard", guard), ("human_resolution", human_resolution), ("apply_resolution", apply_resolution),
                       ("prepare_intent_confirmation", prepare_intent_confirmation), ("intent_gate", human_gate),
                       ("apply_intent_decision", apply_intent_decision),
                       ("prepare_approval", prepare_approval), ("human_gate", human_gate),
                       ("apply_decision", apply_decision), ("freshness", verify_freshness),
                       ("execute", execute), ("critic", critic_review),
                       ("publish", publish), ("escalate", escalate)]:
        graph.add_node(name, safe_node(name, node) if name in
                       {'prepare_intent_confirmation', 'apply_intent_decision',
                        'prepare_approval', 'apply_decision', 'publish'} else node)
    graph.set_conditional_entry_point(
        lambda raw: "reuse_check" if raw.get("orchestration_role") == "work" else "repo_overview",
        {"repo_overview": "repo_overview", "reuse_check": "reuse_check"},
    )
    graph.add_edge('repo_overview', 'guard')
    graph.add_edge('planner', 'guard')
    graph.add_edge('reuse_check', 'guard')
    graph.add_edge('code_intelligence', 'guard')
    graph.add_edge('diagnosis', 'guard')
    graph.add_edge('step_planner', 'guard')
    graph.add_edge('execute', 'guard')
    graph.add_edge('freshness', 'guard')
    graph.add_edge('critic', 'guard')
    def after_guard(raw):
        if raw['status'] not in {'running', 'integration_pending', 'in_review'}:
            return 'escalate'
        node = raw['guard_node']
        if raw['guard_retry']:
            return node
        if node == 'apply_intent_decision' and raw.get('orchestration_role') == 'coordinator':
            return END
        if node == 'critic' and raw.get('orchestration_role') == 'work' and raw['status'] == 'integration_pending':
            return END
        return {'repo_overview': 'planner', 'planner': 'prepare_intent_confirmation',
                'prepare_intent_confirmation': 'intent_gate', 'apply_intent_decision': 'reuse_check',
                'reuse_check': 'prepare_approval' if raw['reuse_source'] else 'code_intelligence',
                'code_intelligence': 'diagnosis',
                'diagnosis': 'step_planner', 'step_planner': 'prepare_approval',
                'freshness': 'execute',
                'execute': 'critic' if raw['execution_complete'] else 'execute',
                'critic': 'publish' if raw['execution_complete'] else 'execute',
                'prepare_approval': 'human_gate', 'human_gate': 'apply_decision',
                'apply_decision': 'freshness' if raw['approval_status'] == 'approved' else
                                  ('step_planner' if raw['diagnosis'] else 'code_intelligence'),
                'publish': END}[node]
    graph.add_conditional_edges('guard', after_guard)
    for guarded in ('prepare_intent_confirmation', 'apply_intent_decision',
                    'prepare_approval', 'apply_decision', 'publish'):
        graph.add_edge(guarded, 'guard')
    graph.add_edge('intent_gate', 'apply_intent_decision')
    graph.add_edge('human_gate', 'apply_decision')
    graph.add_edge('escalate', 'human_resolution')
    graph.add_edge('human_resolution', 'apply_resolution')
    graph.add_conditional_edges('apply_resolution', lambda raw: END if raw['resolution'] == 'reject' else
                                'critic' if raw['guard_node'] == 'execute' and raw['execution_complete'] else raw['guard_node'])
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
    if (decision.approval_status == 'rejected' and decision.note and
            state.approval_status == 'pending' and state.last_rejection_note == decision.note):
        return state
    if state.approval_status != "pending":
        # Retrying the same decision recovers any nodes left after a crash.
        if state.approval_status != decision.approval_status or state.approval_note != decision.note:
            raise ApprovalConflict("This plan already has a different decision")
        if snapshot.next:
            await graph.ainvoke(None, config=config)
    else:
        if snapshot.next not in {(node,) for node in GATE_NODES} or not any(task.interrupts for task in snapshot.tasks):
            raise ApprovalConflict("This subtask is not waiting for approval")
        await graph.ainvoke(Command(resume=decision.model_dump()), config=config)
    return SubtaskState.model_validate((await graph.aget_state(config)).values)
