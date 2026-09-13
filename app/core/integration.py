"""Whole-ticket integration gate between isolated Critic approval and PR publication."""
from __future__ import annotations

import asyncio
import hashlib
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agents.state import SubtaskState
from app.core.failures import describe_failure
from app.core.code_impact import SymbolImpact, inspect_symbols, merge_impacts
from app.db.connection import SessionLocal
from app.db.models import Subtask, Ticket
from app.events import log_event
from app.tools.github_tool import GitHubTool
from app.tools.jira_tool import JiraTool
from app.tools.repo_tool import RepoTool
from app.tools.code_search import CodeSearchTool
from app.tools.test_runner import TestResult, run_tests


class IntegrationRepoResult(BaseModel):
    repo: str
    subtask_ids: list[str]
    baseline: str
    combined: str
    passed: bool
    report: str = ""


class IntegrationResult(BaseModel):
    ticket_id: str
    passed: bool
    repos: list[IntegrationRepoResult] = Field(default_factory=list)
    pr_urls: list[str] = Field(default_factory=list)
    report: str = ""


async def integrate(
    ticket: str | uuid.UUID | Ticket,
    *,
    repo_tool: RepoTool | None = None,
    test_runner: Callable[[Path], TestResult] = run_tests,
    publisher: GitHubTool | None = None,
    jira: JiraTool | None = None,
    code_search_factory=None,
) -> IntegrationResult:
    """Assemble every integration-ready change, test it together, then publish."""
    ticket_id = str(ticket.id if isinstance(ticket, Ticket) else ticket)
    states, ticket_row = _load_ticket(ticket_id)
    candidates = [state for state in states if state.status == "integration_pending"]
    unsettled = [state for state in states if state.status in {"running", "queued"}]
    if unsettled or not candidates:
        raise ValueError("Integration requires all runnable sub-tasks to be settled")

    tool = repo_tool or RepoTool()
    github = publisher or GitHubTool()
    jira_tool = jira or JiraTool()
    await log_event(ticket_id=ticket_id, agent="integration", stage="started",
                    message=f"Integrating {len(candidates)} completed sub-task(s) across {len({s.repo for s in candidates})} repo(s)")
    repo_results: list[IntegrationRepoResult] = []
    workspaces: list[str] = []
    try:
        loop = asyncio.get_running_loop()
        for repo in dict.fromkeys(state.repo for state in candidates):
            repo_states = [state for state in candidates if state.repo == repo]
            graph_risks = []
            for state in repo_states:
                def stream_graph_tool(record, checked_state=state):
                    future = asyncio.run_coroutine_threadsafe(
                        log_event(
                            ticket_id=ticket_id, subtask_id=checked_state.subtask_id,
                            agent="integration", stage=record.get("tool", "graph_tool"),
                            message=str(record),
                        ),
                        loop,
                    )
                    future.result()

                search = (code_search_factory(state) if code_search_factory else
                          CodeSearchTool(repo_tool=tool, event_sink=stream_graph_tool))
                refreshed = []
                for raw in state.code_impacts[:8]:
                    prior = SymbolImpact.model_validate(raw)
                    checked = await asyncio.to_thread(
                        inspect_symbols, state, prior.target_file, [prior.symbol], search, limit=25
                    )
                    refreshed.extend(checked or [prior])
                if refreshed:
                    merge_impacts(state, refreshed)
                    _save_state(state)
                await log_event(
                    ticket_id=ticket_id, subtask_id=state.subtask_id, agent="integration",
                    stage="graph_checked",
                    message=f"Checked {len(refreshed)} changed symbol(s) against sibling sub-task dependencies",
                )
            graph_risks = cross_subtask_graph_risks(repo_states)
            if graph_risks:
                await log_event(
                    ticket_id=ticket_id, agent="integration", stage="graph_dependency",
                    message=f"Graph found cross-sub-task dependencies in {repo}: " +
                            "; ".join(dict.fromkeys(graph_risks)),
                )
            workspace_id = f"integration-{ticket_id}-{hashlib.sha256(repo.encode()).hexdigest()[:10]}"
            workspaces.append(workspace_id)
            tool.cleanup_workspace(workspace_id)
            checkout = tool.clone_or_pull(repo, workspace_id)
            commits = {state.base_commit for state in repo_states}
            current_commit = tool.revision(repo, workspace_id)
            if None in commits or commits != {current_commit}:
                message = (f"Freshness conflict in {repo}: sub-tasks were diagnosed at "
                           f"{sorted(str(item) for item in commits)}, current default is {current_commit}")
                repo_results.append(_failed_repo(repo, repo_states, "not_run", message))
                continue

            baseline = test_runner(checkout)
            baseline_label = _outcome(baseline)
            owners: dict[str, list[str]] = {}
            paths = {path for state in repo_states for path in state.file_changes}
            base_contents = {path: _read_optional(tool.execution_file(checkout, path)) for path in paths}
            conflict = ""
            for state in repo_states:
                try:
                    _merge_state(checkout, state, owners, tool, base_contents)
                except Exception as exc:
                    implicated = owners.get(getattr(exc, "path", ""), []) + [state.spec_id or state.subtask_id]
                    conflict = (f"Combined-change conflict in {repo}: {exc}. "
                                f"Implicated sub-tasks: {', '.join(dict.fromkeys(implicated))}")
                    break
            if conflict:
                repo_results.append(_failed_repo(repo, repo_states, baseline_label, conflict))
                continue

            combined = test_runner(checkout)
            combined_label = _outcome(combined)
            passed = combined.outcome in {"passed", "no_tests_collected"}
            if not passed:
                implicated = ("; ".join(dict.fromkeys(graph_risks)) if graph_risks else
                              ", ".join(state.spec_id or state.subtask_id for state in repo_states))
                regression = "previously-passing tests now fail" if baseline.outcome == "passed" else "combined suite does not pass"
                report = (f"Integration failed in {repo}: {regression} (exit {combined.returncode}). "
                          f"Graph-attributed impact: {implicated}. {combined.output}").strip()
            else:
                report = f"Full suite passed for the combined change in {repo}."
            repo_results.append(IntegrationRepoResult(repo=repo,
                subtask_ids=[state.subtask_id for state in repo_states], baseline=baseline_label,
                combined=combined_label, passed=passed, report=report))
            await log_event(ticket_id=ticket_id, agent="integration",
                            stage="repo_passed" if passed else "cross_breakage", message=report)

        failures = [result for result in repo_results if not result.passed]
        if failures:
            for result in failures:
                await log_event(ticket_id=ticket_id, agent="integration", stage="cross_breakage",
                                message=result.report)
            report = "\n".join(result.report for result in failures)
            await _escalate(ticket_row, candidates, report, jira_tool, tool)
            return IntegrationResult(ticket_id=ticket_id, passed=False, repos=repo_results, report=report)

        has_failed_subtasks = any(state.status in {"needs_human", "failed"} for state in states)
        from app.core.publish import publish
        pr_urls = await publish(ticket_row, candidates, publisher=github, jira=jira_tool,
                                repo_tool=tool, transition_ticket=not has_failed_subtasks)
        await log_event(ticket_id=ticket_id, agent="integration", stage="passed",
                        message=f"Integration passed; opened {len(pr_urls)} sub-task PR(s)")
        return IntegrationResult(ticket_id=ticket_id, passed=True, repos=repo_results,
                                 pr_urls=pr_urls, report="All combined repository suites passed")
    except Exception as exc:
        report = describe_failure("Running whole-ticket integration", exc)
        await _escalate(ticket_row, candidates, report, jira_tool, tool)
        return IntegrationResult(ticket_id=ticket_id, passed=False, repos=repo_results, report=report)
    finally:
        for workspace_id in workspaces:
            tool.cleanup_workspace(workspace_id)


