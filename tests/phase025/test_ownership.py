from datetime import datetime, timezone

from app.core.ownership import has_active_ai_run
from app.db.models import Ticket


def _ticket(status: str, claimed: bool) -> Ticket:
    return Ticket(
        source="jira",
        external_key="SCRUM-1",
        title="Example",
        description="Example",
        status=status,
        claimed_at=datetime.now(timezone.utc) if claimed else None,
    )


def test_claimed_active_ticket_is_ai_owned() -> None:
    assert has_active_ai_run(_ticket("new", claimed=True)) is True


def test_unclaimed_ticket_is_not_ai_owned() -> None:
    assert has_active_ai_run(_ticket("new", claimed=False)) is False


def test_released_or_human_status_is_not_active_ai_run() -> None:
    for status in ["done", "failed", "human_owned", "needs_human", "superseded"]:
        assert has_active_ai_run(_ticket(status, claimed=True)) is False
