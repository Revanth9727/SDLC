import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.state import SubtaskState
from app.core.integration import integrate
from app.db.connection import SessionLocal
from app.db.models import PRLink, Subtask, Ticket
from app.tools.test_runner import TestResult


class FakeRepo:
    def __init__(self, source: Path, root: Path):
        self.source, self.root = source, root

    def cleanup_workspace(self, subtask_id):
        shutil.rmtree(self.root / subtask_id, ignore_errors=True)

    def clone_or_pull(self, repo, subtask_id):
        target = self.root / subtask_id / repo.replace("/", "__")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.source, target)
        return target

    def revision(self, repo, subtask_id):
        return "base-sha"

    def execution_file(self, checkout, path):
        candidate = (checkout / path).resolve()
        assert checkout.resolve() in candidate.parents
        return candidate


class FakePublisher:
    def __init__(self):
        self.states = []

    def publish_changes(self, state):
        self.states.append(state.model_copy(deep=True))
        number = len(self.states)
        return {"id": str(number), "number": number, "url": f"https://github.com/{state.repo}/pull/{number}",
                "repo": state.repo, "branch": f"sdlc/{state.subtask_id}", "state": "open"}


class FakeJira:
    def __init__(self):
        self.mentions = []
        self.comments = []
        self.statuses = []

    def get_issue_detail(self, key):
        return {"assignee_id": "owner-id", "assignee_name": "Owner"}

    def comment_mentioning(self, *args):
        self.mentions.append(args)

    def comment(self, *args):
        self.comments.append(args)

    def set_status(self, *args):
        self.statuses.append(args)
        return {"applied": True}


@pytest.fixture
def integrated_ticket(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "service.py").write_text("def one():\n    return 1\n\ndef two():\n    return 2\n")
    ticket_id = uuid.uuid4()
    states = []
    changes = [
        "def one():\n    return 10\n\ndef two():\n    return 2\n",
        "def one():\n    return 1\n\ndef two():\n    return 20\n",
    ]
    with SessionLocal() as db:
        for link in db.scalars(select(PRLink).where(PRLink.jira_issue_key == "INT-1")):
            db.delete(link)
        db.add(Ticket(id=ticket_id, source="jira", external_key="INT-1", title="Integrate",
                      description="Two changes", status="processing"))
        for index, content in enumerate(changes, 1):
            state = SubtaskState(ticket_id=str(ticket_id), subtask_id=str(uuid.uuid4()), jira_key="INT-1",
                subtask_type="bug", description=f"Change {index}", repo="org/repo", confirmed_repos=["org/repo"],
                orchestration_role="work", spec_id=str(index), approval_status="approved", status="integration_pending",
                base_commit="base-sha", execution_complete=True, file_changes={"service.py": content})
            states.append(state)
            db.add(Subtask(id=uuid.UUID(state.subtask_id), ticket_id=ticket_id, type="bug",
                           description=state.description, status=state.status, state=state.model_dump(mode="json")))
        db.commit()
    yield ticket_id, states, FakeRepo(source, tmp_path / "workspaces")
    with SessionLocal() as db:
        for link in db.scalars(select(PRLink).where(PRLink.jira_issue_key == "INT-1")):
            db.delete(link)
        ticket = db.get(Ticket, ticket_id)
        if ticket:
            db.delete(ticket)
            db.commit()


@pytest.mark.asyncio
async def test_combines_non_overlapping_same_file_changes_before_publishing(integrated_ticket, monkeypatch):
    ticket_id, states, repo = integrated_ticket
    seen = []

    def tests(checkout):
        text = (checkout / "service.py").read_text()
        seen.append(text)
        passed = len(seen) == 1 or ("return 10" in text and "return 20" in text)
        return TestResult(passed=passed, returncode=0 if passed else 1, output="ok" if passed else "broken")

    publisher, jira = FakePublisher(), FakeJira()
    monkeypatch.setattr("app.tools.memory.write_back", lambda state: None)
    result = await integrate(ticket_id, repo_tool=repo, test_runner=tests, publisher=publisher, jira=jira)
    assert result.passed and len(result.pr_urls) == 1
    assert len(publisher.states) == 1
    assert "return 10" in seen[-1] and "return 20" in seen[-1]
    with SessionLocal() as db:
        statuses = [db.get(Subtask, uuid.UUID(state.subtask_id)).status for state in states]
        assert statuses.count("in_review") == 1
        assert statuses.count("integration_pending") == 1


@pytest.mark.asyncio
async def test_cross_breakage_blocks_every_pr_and_escalates_with_implicated_tasks(integrated_ticket, monkeypatch):
    ticket_id, states, repo = integrated_ticket
    calls = 0

    def tests(checkout):
        nonlocal calls
        calls += 1
        return TestResult(passed=calls == 1, returncode=0 if calls == 1 else 1,
                          output="baseline passed" if calls == 1 else "test_checkout_contract failed")

    publisher, jira = FakePublisher(), FakeJira()
    monkeypatch.setattr("app.tools.memory.write_back", lambda state: None)
    result = await integrate(ticket_id, repo_tool=repo, test_runner=tests, publisher=publisher, jira=jira)
    assert not result.passed
    assert "previously-passing tests now fail" in result.report
    assert "1" in result.report and "2" in result.report
    assert publisher.states == []
    assert jira.mentions and "No PR is opened" in jira.mentions[0][-1]
    with SessionLocal() as db:
        assert db.get(Ticket, ticket_id).status == "needs_human"
        assert all(db.get(Subtask, uuid.UUID(state.subtask_id)).status == "needs_human" for state in states)
