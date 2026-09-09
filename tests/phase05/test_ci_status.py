"""CI is the authoritative check (ai_rules.md R-32/R-46): GitHubTool.pr_checks
aggregates a PR's real CI result, and the ticket UI can ask for it on demand."""
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.db.connection import SessionLocal
from app.db.models import Subtask
from app.tools.github_tool import GitHubTool
from app.web import approval as approval_module


class FakeCheckRun:
    def __init__(self, name, status, conclusion, html_url='https://github.com/o/r/runs/1'):
        self.name, self.status, self.conclusion, self.html_url = name, status, conclusion, html_url


class FakeStatus:
    def __init__(self, context, state, target_url='https://ci.example/1'):
        self.context, self.state, self.target_url = context, state, target_url


class FakeCombinedStatus:
    def __init__(self, state='pending', statuses=()):
        self.state, self.statuses = state, list(statuses)


class FakeCommit:
    def __init__(self, check_runs=(), combined=None):
        self._check_runs, self._combined = list(check_runs), combined or FakeCombinedStatus()
    def get_check_runs(self):
        return self._check_runs
    def get_combined_status(self):
        return self._combined


class FakeAPIRepoForChecks:
    def __init__(self, commit):
        self._commit = commit
    def get_pull(self, number):
        return SimpleNamespace(head=SimpleNamespace(sha='deadbeef'))
    def get_commit(self, sha):
        return self._commit


def _tool(commit):
    tool = GitHubTool.__new__(GitHubTool)
    tool.get_repo = lambda full_name=None: FakeAPIRepoForChecks(commit)
    return tool


def test_pr_checks_success_from_check_runs():
    commit = FakeCommit(check_runs=[FakeCheckRun('build', 'completed', 'success'),
                                    FakeCheckRun('lint', 'completed', 'neutral')])
    result = _tool(commit).pr_checks('owner/repo', 1)
    assert result['configured'] is True
    assert result['status'] == 'completed'
    assert result['conclusion'] == 'success'
    assert len(result['runs']) == 2


def test_pr_checks_failure_when_any_run_fails():
    commit = FakeCommit(check_runs=[FakeCheckRun('build', 'completed', 'success'),
                                    FakeCheckRun('tests', 'completed', 'failure')])
    result = _tool(commit).pr_checks('owner/repo', 1)
    assert result['conclusion'] == 'failure'


def test_pr_checks_in_progress_while_a_run_is_still_running():
    commit = FakeCommit(check_runs=[FakeCheckRun('build', 'completed', 'success'),
                                    FakeCheckRun('slow-suite', 'in_progress', None)])
    result = _tool(commit).pr_checks('owner/repo', 1)
    assert result['status'] == 'in_progress'
    assert result['conclusion'] is None


def test_pr_checks_falls_back_to_combined_status_for_non_actions_ci():
    commit = FakeCommit(combined=FakeCombinedStatus('success', [FakeStatus('ci/circleci', 'success')]))
    result = _tool(commit).pr_checks('owner/repo', 1)
    assert result == {'configured': True, 'status': 'completed', 'conclusion': 'success',
                      'url': 'https://ci.example/1',
                      'runs': [{'name': 'ci/circleci', 'status': 'completed', 'conclusion': 'success',
                                'url': 'https://ci.example/1'}]}


def test_pr_checks_not_configured_when_nothing_reports():
    result = _tool(FakeCommit()).pr_checks('owner/repo', 1)
    assert result == {'configured': False, 'status': 'none', 'conclusion': None, 'url': None, 'runs': []}


@pytest.mark.asyncio
async def test_ci_status_endpoint_reports_not_configured_before_a_pr_exists(db_ticket):
    ticket_id, _key = db_ticket
    sid = uuid.uuid4()
    with SessionLocal() as db:
        db.add(Subtask(id=sid, ticket_id=uuid.UUID(ticket_id), type='bug', description='x',
                       status='running', state={}))
        db.commit()
    app = FastAPI(); app.include_router(approval_module.router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get(f'/tickets/{ticket_id}/subtasks/{sid}/ci-status')
        assert response.status_code == 200
        assert response.json() == {'configured': False, 'status': 'none', 'conclusion': None, 'url': None, 'runs': []}
    finally:
        with SessionLocal() as db:
            db.delete(db.get(Subtask, sid)); db.commit()


@pytest.mark.asyncio
async def test_ci_status_endpoint_resolves_repo_and_number_from_the_pr_url(db_ticket, monkeypatch):
    ticket_id, _key = db_ticket
    sid = uuid.uuid4()
    with SessionLocal() as db:
        db.add(Subtask(id=sid, ticket_id=uuid.UUID(ticket_id), type='bug', description='x',
                       status='in_review', state={'pr_url': 'https://github.com/owner/repo/pull/7'}))
        db.commit()
    seen = {}
    def fake_pr_checks(self, full_name, number):
        seen['args'] = (full_name, number)
        return {'configured': True, 'status': 'completed', 'conclusion': 'success', 'url': 'x', 'runs': []}
    monkeypatch.setattr(GitHubTool, 'pr_checks', fake_pr_checks)
    app = FastAPI(); app.include_router(approval_module.router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get(f'/tickets/{ticket_id}/subtasks/{sid}/ci-status')
        assert response.status_code == 200
        assert response.json()['conclusion'] == 'success'
        assert seen['args'] == ('owner/repo', 7)
    finally:
        with SessionLocal() as db:
            db.delete(db.get(Subtask, sid)); db.commit()