class MergeConflict(ValueError):
    def __init__(self, path: str, message: str):
        self.path = path
        super().__init__(message)


def cross_subtask_graph_risks(states: list[SubtaskState]) -> list[str]:
    """Match verified dependents of one change to another sub-task's working slice."""
    risks = []
    for source in states:
        for impact_raw in source.code_impacts:
            impact = SymbolImpact.model_validate(impact_raw)
            related = {item.get("path") for item in impact.callers + impact.references if item.get("path")}
            for dependent in states:
                if dependent.subtask_id == source.subtask_id:
                    continue
                dependent_paths = set(dependent.file_changes)
                dependent_paths.update(dependent.code_context.get("relevant_files", []))
                overlap = sorted(related & dependent_paths)
                if overlap:
                    risks.append(
                        f"sub-task {source.spec_id or source.subtask_id} changes {impact.symbol}; "
                        f"sub-task {dependent.spec_id or dependent.subtask_id} relies on a verified dependent "
                        f"path ({', '.join(overlap)})"
                    )
    return list(dict.fromkeys(risks))


def _merge_state(checkout: Path, state: SubtaskState, owners: dict[str, list[str]],
                 tool: RepoTool, base_contents: dict[str, str | None]) -> None:
    for path, incoming in state.file_changes.items():
        target = tool.execution_file(checkout, path)
        base = base_contents[path]
        current = _read_optional(target)
        prior = owners.get(path, [])
        if current == incoming:
            owners.setdefault(path, []).append(state.spec_id or state.subtask_id)
            continue
        if incoming is None:
            if current == base:
                if target.exists():
                    target.unlink()
                owners.setdefault(path, []).append(state.spec_id or state.subtask_id)
                continue
            raise MergeConflict(path, f"sub-task {state.spec_id} deletes {path}, already changed by {', '.join(prior)}")
        if current == base:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(incoming, encoding="utf-8")
            owners.setdefault(path, []).append(state.spec_id or state.subtask_id)
            continue
        if base is None or current is None:
            raise MergeConflict(path, f"sub-task {state.spec_id} conflicts on {path}, already changed by {', '.join(prior)}")
        merged = _three_way_merge(current, base, incoming)
        if merged is None:
            raise MergeConflict(path, f"sub-task {state.spec_id} overlaps changes to {path} from {', '.join(prior)}")
        target.write_text(merged, encoding="utf-8")
        owners.setdefault(path, []).append(state.spec_id or state.subtask_id)


