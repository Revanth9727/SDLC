import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.state import SubtaskState
from app.core.integration import (MergeToolError, ReconciliationCandidate, _state_order,
                                  _three_way_merge, integrate)
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


class ApprovingCritic:
    def __init__(self):
        self.reviewed = []
        self.evidence = []

    def run(self, state):
        self.reviewed.append(dict(state.file_changes))
        self.evidence.append(state.integration_review_evidence)
        state.critic_verdict = {
            "approved": True, "issues": [], "verifiability": "ok",
            "test_validity": "valid", "test_issues": [], "implementation_valid": True,
        }
        return state


@pytest.fixture
def integrated_ticket(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "service.py").write_text(
        "def one():\n    return 1\n\ndef two():\n    return 2\n\ndef three():\n    return 3\n"
    )
    ticket_id = uuid.uuid4()
    states = []
    changes = [
        "def one():\n    return 10\n\ndef two():\n    return 2\n\ndef three():\n    return 3\n",
        "def one():\n    return 1\n\ndef two():\n    return 20\n\ndef three():\n    return 3\n",
        "def one():\n    return 1\n\ndef two():\n    return 2\n\ndef three():\n    return 30\n",
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
async def test_three_subtasks_publish_the_exact_tested_combination(integrated_ticket, monkeypatch):
    ticket_id, states, repo = integrated_ticket
    seen = []

    def tests(checkout):
        text = (checkout / "service.py").read_text()
        seen.append(text)
        passed = len(seen) == 1 or all(value in text for value in ("return 10", "return 20", "return 30"))
        return TestResult(passed=passed, returncode=0 if passed else 1, output="ok" if passed else "broken")

    publisher, jira = FakePublisher(), FakeJira()
    monkeypatch.setattr("app.tools.memory.write_back", lambda state: None)
    critic = ApprovingCritic()
    result = await integrate(ticket_id, repo_tool=repo, test_runner=tests, publisher=publisher,
                             jira=jira, critic=critic)
    assert result.passed and len(result.pr_urls) == 1
    assert len(publisher.states) == 1
    tested = seen[-1]
    published = publisher.states[0].file_changes["service.py"]
    assert all(value in tested for value in ("return 10", "return 20", "return 30"))
    assert published == tested
    assert publisher.states[0].integration_verified is True
    assert publisher.states[0].integrated_subtask_ids == [state.subtask_id for state in states]
    assert critic.reviewed == [{"service.py": tested}]
    with SessionLocal() as db:
        statuses = [db.get(Subtask, uuid.UUID(state.subtask_id)).status for state in states]
        assert statuses == ["in_review", "in_review", "in_review"]


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
    def ambiguous(_facts, _attempt):
        return ReconciliationCandidate(ambiguous=True, reasoning="Two intended behaviors remain plausible")

    result = await integrate(ticket_id, repo_tool=repo, test_runner=tests, publisher=publisher,
                             jira=jira, reconciler=ambiguous, critic=ApprovingCritic())
    assert not result.passed
    assert "behavioral interaction could not be reconciled unambiguously" in result.report
    assert "1" in result.report and "2" in result.report
    assert publisher.states == []
    assert jira.mentions and "No PR is opened" in jira.mentions[0][-1]
    with SessionLocal() as db:
        assert db.get(Ticket, ticket_id).status == "needs_human"
        assert all(db.get(Subtask, uuid.UUID(state.subtask_id)).status == "needs_human" for state in states)


@pytest.mark.asyncio
async def test_unverifiable_integration_never_publishes(integrated_ticket, monkeypatch):
    ticket_id, states, repo = integrated_ticket
    def unavailable(_checkout):
        return TestResult(passed=False, returncode=1,
                          output='password authentication failed for database')
    publisher, jira = FakePublisher(), FakeJira()
    monkeypatch.setattr("app.tools.memory.write_back", lambda state: None)
    result = await integrate(ticket_id, repo_tool=repo, test_runner=unavailable,
                             publisher=publisher, jira=jira)
    assert not result.passed
    assert 'UNVERIFIABLE' in result.report
    assert publisher.states == []
    with SessionLocal() as db:
        persisted = db.get(Subtask, uuid.UUID(states[0].subtask_id)).state
        assert persisted['verification_summary']['outcome'] == 'UNVERIFIABLE'


@pytest.mark.asyncio
async def test_behavioral_failure_reconciles_retests_recritics_and_publishes(integrated_ticket, monkeypatch):
    ticket_id, states, repo = integrated_ticket
    runs = []

    def tests(checkout):
        text = (checkout / "service.py").read_text()
        runs.append(text)
        # baseline passes, first combination fails, reconciled candidate passes
        passed = len(runs) != 2
        return TestResult(passed=passed, returncode=0 if passed else 1,
                          output="3 passed" if passed else "1 failed\nFAILED tests/test_service.py::test_combined")

    final = ("# reconciled combined contract\n"
             "def one():\n    return 10\n\ndef two():\n    return 20\n\n"
             "def three():\n    return 30\n")
    def reconcile(facts, attempt):
        assert attempt == 1
        assert facts["combined_test"]["outcome"] == "FAIL"
        return ReconciliationCandidate(reasoning="Preserve all three approved outcomes",
                                       file_changes={"service.py": final})

    publisher, jira, critic = FakePublisher(), FakeJira(), ApprovingCritic()
    monkeypatch.setattr("app.tools.memory.write_back", lambda state: None)
    result = await integrate(ticket_id, repo_tool=repo, test_runner=tests, publisher=publisher,
                             jira=jira, reconciler=reconcile, critic=critic)

    assert result.passed and result.repos[0].interaction
    assert result.repos[0].reconciliation_attempts == 1
    assert len(runs) == 3
    assert critic.reviewed == [{"service.py": final}]
    evidence = critic.evidence[0]
    assert evidence["original_combined_test_failure"]["outcome"] == "FAIL"
    assert "test_combined" in evidence["original_combined_test_failure"]["output"]
    assert evidence["reconciliation_attempts"][0]["reasoning"] == "Preserve all three approved outcomes"
    assert evidence["reconciliation_attempts"][0]["delta"][0]["path"] == "service.py"
    assert evidence["reconciliation_attempts"][0]["delta"][0]["before_sha256"]
    assert evidence["reconciliation_attempts"][0]["delta"][0]["after_sha256"]
    assert evidence["final_combined_test_result"]["outcome"] == "PASS"
    assert publisher.states[0].file_changes == critic.reviewed[0]
    assert publisher.states[0].file_changes["service.py"] == runs[-1]


@pytest.mark.asyncio
async def test_textual_conflict_uses_no_reconciliation_llm_and_opens_no_pr(integrated_ticket, monkeypatch):
    ticket_id, _states, repo = integrated_ticket
    calls = []
    monkeypatch.setattr("app.core.integration._three_way_merge", lambda *_args: None)
    def must_not_run(*args):
        calls.append(args)
        raise AssertionError("textual conflict must not invoke reconciliation")
    publisher = FakePublisher()
    result = await integrate(
        ticket_id, repo_tool=repo,
        test_runner=lambda _: TestResult(passed=True, returncode=0, output="3 passed"),
        publisher=publisher, jira=FakeJira(), reconciler=must_not_run, critic=ApprovingCritic(),
    )
    assert not result.passed
    assert "Combined-change conflict" in result.report
    assert calls == [] and publisher.states == []


@pytest.mark.asyncio
async def test_merge_infrastructure_failure_records_context_without_reasoning_retry(integrated_ticket, monkeypatch):
    ticket_id, states, repo = integrated_ticket
    calls = []
    def crash(*_args):
        raise MergeToolError("merge adapter crashed")
    monkeypatch.setattr("app.core.integration._three_way_merge", crash)
    def must_not_run(*args):
        calls.append(args)
        raise AssertionError("infrastructure failures must not invoke reconciliation")
    publisher = FakePublisher()
    result = await integrate(
        ticket_id, repo_tool=repo,
        test_runner=lambda _: TestResult(passed=True, returncode=0, output="3 passed"),
        publisher=publisher, jira=FakeJira(), reconciler=must_not_run, critic=ApprovingCritic(),
    )
    assert not result.passed and calls == [] and publisher.states == []
    with SessionLocal() as db:
        persisted = db.get(Subtask, uuid.UUID(states[0].subtask_id)).state
    context = persisted["failure_contexts"][-1]
    assert context["classification"] == "infrastructure"
    assert context["component"] == "integration"
    assert context["exception_type"] == "MergeToolError"


def test_merge_tool_failure_is_not_a_text_conflict(monkeypatch):
    def crash(*_args, **_kwargs):
        raise OSError("git unavailable")
    monkeypatch.setattr("app.core.integration.subprocess.run", crash)
    with pytest.raises(MergeToolError, match="could not run"):
        _three_way_merge("a\n", "b\n", "c\n")


def test_same_function_compatible_edits_merge_without_conflict():
    base = ("def calculate(value):\n"
            "    subtotal = value\n"
            "    # tax is calculated independently\n"
            "    tax = 0\n"
            "    total = subtotal + tax\n"
            "    return total\n")
    current = base.replace("    subtotal = value\n", "    subtotal = max(value, 0)\n")
    incoming = base.replace("    tax = 0\n", "    tax = subtotal * 0.1\n")
    merged = _three_way_merge(current, base, incoming)
    assert merged is not None
    assert "subtotal = max(value, 0)" in merged
    assert "tax = subtotal * 0.1" in merged


def test_true_same_line_conflict_is_reported_deterministically():
    assert _three_way_merge("value = 2\n", "value = 1\n", "value = 3\n") is None


def test_merge_order_is_stable_for_three_or_more_subtasks():
    states = [SubtaskState(ticket_id=str(uuid.uuid4()), subtask_id=str(uuid.uuid4()),
                          subtask_type="bug", description=str(index), repo="org/repo",
                          orchestration_index=index, spec_id=str(index))
              for index in (3, 1, 2)]
    assert [state.orchestration_index for state in sorted(states, key=_state_order)] == [1, 2, 3]
