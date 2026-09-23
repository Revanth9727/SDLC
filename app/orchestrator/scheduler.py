"""Sequential, dependency-aware scheduling for isolated Phase 8 sub-tasks."""
from __future__ import annotations

import asyncio
import copy
import json
import uuid
from typing import Awaitable, Callable

from sqlalchemy import select, text

from app.agents.state import SubtaskState
from app.core.failures import describe_failure
from app.core.execution_constraints import inherited_ticket_constraints
from app.db.connection import SessionLocal
from app.db.models import Subtask, Ticket
from app.events import log_event
from app.tools.repo_tool import RepoTool

SUCCESS = {"integration_pending", "in_review", "done"}
FAILURE = {"needs_human", "failed"}
ACTIVE = {"running", "waiting", "pending"}


def materialize_subtasks(coordinator: SubtaskState) -> list[SubtaskState]:
    """Create one DB row/state per Planner spec without sibling state leakage."""
    if coordinator.orchestration_role != "coordinator" or not coordinator.subtask_specs:
        raise ValueError("A confirmed coordinator decomposition is required")
    ticket_id = uuid.UUID(coordinator.ticket_id)
    parent_id = uuid.UUID(coordinator.subtask_id)
    specs = coordinator.subtask_specs
    spec_ids = [str(spec["spec_id"]) for spec in specs]
    if len(set(spec_ids)) != len(spec_ids):
        raise ValueError("Planner produced duplicate sub-task IDs")
    unknown = sorted({str(dep) for spec in specs for dep in spec.get("depends_on", []) if str(dep) not in spec_ids})
    if unknown:
        raise ValueError(f"Planner dependencies reference unknown sub-task(s): {', '.join(unknown)}")
    remaining = {str(spec["spec_id"]): {str(dep) for dep in spec.get("depends_on", [])} for spec in specs}
    resolved: set[str] = set()
    while remaining:
        ready = [spec_id for spec_id, dependencies in remaining.items() if dependencies <= resolved]
        if not ready:
            raise ValueError("Planner produced a cyclic sub-task dependency graph")
        resolved.update(ready)
        for spec_id in ready:
            remaining.pop(spec_id)

    with SessionLocal() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                   {"key": coordinator.ticket_id})
        existing = [row for row in db.scalars(select(Subtask).where(Subtask.ticket_id == ticket_id)).all()
                    if (row.state or {}).get("parent_subtask_id") == coordinator.subtask_id]
        if existing:
            return [SubtaskState.model_validate(row.state) for row in existing]

        ids = {spec_id: uuid.uuid4() for spec_id in spec_ids}
        states: list[SubtaskState] = []
        for index, spec in enumerate(specs):
            dependencies = [str(ids[str(dep)]) for dep in spec.get("depends_on", [])]
            state = SubtaskState(
                ticket_id=coordinator.ticket_id,
                jira_key=coordinator.jira_key,
                subtask_id=str(ids[str(spec["spec_id"])]),
                subtask_type=spec["type"],
                description=spec["description"],
                execution_constraints=inherited_ticket_constraints(coordinator),
                repo=spec["repo"],
                confirmed_repos=[spec["repo"]],
                depends_on=dependencies,
                orchestration_role="work",
                spec_id=str(spec["spec_id"]),
                orchestration_index=index,
                parent_subtask_id=coordinator.subtask_id,
                prior_attempt=copy.deepcopy(coordinator.prior_attempt),
            )
            states.append(state)
            db.add(Subtask(id=ids[str(spec["spec_id"])], ticket_id=ticket_id, type=spec["type"],
                           description=spec["description"], status="queued",
                           depends_on=dependencies, state=state.model_dump(mode="json")))
        parent = db.get(Subtask, parent_id)
        if parent:
            parent.status = "done"
            parent.state = coordinator.model_copy(update={"status": "done"}).model_dump(mode="json")
        ticket = db.get(Ticket, ticket_id)
        if ticket:
            ticket.status = "processing"
        db.commit()
        return states


