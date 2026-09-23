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
from app.agents.critic import CriticAgent, CriticVerdict
from app.agents.llm import LLMClient
from app.agents.router import model_tier
from app.config import settings
from app.core.failures import describe_failure, failure_context, failure_summary
from app.core.code_impact import SymbolImpact, inspect_symbols, merge_impacts
from app.db.connection import SessionLocal
from app.db.models import Subtask, Ticket
from app.events import log_event
from app.tools.github_tool import GitHubTool
from app.tools.jira_tool import JiraTool
from app.tools.repo_tool import RepoTool
from app.tools.code_search import CodeSearchTool
from app.tools.test_runner import TestResult, run_tests
from app.tools.test_preflight import clear_preflight, predict_test_credentials, remember_preflight


class IntegrationRepoResult(BaseModel):
    repo: str
    subtask_ids: list[str]
    baseline: str
    combined: str
    passed: bool
    report: str = ""
    interaction: bool = False
    reconciliation_attempts: int = 0


class IntegrationResult(BaseModel):
    ticket_id: str
    passed: bool
    repos: list[IntegrationRepoResult] = Field(default_factory=list)
    pr_urls: list[str] = Field(default_factory=list)
    report: str = ""


class ReconciliationCandidate(BaseModel):
    """One bounded proposal for a cleanly-merged but behaviorally failing tree."""

    ambiguous: bool = False
    reasoning: str
    file_changes: dict[str, str | None] = Field(default_factory=dict)


class MergeToolError(RuntimeError):
    """The deterministic merge program failed to run; this is not a text conflict."""


