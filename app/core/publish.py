"""The single PR publication boundary for every workflow (R-53)."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

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
                  transition_ticket: bool = True) -> list[str]:
    """Publish at most one PR, with every post-publication side effect centralized."""
    github, jira_tool, tool = publisher or GitHubTool(), jira or JiraTool(), repo_tool or RepoTool()
    existing = await preflight(ticket.id, jira=jira_tool, github=github)
    if existing:
        raise OpenPRBlocked(existing)
    if not states:
        return []

    state = states[0]
    result = await asyncio.to_thread(github.publish_changes, state)
    state.pr_url, state.status = result["url"], "in_review"
    with SessionLocal() as db:
        durable_ticket = db.get(Ticket, ticket.id) is not None
    if state.jira_key and durable_ticket:
        from app.core.pr_sync import record_pr
        await asyncio.to_thread(record_pr, state.jira_key, result)
    if state.jira_key:
        await asyncio.to_thread(jira_tool.comment, state.jira_key,
                                f"Sub-task {state.spec_id or state.subtask_id} PR ready for review: {state.pr_url}")
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
    await log_event(ticket_id=state.ticket_id, subtask_id=state.subtask_id,
                    agent="github", stage="pr_opened", message=f"PR opened: {state.pr_url}")
    if transition_ticket and ticket.external_key:
        await asyncio.to_thread(jira_tool.set_status, ticket.external_key, "in_review")
    with SessionLocal() as db:
        local = db.get(Ticket, ticket.id)
        if local:
            local.status = "in_review" if transition_ticket else "mixed"
            db.commit()
    return [state.pr_url]


async def redo(ticket_id: str | uuid.UUID, *, publisher: GitHubTool | None = None,
               jira: JiraTool | None = None, repo_tool: RepoTool | None = None) -> list[str]:
    """Close the current PR and republish its tested state through this same boundary."""
    github = publisher or GitHubTool()
    existing = await preflight(ticket_id, github=github, notify=False)
    if not existing:
        raise ValueError("This ticket has no open PR to replace")
    with SessionLocal() as db:
        ticket = db.get(Ticket, uuid.UUID(str(ticket_id)))
        row = db.scalar(select(Subtask).where(
            Subtask.ticket_id == ticket.id,
            Subtask.state["pr_url"].astext == existing.url,
        ))
        if not row or not row.state:
            raise LookupError("The open PR has no publishable sub-task state")
        state = SubtaskState.model_validate(row.state)
        db.expunge(ticket)
    await asyncio.to_thread(github.close_pr, existing.repo, existing.number)
    with SessionLocal() as db:
        link = db.scalar(select(PRLink).where(PRLink.url == existing.url))
        if link:
            link.pr_state = "closed"
            db.commit()
    state.status = "integration_pending"
    state.pr_url = None
    _save_state(state)
    await log_event(ticket_id=str(ticket_id), subtask_id=state.subtask_id,
                    agent="publish", stage="pr_replacing",
                    message=f"Closed PR #{existing.number}; creating its replacement")
    return await publish(ticket, [state], publisher=github, jira=jira,
                         repo_tool=repo_tool, transition_ticket=True)


def _save_state(state: SubtaskState) -> None:
    with SessionLocal() as db:
        row = db.get(Subtask, uuid.UUID(state.subtask_id))
        if row:
            row.status, row.state = state.status, state.model_dump(mode="json")
            db.commit()