async def materialize_and_advance(coordinator: SubtaskState) -> list[SubtaskState]:
    created = await asyncio.to_thread(materialize_subtasks, coordinator)
    await asyncio.to_thread(RepoTool().cleanup_workspace, coordinator.subtask_id)
    await log_event(ticket_id=coordinator.ticket_id, subtask_id=coordinator.subtask_id,
                    agent="orchestrator", stage="subtasks_created",
                    message=f"Created {len(created)} isolated sub-tasks")
    await advance_ticket(coordinator.ticket_id)
    # `advance_ticket` runs and persists separate role="work" blackboards. Never
    # return the pre-run objects created by materialize_subtasks: those cannot
    # contain later Diagnosis/Executor/Critic/publication results.
    return await asyncio.to_thread(
        _work_states, coordinator.ticket_id, coordinator.subtask_id
    )


def _work_states(ticket_id: str, parent_subtask_id: str | None = None) -> list[SubtaskState]:
    """Reload durable work blackboards in Planner order."""
    with SessionLocal() as db:
        rows = list(db.scalars(select(Subtask).where(
            Subtask.ticket_id == uuid.UUID(str(ticket_id))
        ).order_by(Subtask.created_at, Subtask.id)))
        states = [SubtaskState.model_validate(row.state) for row in rows if row.state and
                  row.state.get("orchestration_role") == "work" and
                  (parent_subtask_id is None or row.state.get("parent_subtask_id") == parent_subtask_id)]
    return sorted(states, key=lambda state: (
        state.orchestration_index if state.orchestration_index is not None else 10**9,
        state.subtask_id,
    ))


async def advance_ticket(
    ticket_id: str,
    *,
    runner: Callable[[SubtaskState], Awaitable[SubtaskState]] | None = None,
    repo_tool: RepoTool | None = None,
) -> SubtaskState | None:
    """Run each dependency-ready wave concurrently; each graph remains isolated."""
    if runner is None:
        from app.orchestrator.graph import run_diagnosis_graph
        runner = run_diagnosis_graph
    repo_tool = repo_tool or RepoTool()
    while True:
        states, blocked = await asyncio.to_thread(_claim_ready, ticket_id)
        for blocked_state in blocked:
            await log_event(ticket_id=ticket_id, subtask_id=blocked_state.subtask_id,
                            agent="orchestrator", stage="dependency_blocked",
                            message=blocked_state.failure_reason or "Dependency failed")
            from app.memory.store import write_resolution
            await asyncio.to_thread(write_resolution, blocked_state, "escalated")
        if not states:
            await _integrate_if_ready(ticket_id)
            return _first_active(ticket_id)

        async def run_one(state: SubtaskState) -> SubtaskState:
            await log_event(ticket_id=ticket_id, subtask_id=state.subtask_id,
                            agent="orchestrator", stage="subtask_started",
                            message=f"Starting sub-task {state.spec_id}: {state.description}")
            try:
                await asyncio.to_thread(repo_tool.clone_or_pull, state.repo, state.subtask_id)
                loop = asyncio.get_running_loop()

                def stream_index(record):
                    future = asyncio.run_coroutine_threadsafe(
                        log_event(
                            ticket_id=ticket_id,
                            subtask_id=state.subtask_id,
                            agent="repo_index",
                            stage=record["stage"],
                            message=json.dumps(record),
                        ),
                        loop,
                    )
                    future.result()

                # Lightweight scheduler test doubles may implement clone only.
                # A production RepoTool always exposes revision/checkout methods.
                if hasattr(repo_tool, "revision") and hasattr(repo_tool, "_checkout_path"):
                    from app.repo_intelligence.indexer import RepositoryIndexer
                    snapshot = await asyncio.to_thread(
                        RepositoryIndexer(repo_tool, event_sink=stream_index).ensure,
                        state.repo,
                        state.subtask_id,
                    )
                    state.repo_snapshot_id = str(snapshot.id)
                result = await runner(state)
            except Exception as exc:
                result = state
                result.status = "needs_human"
                result.failure_reason = describe_failure(f"Running sub-task {state.spec_id}", exc)
                from app.memory.store import write_resolution
                await asyncio.to_thread(write_resolution, result, "escalated")
            from app.web.approval import persist_state
            await asyncio.to_thread(persist_state, result)
            return result

        results = await asyncio.gather(*(run_one(state) for state in states))
        # Completed siblings may unlock another dependency wave immediately. If
        # every result paused, the next claim returns empty and we yield to humans.
        if results and all(result.status in ACTIVE for result in results):
            return results[0]