async def integrate(
    ticket: str | uuid.UUID | Ticket,
    *,
    repo_tool: RepoTool | None = None,
    test_runner: Callable[[Path], TestResult] = run_tests,
    publisher: GitHubTool | None = None,
    jira: JiraTool | None = None,
    code_search_factory=None,
    reconciler=None,
    critic=None,
    human_intent: str | None = None,
    rejected_subtask_ids: set[str] | None = None,
) -> IntegrationResult:
    """Assemble every integration-ready change, test it together, then publish."""
    ticket_id = str(ticket.id if isinstance(ticket, Ticket) else ticket)
    states, ticket_row = _load_ticket(ticket_id)
    rejected_subtask_ids = rejected_subtask_ids or set()
    candidates = [state for state in states if state.status == "integration_pending"
                  and state.subtask_id not in rejected_subtask_ids]
    candidates.sort(key=_state_order)
    unsettled = [state for state in states if state.status in {"running", "queued"}]
    if unsettled or not candidates:
        raise ValueError("Integration requires all runnable sub-tasks to be settled")

    tool = repo_tool or RepoTool()
    github = publisher or GitHubTool()
    jira_tool = jira or JiraTool()
    await log_event(ticket_id=ticket_id, agent="integration", stage="started",
                    message=f"Integrating {len(candidates)} completed sub-task(s) across {len({s.repo for s in candidates})} repo(s)")
    repo_results: list[IntegrationRepoResult] = []
    publish_artifacts = []
    workspaces: list[str] = []
    try:
        loop = asyncio.get_running_loop()
        for repo in sorted({state.repo for state in candidates}):
            repo_states = sorted((state for state in candidates if state.repo == repo), key=_state_order)
            requires_final_review = (len(repo_states) > 1 or human_intent is not None
                                     or bool(rejected_subtask_ids))
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

            predicted = predict_test_credentials(checkout)
            for state in repo_states:
                remember_preflight(state.subtask_id, predicted)
            baseline = test_runner(checkout)
            baseline_label = _outcome(baseline)
            owners: dict[str, list[str]] = {}
            paths = sorted({path for state in repo_states for path in state.file_changes})
            base_contents = {path: _read_optional(tool.execution_file(checkout, path)) for path in paths}
            conflict = ""
            for state in repo_states:
                try:
                    _merge_state(checkout, state, owners, tool, base_contents)
                except MergeConflict as exc:
                    implicated = owners.get(getattr(exc, "path", ""), []) + [state.spec_id or state.subtask_id]
                    conflict = (f"Combined-change conflict in {repo}: {exc}. "
                                f"Implicated sub-tasks: {', '.join(dict.fromkeys(implicated))}")
                    break
            if conflict:
                _record_integration_issue(repo_states, kind="textual", report=conflict,
                                          files=list(owners), attempts=[])
                repo_results.append(_failed_repo(repo, repo_states, baseline_label, conflict))
                continue

            # Freeze a candidate before verification. Only the candidate that later
            # passes both the full suite and final Critic may enter publish (R-31b).
            from app.core.publish import PublishArtifact
            integrated_changes = {path: _read_optional(tool.execution_file(checkout, path)) for path in paths}
            artifact = PublishArtifact(
                repo=repo,
                base_commit=current_commit,
                file_changes=integrated_changes,
                subtask_ids=[state.subtask_id for state in repo_states],
            )
            combined = test_runner(checkout)
            combined_label = _outcome(combined)
            passed = combined.verification_outcome == "PASS"
            interaction = False
            attempts: list[dict] = []
            if combined.verification_outcome != "UNVERIFIABLE":
                for state in repo_states:
                    clear_preflight(state.subtask_id)
            if not passed and combined.verification_outcome == "FAIL" and len(repo_states) > 1:
                interaction = True
                _record_interaction(repo_states, repo, combined)
                artifact, combined, attempts, verdict = await _reconcile(
                    ticket_id=ticket_id, repo=repo, checkout=checkout, tool=tool,
                    states=repo_states, artifact=artifact, failed=combined,
                    test_runner=test_runner, reconciler=reconciler, critic=critic,
                    human_intent=human_intent,
                )
                combined_label = _outcome(combined)
                passed = combined.verification_outcome == "PASS" and verdict is not None and verdict.approved
            elif passed and requires_final_review:
                verdict = await _review_integrated_artifact(
                    repo_states, artifact, critic,
                    evidence=_integration_review_evidence(combined, [], combined),
                )
                passed = verdict.approved
                if not passed:
                    interaction = True
                    _record_interaction(repo_states, repo, combined, verdict.issues)
                    artifact, combined, attempts, verdict = await _reconcile(
                        ticket_id=ticket_id, repo=repo, checkout=checkout, tool=tool,
                        states=repo_states, artifact=artifact, failed=combined,
                        test_runner=test_runner, reconciler=reconciler, critic=critic,
                        human_intent=human_intent, critic_feedback=verdict.issues,
                    )
                    combined_label = _outcome(combined)
                    passed = combined.verification_outcome == "PASS" and verdict is not None and verdict.approved
            if not passed:
                implicated = ("; ".join(dict.fromkeys(graph_risks)) if graph_risks else
                              ", ".join(state.spec_id or state.subtask_id for state in repo_states))
                if combined.verification_outcome == "UNVERIFIABLE":
                    regression = "combined suite is UNVERIFIABLE"
                    for state in repo_states:
                        state.verifiability = "unverifiable"
                        state.required_test_credentials = combined.required_credentials
                        state.verification_summary = {"outcome": "UNVERIFIABLE", "reason": combined.reason,
                                                      "facts": combined.facts}
                        _save_state(state)
                elif interaction:
                    regression = "behavioral interaction could not be reconciled unambiguously"
                else:
                    regression = "previously-passing tests now fail" if baseline.verification_outcome == "PASS" else "combined suite does not pass"
                report = (f"Integration failed in {repo}: {regression} (exit {combined.returncode}). "
                           f"Graph-attributed impact: {implicated}. {combined.output}").strip()
                _record_integration_issue(repo_states, kind="behavioral" if interaction else "verification",
                                          report=report, files=paths, attempts=attempts)
            else:
                report = (f"Full suite and final Critic passed for the combined change in {repo}."
                          if requires_final_review else
                          f"Full suite passed for the combined change in {repo}.")
                publish_artifacts.append(artifact)
            repo_results.append(IntegrationRepoResult(repo=repo,
                subtask_ids=[state.subtask_id for state in repo_states], baseline=baseline_label,
                combined=combined_label, passed=passed, report=report, interaction=interaction,
                reconciliation_attempts=len(attempts)))
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
                                repo_tool=tool, transition_ticket=not has_failed_subtasks,
                                artifacts=publish_artifacts)
        await log_event(ticket_id=ticket_id, agent="integration", stage="passed",
                        message=f"Integration passed; opened {len(pr_urls)} tested repository PR(s)")
        return IntegrationResult(ticket_id=ticket_id, passed=True, repos=repo_results,
                                 pr_urls=pr_urls, report="All combined repository suites passed")
    except Exception as exc:
        context = failure_context(
            exc, classification="infrastructure", component="integration",
            operation="merge_verify_publish", reason="deterministic_integration_operation_failed",
            identifiers={"ticket_id": ticket_id},
        )
        for state in candidates:
            state.failure_contexts.append(context)
            _save_state(state)
        report = failure_summary(context)
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


