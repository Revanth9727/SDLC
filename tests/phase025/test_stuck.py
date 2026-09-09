from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core import stuck
from app.config import settings
from app.db.models import TicketEvent


def _event(agent: str, stage: str, message: str) -> TicketEvent:
    return TicketEvent(
        agent=agent,
        stage=stage,
        message=message,
        ts=datetime.now(timezone.utc),
    )


def test_history_context_prefers_substantive_agent_progress() -> None:
    events = [
        _event("poller", "claimed", "SCRUM-1 claimed"),
        _event("diagnosis", "started", "reading files"),
        _event("diagnosis", "done", "found divide-by-zero"),
    ]

    context = stuck._history_context(events)

    assert "Started at diagnosis/started" in context
    assert "last progress: diagnosis/done" in context
    assert "found divide-by-zero" in context


def test_history_context_reports_no_activity() -> None:
    assert stuck._history_context([]) == "No activity recorded for this ticket yet. "


def test_history_context_uses_claim_when_only_system_events_exist() -> None:
    context = stuck._history_context([_event("poller", "claimed", "SCRUM-1 claimed")])

    assert "Ticket was claimed" in context
    assert "no agent work has been recorded yet" in context


@pytest.mark.asyncio
async def test_handle_stuck_dedup_skips_existing_episode(monkeypatch: pytest.MonkeyPatch) -> None:
    ticket_id = uuid4()
    ticket = SimpleNamespace(
        id=ticket_id,
        external_key="SCRUM-1",
        last_stuck_comment_at=datetime.now(timezone.utc),
    )
    jira = SimpleNamespace(
        get_issue_detail=lambda _key: {
            "key": "SCRUM-1",
            "summary": "Example",
            "description": "",
            "status": "On Hold",
            "status_category": "indeterminate",
            "updated": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
            "assignee_id": "a1",
            "assignee_name": "Assignee",
            "reporter_id": None,
            "reporter_name": None,
        }
    )
    monkeypatch.setattr(
        stuck,
        "_load_history",
        lambda _ticket_id: [
            _event(
                "stuck_detector",
                "stuck_comment_posted",
                "SCRUM-1 stuck (human) jira_status='On Hold' jira_category='indeterminate'",
            )
        ],
    )

    async def emit(_event: dict) -> None:
        raise AssertionError("deduped stuck tickets should not emit")

    assert await stuck._handle_stuck(jira, ticket, "ai", datetime.now(timezone.utc), emit) is None


@pytest.mark.asyncio
async def test_handle_stuck_skips_recently_parked_status(monkeypatch: pytest.MonkeyPatch) -> None:
    ticket = SimpleNamespace(
        id=uuid4(),
        external_key="SCRUM-1",
        last_stuck_comment_at=None,
    )
    jira = SimpleNamespace(
        get_issue_detail=lambda _key: {
            "key": "SCRUM-1",
            "summary": "Example",
            "description": "",
            "status": "On Hold",
            "status_category": "indeterminate",
            "updated": datetime.now(timezone.utc).isoformat(),
            "assignee_id": "a1",
            "assignee_name": "Assignee",
            "reporter_id": None,
            "reporter_name": None,
        },
        comment_mentioning=lambda *_args: (_ for _ in ()).throw(
            AssertionError("recently moved parked tickets should not be nudged")
        ),
    )
    monkeypatch.setattr(settings, "stuck_threshold_minutes", 120)

    async def emit(_event: dict) -> None:
        raise AssertionError("recently moved parked tickets should not emit")

    assert await stuck._handle_stuck(jira, ticket, "human", datetime.now(timezone.utc), emit) is None


def test_last_stuck_episode_status_reads_latest_stuck_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        stuck,
        "_load_history",
        lambda _ticket_id: [
            _event(
                "stuck_detector",
                "stuck_comment_posted",
                "SCRUM-1 stuck (human) jira_status='On Hold' jira_category='indeterminate'",
            ),
            _event(
                "stuck_detector",
                "stuck_comment_posted",
                "SCRUM-1 stuck (human) jira_status='Parking Lot' jira_category='new'",
            ),
        ],
    )

    assert stuck._last_stuck_episode_status(str(uuid4())) == "Parking Lot"
