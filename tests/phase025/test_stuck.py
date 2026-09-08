from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core import stuck
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
    ticket = SimpleNamespace(
        external_key="SCRUM-1",
        last_stuck_comment_at=datetime.now(timezone.utc),
    )
    jira = SimpleNamespace()

    async def emit(_event: dict) -> None:
        raise AssertionError("deduped stuck tickets should not emit")

    assert await stuck._handle_stuck(jira, ticket, "ai", datetime.now(timezone.utc), emit) is None