async def _reconcile(*, ticket_id: str, repo: str, checkout: Path, tool: RepoTool,
                     states: list[SubtaskState], artifact, failed: TestResult,
                     test_runner, reconciler=None, critic=None,
                     human_intent: str | None = None,
                     critic_feedback: list[str] | None = None):
    """Bounded reasoning over a deterministic clean merge; every candidate is re-proven."""
    attempts: list[dict] = []
    current = artifact
    result = failed
    original_failure = failed
    verdict = None
    llm = None if reconciler else LLMClient(ticket_id=ticket_id)
    expected_paths = set(artifact.file_changes)
    for number in range(1, settings.max_agent_retries + 1):
        facts = {
            "repo": repo,
            "approved_subtasks": [
                {"id": state.subtask_id, "spec_id": state.spec_id,
                 "intent": state.description, "plan": [step.model_dump() for step in state.plan]}
                for state in states
            ],
            "combined_candidate": current.file_changes,
            "combined_test": {"exit_code": result.returncode, "outcome": result.verification_outcome,
                              "output": result.output},
            "critic_feedback": critic_feedback or [],
            "human_intent": human_intent,
            "allowed_paths": sorted(expected_paths),
        }
        if reconciler:
            proposal = reconciler(facts, number)
            if asyncio.iscoroutine(proposal):
                proposal = await proposal
            proposal = ReconciliationCandidate.model_validate(proposal)
        else:
            proposal = await asyncio.to_thread(
                llm.complete_json, _RECONCILIATION_PROMPT, _json(facts),
                ReconciliationCandidate, tier=model_tier("reconciliation"), ticket_id=ticket_id,
            )
            proposal = ReconciliationCandidate.model_validate(proposal)
        record = {"attempt": number, "reasoning": proposal.reasoning,
                  "ambiguous": proposal.ambiguous}
        attempts.append(record)
        await log_event(ticket_id=ticket_id, agent="integration", stage="reconciliation_attempt",
                        message=f"{repo} reconciliation {number}: {proposal.reasoning}")
        if proposal.ambiguous:
            break
        if set(proposal.file_changes) != expected_paths:
            record["rejected"] = "candidate changed the approved path set"
            critic_feedback = [record["rejected"]]
            continue
        record["delta"] = _artifact_delta(current.file_changes, proposal.file_changes)
        for path in sorted(expected_paths):
            target = tool.execution_file(checkout, path)
            content = proposal.file_changes[path]
            if content is None:
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        current = current.model_copy(update={"file_changes": dict(proposal.file_changes)})
        result = test_runner(checkout)
        record["verification"] = {
            "outcome": result.verification_outcome,
            "exit_code": result.returncode,
            "output": result.output,
        }
        if result.verification_outcome != "PASS":
            critic_feedback = []
            continue
        verdict = await _review_integrated_artifact(
            states, current, critic,
            evidence=_integration_review_evidence(original_failure, attempts, result),
        )
        record["critic_approved"] = verdict.approved
        if verdict.approved:
            return current, result, attempts, verdict
        critic_feedback = list(dict.fromkeys(verdict.test_issues + verdict.issues))
    return current, result, attempts, verdict


async def _review_integrated_artifact(states: list[SubtaskState], artifact, critic=None,
                                      *, evidence: dict | None = None) -> CriticVerdict:
    """Critic-review the exact file map that verification just exercised."""
    review = states[0].model_copy(deep=True)
    review.description = "\n".join(state.description for state in states)
    review.plan = [step for state in states for step in state.plan]
    review.steps_done = [step for state in states for step in state.steps_done]
    review.file_changes = dict(artifact.file_changes)
    review.integrated_file_changes = dict(artifact.file_changes)
    review.integrated_base_commit = artifact.base_commit
    review.integrated_subtask_ids = list(artifact.subtask_ids)
    review.integration_verified = True
    review.integration_review_evidence = evidence
    review.code_impacts = []
    reviewer = critic or CriticAgent()
    updated = await asyncio.to_thread(reviewer.run, review) if hasattr(reviewer, "run") \
        else await asyncio.to_thread(reviewer, review)
    verdict = CriticVerdict.model_validate(updated.critic_verdict)
    for state in states:
        state.integration_review_evidence = evidence
        state.critic_verdict = verdict.model_dump(mode="json")
        _save_state(state)
    return verdict