async def _integrate_if_ready(ticket_id: str) -> None:
    with SessionLocal() as db:
        rows = list(db.scalars(select(Subtask).where(Subtask.ticket_id == uuid.UUID(str(ticket_id)))))
        work = [row for row in rows if (row.state or {}).get("orchestration_role") == "work"]
        work.sort(key=lambda row: ((row.state or {}).get("orchestration_index", 10**9), str(row.id)))
        ready = bool(work) and not any(row.status in ACTIVE | {"queued"} for row in work)
        pending = any(row.status == "integration_pending" for row in work)
    if ready and pending:
        from app.core.integration import integrate
        await integrate(ticket_id)


def _claim_ready(ticket_id: str) -> tuple[list[SubtaskState], list[SubtaskState]]:
    parsed = uuid.UUID(str(ticket_id))
    blocked: list[SubtaskState] = []
    with SessionLocal() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": str(parsed)})
        rows = list(db.scalars(select(Subtask).where(Subtask.ticket_id == parsed)
                               .order_by(Subtask.created_at, Subtask.id)))
        work = [row for row in rows if (row.state or {}).get("orchestration_role") == "work"]
        work.sort(key=lambda row: ((row.state or {}).get("orchestration_index", 10**9), str(row.id)))
        by_id = {str(row.id): row for row in work}
        for row in work:
            if row.status != "queued":
                continue
            failed_dependencies = [dep for dep in row.depends_on or []
                                   if dep in by_id and by_id[dep].status in FAILURE]
            if failed_dependencies:
                state = SubtaskState.model_validate(row.state)
                state.status = "needs_human"
                state.failure_reason = "Blocked because a required sub-task did not complete successfully: " + ", ".join(failed_dependencies)
                row.status, row.state = state.status, state.model_dump(mode="json")
                blocked.append(state)
        candidates = [row for row in work if row.status == "queued" and
                      all(by_id.get(dep) and by_id[dep].status in SUCCESS for dep in row.depends_on or [])]
        if not candidates:
            _set_ticket_aggregate(db, parsed, work)
            db.commit()
            return [], blocked
        states = []
        for candidate in candidates:
            state = SubtaskState.model_validate(candidate.state)
            state.status = "running"
            candidate.status, candidate.state = "running", state.model_dump(mode="json")
            states.append(state)
        ticket = db.get(Ticket, parsed)
        if ticket:
            ticket.status = "processing"
        db.commit()
        return states, blocked


def _first_active(ticket_id: str) -> SubtaskState | None:
    with SessionLocal() as db:
        rows = db.scalars(select(Subtask).where(
            Subtask.ticket_id == uuid.UUID(str(ticket_id)), Subtask.status.in_(ACTIVE),
        ).order_by(Subtask.created_at, Subtask.id)).all()
        return SubtaskState.model_validate(rows[0].state) if rows and rows[0].state else None


def _set_ticket_aggregate(db, ticket_id: uuid.UUID, work: list[Subtask]) -> None:
    ticket = db.get(Ticket, ticket_id)
    if not ticket or not work:
        return
    statuses = [row.status for row in work]
    success = any(status in SUCCESS for status in statuses)
    failure = any(status in FAILURE for status in statuses)
    if success and failure:
        ticket.status = "mixed"
    elif failure:
        ticket.status = "needs_human"
    elif any(status == "integration_pending" for status in statuses):
        ticket.status = "processing"
    elif any(status == "in_review" for status in statuses):
        ticket.status = "in_review"
    elif all(status == "done" for status in statuses):
        ticket.status = "done"
    else:
        ticket.status = "processing"
