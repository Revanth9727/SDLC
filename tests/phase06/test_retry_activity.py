from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core import run_control


class _Session:
    def __init__(self, task_status: str, ticket_status: str):
        self.task = SimpleNamespace(status=task_status)
        self.ticket = SimpleNamespace(status=ticket_status)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def scalar(self, _query):
        return self.task

    def get(self, _model, _key):
        return self.ticket


def _state():
    return SimpleNamespace(ticket_id=str(uuid4()), subtask_id=str(uuid4()))


def test_active_subtask_can_retry_when_ticket_aggregate_needs_human(monkeypatch):
    monkeypatch.setattr(run_control, "SessionLocal", lambda: _Session("running", "needs_human"))

    run_control.require_active(_state())


def test_terminal_ticket_still_stops_active_subtask(monkeypatch):
    monkeypatch.setattr(run_control, "SessionLocal", lambda: _Session("running", "done"))

    with pytest.raises(ValueError, match="ticket is no longer active"):
        run_control.require_active(_state())