def _integration_review_evidence(original: TestResult, attempts: list[dict], final: TestResult) -> dict:
    return {
        "original_combined_test_failure": _test_evidence(original),
        "reconciliation_attempts": attempts,
        "final_combined_test_result": _test_evidence(final),
    }


def _test_evidence(result: TestResult) -> dict:
    return {"outcome": result.verification_outcome, "exit_code": result.returncode,
            "output": result.output, "reason": result.reason}


def _artifact_delta(before: dict[str, str | None], after: dict[str, str | None]) -> list[dict]:
    """Compact explicit identity delta; source remains in final changed_files."""
    return [
        {
            "path": path,
            "before_sha256": _content_hash(before.get(path)),
            "after_sha256": _content_hash(after.get(path)),
            "operation": "delete" if after.get(path) is None else
                         "create" if before.get(path) is None else "edit",
        }
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path)
    ]


def _content_hash(content: str | None) -> str | None:
    return hashlib.sha256(content.encode()).hexdigest() if content is not None else None


def _record_interaction(states: list[SubtaskState], repo: str, result: TestResult,
                        critic_issues: list[str] | None = None) -> None:
    evidence = {
        "repo": repo,
        "originally_independent": not any(state.depends_on for state in states),
        "integration_interaction": True,
        "subtask_ids": [state.subtask_id for state in states],
        "verification_outcome": result.verification_outcome,
        "verification_reason": result.reason or result.output,
        "verification_output": result.output,
        "files": sorted({path for state in states for path in state.file_changes}),
        "symbols": sorted({
            SymbolImpact.model_validate(raw).symbol
            for state in states for raw in state.code_impacts
        }),
        "critic_issues": critic_issues or [],
    }
    for state in states:
        if evidence not in state.integration_evidence:
            state.integration_evidence.append(evidence)
        _save_state(state)


def _record_integration_issue(states: list[SubtaskState], *, kind: str, report: str,
                              files: list[str], attempts: list[dict]) -> None:
    issue = {
        "kind": kind,
        "subtask_ids": [state.subtask_id for state in states],
        "subtasks": [{"id": state.subtask_id, "spec_id": state.spec_id,
                      "intent": state.description} for state in states],
        "files": sorted(files),
        "symbols": sorted({
            SymbolImpact.model_validate(raw).symbol
            for state in states for raw in state.code_impacts
        }),
        "report": report,
        "attempts": attempts,
        "actions": ["resolve_manually", "state_intended_behavior", "reject_change", "replan"],
    }
    for state in states:
        state.integration_issue = issue
        _save_state(state)


def _state_order(state: SubtaskState) -> tuple[int, str, str]:
    return (state.orchestration_index if state.orchestration_index is not None else 2**31,
            state.spec_id or "", state.subtask_id)


def _json(value: object) -> str:
    import json
    return json.dumps(value, sort_keys=True)


_RECONCILIATION_PROMPT = """Reconcile a cleanly text-merged but behaviorally failing integrated change.
Use only the approved intents, concrete test/critic evidence, and supplied source. Return one
complete file map for exactly allowed_paths. Do not add paths or broaden scope. If more than
one product behavior is plausible, set ambiguous=true and do not guess. This is bounded;
every non-ambiguous candidate will be fully re-tested and Critic-reviewed."""


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
        try:
            result = subprocess.run(["git", "merge-file", "-p", *map(str, files)],
                                    capture_output=True, text=True, check=False)
        except OSError as exc:
            raise MergeToolError(f"git merge-file could not run: {exc}") from exc
        if result.returncode == 0:
            return result.stdout
        if result.returncode == 1:
            return None
        raise MergeToolError(
            f"git merge-file failed with exit {result.returncode}: {result.stderr.strip()}"
        )


def _read_optional(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


async def _escalate(ticket: Ticket, states: list[SubtaskState], report: str,
                    jira: JiraTool, repo_tool: RepoTool) -> None:
    for state in states:
        state.status, state.failure_reason = "needs_human", f"Integration failed: {report}"
        _save_state(state)
        repo_tool.cleanup_workspace(state.subtask_id)
        # Do not invoke memory summarization here: textual-conflict detection and
        # its escalation path must remain fully deterministic and zero-LLM.
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
