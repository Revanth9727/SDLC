from types import SimpleNamespace

import pytest

from app.core import steps


class _FakeJiraTool:
    comments: list[tuple[str, str]] = []

    def comment(self, key: str, message: str) -> None:
        self.comments.append((key, message))


@pytest.mark.asyncio
async def test_post_step_comments_and_emits(monkeypatch) -> None:
    emitted: list[dict] = []

    async def fake_log_event(**kwargs):
        return {"type": "event", **kwargs}

    async def fake_emit(event: dict) -> None:
        emitted.append(event)

    _FakeJiraTool.comments = []
    monkeypatch.setattr(steps, "JiraTool", _FakeJiraTool)
    monkeypatch.setattr(steps, "log_event", fake_log_event)

    ticket = SimpleNamespace(id="ticket-1", external_key="SCRUM-1")

    event = await steps.post_step(
        ticket,
        "Using repo `owner/repo`",
        stage="repo_resolved",
        emit=fake_emit,
    )

    assert _FakeJiraTool.comments == [("SCRUM-1", "Using repo `owner/repo`")]
    assert emitted == [event]
    assert event["ticket_id"] == "ticket-1"
    assert event["key"] == "SCRUM-1"
    assert event["stage"] == "repo_resolved"
