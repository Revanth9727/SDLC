from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core import reconcile


class _Session:
    executed = False
    committed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, *_args, **_kwargs):
        type(self).executed = True

    def commit(self):
        type(self).committed = True


@pytest.mark.asyncio
async def test_refresh_local_content_updates_changed_title_description(monkeypatch) -> None:
    local = SimpleNamespace(
        id=uuid4(),
        title="Old title",
        description="Old description",
    )
    detail = {
        "key": "SCRUM-1",
        "summary": "New title",
        "description": "New description with https://github.com/owner/repo",
    }
    events: list[dict] = []

    async def fake_ev_log(**kwargs):
        events.append(kwargs)
        return kwargs

    _Session.executed = False
    _Session.committed = False
    monkeypatch.setattr(reconcile, "SessionLocal", _Session)
    monkeypatch.setattr(reconcile, "ev_log", fake_ev_log)

    event = await reconcile._refresh_local_content("SCRUM-1", local, detail)

    assert _Session.executed is True
    assert _Session.committed is True
    assert event["stage"] == "content_refreshed"
    assert events[0]["ticket_id"] == str(local.id)