def _three_way_merge(current: str, base: str, incoming: str) -> str | None:
    with tempfile.TemporaryDirectory(prefix="sdlc-integration-") as directory:
        root = Path(directory)
        files = [root / name for name in ("current", "base", "incoming")]
        for path, content in zip(files, (current, base, incoming), strict=True):
            path.write_text(content, encoding="utf-8")
        result = subprocess.run(["git", "merge-file", "-p", *map(str, files)],
                                capture_output=True, text=True, check=False)
        return result.stdout if result.returncode == 0 else None


def _read_optional(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


async def _escalate(ticket: Ticket, states: list[SubtaskState], report: str,
                    jira: JiraTool, repo_tool: RepoTool) -> None:
    for state in states:
        state.status, state.failure_reason = "needs_human", f"Integration failed: {report}"
        _save_state(state)
        repo_tool.cleanup_workspace(state.subtask_id)
        from app.memory.store import write_resolution
        await asyncio.to_thread(write_resolution, state, "escalated")
    with SessionLocal() as db:
        local = db.get(Ticket, ticket.id)
        if local:
            local.status = "needs_human"
            db.commit()
    if ticket.external_key:
        try:
            detail = await asyncio.to_thread(jira.get_issue_detail, ticket.external_key)
            account_id = detail.get("assignee_id") or detail.get("reporter_id")
            display = detail.get("assignee_name") or detail.get("reporter_name") or "owner"
            await asyncio.to_thread(jira.comment_mentioning, ticket.external_key, account_id, display,
                                    f"Integration blocked PR publication. No PR is opened from a failing integration check. {report}")
            await asyncio.to_thread(jira.set_status, ticket.external_key, "blocked")
        except Exception as exc:
            await log_event(ticket_id=str(ticket.id), agent="integration", stage="jira_warning",
                            message=describe_failure("Escalating integration failure in Jira", exc))
    await log_event(ticket_id=str(ticket.id), agent="integration", stage="needs_human", message=report)


def _load_ticket(ticket_id: str) -> tuple[list[SubtaskState], Ticket]:
    parsed = uuid.UUID(str(ticket_id))
    with SessionLocal() as db:
        ticket = db.get(Ticket, parsed)
        if not ticket:
            raise LookupError(f"ticket {ticket_id} not found")
        rows = list(db.scalars(select(Subtask).where(Subtask.ticket_id == parsed).order_by(Subtask.created_at, Subtask.id)))
        states = [SubtaskState.model_validate(row.state) for row in rows
                  if row.state and row.state.get("orchestration_role") == "work"]
        db.expunge(ticket)
        return states, ticket


def _save_state(state: SubtaskState) -> None:
    with SessionLocal() as db:
        row = db.get(Subtask, uuid.UUID(state.subtask_id))
        if row:
            row.status, row.state = state.status, state.model_dump(mode="json")
            db.commit()


def _failed_repo(repo: str, states: list[SubtaskState], baseline: str, report: str) -> IntegrationRepoResult:
    return IntegrationRepoResult(repo=repo, subtask_ids=[state.subtask_id for state in states],
                                 baseline=baseline, combined="not_run", passed=False, report=report)


def _outcome(result: TestResult) -> str:
    return f"{result.outcome} (exit {result.returncode})"
