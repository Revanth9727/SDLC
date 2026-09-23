"""The single PR publication boundary for every workflow (R-53)."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.agents.state import SubtaskState
from app.core.failures import describe_failure
from app.db.connection import SessionLocal
from app.db.models import PRLink, Subtask, Ticket
from app.events import log_event
from app.tools.github_tool import GitHubTool
from app.tools.jira_tool import JiraTool
from app.tools.repo_tool import RepoTool


@dataclass(frozen=True)
class OpenPR:
    repo: str
    number: int
    url: str
    state: str
    title: str = ""

    @property
    def summary(self) -> str:
        title = f' "{self.title}"' if self.title else ""
        return (f"PR #{self.number}{title} is {self.state}: {self.url}. "
                "Keep it, or choose Replace PR to close it and create a fresh PR?")


class OpenPRBlocked(RuntimeError):
    def __init__(self, existing: OpenPR):
        self.existing = existing
        super().__init__(existing.summary)


class PublishArtifact(BaseModel):
    """The exact per-repository change proven by whole-ticket integration."""

    model_config = ConfigDict(extra="forbid")
    repo: str = Field(min_length=3)
    base_commit: str = Field(min_length=1)
    file_changes: dict[str, str | None] = Field(min_length=1)
    subtask_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def one_repo_artifact(self):
        self.subtask_ids = list(dict.fromkeys(self.subtask_ids))
        return self


def find_open_pr(ticket_id: str | uuid.UUID, github: GitHubTool | None = None) -> OpenPR | None:
    """Read the Phase 5.5 matrix; refresh the candidate to avoid stale open state."""
    with SessionLocal() as db:
        ticket = db.get(Ticket, uuid.UUID(str(ticket_id)))
        if not ticket:
            return None
        row = db.scalar(select(PRLink).where(
            PRLink.jira_issue_key == ticket.external_key,
            PRLink.pr_state == "open",
        ).order_by(PRLink.updated_at.desc()))
        if not row:
            return None
        repo, number, url = row.repo, row.number, row.url
    snapshot = (github or GitHubTool()).pr_snapshot(repo, number)
    with SessionLocal() as db:
        current = db.get(PRLink, str(snapshot["id"]))
        if current:
            current.pr_state = snapshot["state"]
            db.commit()
    if snapshot["state"] != "open":
        return None
    return OpenPR(repo=repo, number=number, url=snapshot.get("url", url), state="open",
                  title=snapshot.get("title", ""))


async def preflight(ticket_id: str | uuid.UUID, *, jira: JiraTool | None = None,
                    github: GitHubTool | None = None, notify: bool = True) -> OpenPR | None:
    existing = await asyncio.to_thread(find_open_pr, ticket_id, github)
    if not existing or not notify:
        return existing
    with SessionLocal() as db:
        ticket = db.get(Ticket, uuid.UUID(str(ticket_id)))
        key = ticket.external_key if ticket else None
    if key:
        await asyncio.to_thread((jira or JiraTool()).comment, key, existing.summary)
    await log_event(ticket_id=str(ticket_id), agent="publish", stage="open_pr_blocked",
                    message=existing.summary)
    return existing


async def publish(ticket: Ticket, states: list[SubtaskState], *, publisher: GitHubTool | None = None,
                  jira: JiraTool | None = None, repo_tool: RepoTool | None = None,
                  transition_ticket: bool = True,
                  artifacts: list[PublishArtifact] | None = None) -> list[str]:
    """Publish one tested artifact per repo, centralizing every PR side effect."""
    github, jira_tool, tool = publisher or GitHubTool(), jira or JiraTool(), repo_tool or RepoTool()
    existing = await preflight(ticket.id, jira=jira_tool, github=github)
    if existing:
        raise OpenPRBlocked(existing)
    if not states:
        return []
    with SessionLocal() as db:
        durable_ticket = db.get(Ticket, ticket.id) is not None
    by_id = {state.subtask_id: state for state in states}
    if artifacts is None:
        if len(states) != 1:
            raise ValueError("Publishing multiple sub-tasks requires integration-tested repository artifacts")
        publish_artifacts = [_artifact_from_state(states[0])]
    else:
        publish_artifacts = [PublishArtifact.model_validate(artifact) for artifact in artifacts]
    urls: list[str] = []
    for artifact in publish_artifacts:
        artifact_states = [by_id[subtask_id] for subtask_id in artifact.subtask_ids if subtask_id in by_id]
        if len(artifact_states) != len(artifact.subtask_ids):
            raise ValueError(f"Publish artifact for {artifact.repo} references an unknown sub-task")
        if any(state.repo != artifact.repo for state in artifact_states):
            raise ValueError(f"Publish artifact for {artifact.repo} contains a sub-task from another repo")
        publish_state = _materialize_publish_state(ticket, artifact, artifact_states)
        result = await asyncio.to_thread(github.publish_changes, publish_state)
        url = result["url"]
        urls.append(url)
        jira_key = next((state.jira_key for state in artifact_states if state.jira_key), None)
        if jira_key and durable_ticket:
            from app.core.pr_sync import record_pr
            await asyncio.to_thread(record_pr, jira_key, result)
        if jira_key:
            await asyncio.to_thread(
                jira_tool.comment, jira_key,
                f"Integrated PR ready for review ({len(artifact_states)} sub-task(s), {artifact.repo}): {url}",
            )
        for state in artifact_states:
            state.pr_url, state.status = url, "in_review"
            state.integrated_file_changes = dict(artifact.file_changes)
            state.integrated_base_commit = artifact.base_commit
            state.integrated_subtask_ids = list(artifact.subtask_ids)
            state.integration_verified = True
            _save_state(state)
            await asyncio.to_thread(tool.cleanup_workspace, state.subtask_id)
            if durable_ticket:
                try:
                    from app.tools.memory import write_back
                    await asyncio.to_thread(write_back, state)
                except Exception as exc:
                    await log_event(ticket_id=state.ticket_id, subtask_id=state.subtask_id,
                                    agent="memory", stage="warning",
                                    message=describe_failure("Writing back sub-task memory", exc))
        await log_event(ticket_id=str(ticket.id), subtask_id=publish_state.subtask_id,
                        agent="github", stage="pr_opened",
                        message=f"Integrated PR opened for {artifact.repo}: {url}")
    if transition_ticket and ticket.external_key:
        await asyncio.to_thread(jira_tool.set_status, ticket.external_key, "in_review")
    with SessionLocal() as db:
        local = db.get(Ticket, ticket.id)
        if local:
            local.status = "in_review" if transition_ticket else "mixed"
            db.commit()
    return urls


async def redo(ticket_id: str | uuid.UUID, *, publisher: GitHubTool | None = None,
               jira: JiraTool | None = None, repo_tool: RepoTool | None = None) -> list[str]:
    """Close the current PR and republish its tested state through this same boundary."""
    github = publisher or GitHubTool()
    existing = await preflight(ticket_id, github=github, notify=False)
    if not existing:
        raise ValueError("This ticket has no open PR to replace")
    with SessionLocal() as db:
        ticket = db.get(Ticket, uuid.UUID(str(ticket_id)))
        rows = list(db.scalars(select(Subtask).where(
            Subtask.ticket_id == ticket.id,
            Subtask.state["pr_url"].astext == existing.url,
        )))
        if not rows or not rows[0].state:
            raise LookupError("The open PR has no publishable sub-task state")
        states = [SubtaskState.model_validate(row.state) for row in rows if row.state]
        db.expunge(ticket)
    await asyncio.to_thread(github.close_pr, existing.repo, existing.number)
    with SessionLocal() as db:
        link = db.scalar(select(PRLink).where(PRLink.url == existing.url))
        if link:
            link.pr_state = "closed"
            db.commit()
    for state in states:
        state.status = "integration_pending"
        state.pr_url = None
        _save_state(state)
    artifact = _artifact_from_state(states[0])
    await log_event(ticket_id=str(ticket_id), subtask_id=states[0].subtask_id,
                    agent="publish", stage="pr_replacing",
                    message=f"Closed PR #{existing.number}; creating its replacement")
    return await publish(ticket, states, publisher=github, jira=jira,
                         repo_tool=repo_tool, transition_ticket=True, artifacts=[artifact])


def _artifact_from_state(state: SubtaskState) -> PublishArtifact:
    changes = state.integrated_file_changes or state.file_changes
    base_commit = state.integrated_base_commit or state.base_commit
    subtask_ids = state.integrated_subtask_ids or [state.subtask_id]
    return PublishArtifact(repo=state.repo, base_commit=base_commit or "",
                           file_changes=changes, subtask_ids=subtask_ids)


def _materialize_publish_state(ticket: Ticket, artifact: PublishArtifact,
                               states: list[SubtaskState]) -> SubtaskState:
    """Build the GitHub input only from the validated, integration-tested artifact."""
    representative = states[0].model_copy(deep=True)
    representative.description = ticket.title or representative.description
    representative.base_commit = artifact.base_commit
    representative.file_changes = dict(artifact.file_changes)
    representative.integrated_file_changes = dict(artifact.file_changes)
    representative.integrated_base_commit = artifact.base_commit
    representative.integrated_subtask_ids = list(artifact.subtask_ids)
    representative.integration_verified = True
    representative.execution_complete = True
    representative.approval_status = "approved"
    representative.plan = [step for state in states for step in state.plan]
    representative.steps_done = [step for state in states for step in state.steps_done]
    representative.status = "integration_pending"
    representative.pr_url = None
    return representative


def _save_state(state: SubtaskState) -> None:
    with SessionLocal() as db:
        row = db.get(Subtask, uuid.UUID(state.subtask_id))
        if row:
            row.status, row.state = state.status, state.model_dump(mode="json")
            db.commit()
