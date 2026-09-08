from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core import reconcile


class _ExecResult:
    def first(self):
        return (uuid4(),)


class _Query:
    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return None


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def query(self, *_args, **_kwargs):
        return _Query()

    def execute(self, *_args, **_kwargs):
        return _ExecResult()

    def commit(self):
        return None


class _FakeJira:
    def __init__(self) -> None:
        self.comments: list[str] = []
        self.status_changes: list[tuple[str, str]] = []

    def status_category(self, status_name: str) -> str | None:
        raise AssertionError(f"category should come from issue detail, got {status_name}")

    def comment_mentioning(self, key, account_id, display_name, body):
        self.comments.append(key)

    def set_status(self, key: str, stage: str):
        self.status_changes.append((key, stage))


@pytest.mark.asyncio
async def test_custom_active_status_is_human_owned_not_unknown(monkeypatch) -> None:
    events: list[dict] = []

    async def fake_ev_log(**kwargs):
        events.append(kwargs)
        return kwargs

    monkeypatch.setattr(reconcile, "SessionLocal", _Session)
    monkeypatch.setattr(reconcile, "ev_log", fake_ev_log)

    jira = _FakeJira()
    detail = {
        "key": "SCRUM-X",
        "summary": "Custom status",
        "description": "",
        "status": "On Hold",
        "status_category": "indeterminate",
        "updated": "2026-09-08T04:24:26.000+0000",
        "assignee_id": "abc123",
        "assignee_name": "Revanth",
        "reporter_id": None,
        "reporter_name": None,
    }

    event = await reconcile._reconcile_one(jira, "SCRUM-X", detail)

    assert event["stage"] == "human_owned"
    assert "active in Jira (On Hold)" in event["message"]
    assert jira.comments == []
    assert jira.status_changes == []
