import uuid

import pytest

from app.core import comment_handling
from app.db.connection import SessionLocal
from app.db.models import PRLink, Ticket


class Jira:
    def __init__(self):
        self.comments = []

    @staticmethod
    def extract_plain_text(value):
        return value

    def get_issue_detail(self, key):
        return {"assignee_id": "owner"}

    def comment(self, key, message):
        self.comments.append(message)


class GitHub:
    def pr_snapshot(self, repo, number):
        return {"id": "r53-pr", "repo": repo, "number": number,
                "url": "https://github.com/org/repo/pull/7", "state": "open",
                "branch": "sdlc/work", "title": "R53: current work"}


@pytest.mark.asyncio
async def test_open_pr_blocks_comment_before_any_classifier_call(monkeypatch):
    ticket_id = uuid.uuid4()
    with SessionLocal() as db:
        old = db.get(PRLink, "r53-pr")
        if old:
            db.delete(old)
        db.add(Ticket(id=ticket_id, source="jira", external_key="RTEST-53",
                      title="R53", description="test"))
        db.add(PRLink(github_pr_id="r53-pr", jira_issue_key="RTEST-53", repo="org/repo",
                      number=7, branch="sdlc/work", url="retires", pr_state="open"))
        db.commit()
    jira = Jira()
    monkeypatch.setattr(comment_handling, "is_app_comment", lambda _: False)
    monkeypatch.setattr("app.core.publish.GitHubTool", lambda: GitHub())
    monitor = type("Monitor", (), {"run": lambda *args: pytest.fail("classifier must not run")})()
    try:
        result = await comment_handling.handle_comment(
            "RTEST-53",
            {"id": "human-1", "author": {"accountId": "owner"}, "body": "please change it"},
            jira,
            monitor,
        )
        assert result == "open_pr_requires_decision"
        assert "R53: current work" in jira.comments[-1]
        assert "Keep it" in jira.comments[-1] and "Replace PR" in jira.comments[-1]
    finally:
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            if ticket:
                db.delete(ticket)
            link = db.get(PRLink, "r53-pr")
            if link:
                db.delete(link)
            db.commit()
