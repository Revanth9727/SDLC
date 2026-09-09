from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.tools import jira_tool
from app.tools.jira_tool import JiraTool


class _FakeResponse:
    def __init__(self, payload: dict | list, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, search_payload: dict, statuses_payload: list[dict]) -> None:
        self.search_payload = search_payload
        self.statuses_payload = statuses_payload

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, _path: str, params: dict | None = None) -> _FakeResponse:
        if _path == "/rest/api/3/project/SCRUM/statuses":
            return _FakeResponse(self.statuses_payload)
        assert _path == "/rest/api/3/search/jql"
        assert 'status in ("To Do", "In Progress", "On Hold")' in params["jql"]
        return _FakeResponse(self.search_payload)


def _issue(key: str, updated: datetime) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": f"{key} summary",
            "description": None,
            "status": {"name": "On Hold", "statusCategory": {"key": "indeterminate"}},
            "updated": updated.isoformat(),
            "assignee": {"accountId": "a1", "displayName": "Assignee"},
            "reporter": {"accountId": "r1", "displayName": "Reporter"},
        },
    }


def test_list_non_terminal_stuck_filters_recent_updates(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    payload = {
        "issues": [
            _issue("SCRUM-OLD", now - timedelta(minutes=180)),
            _issue("SCRUM-NEW", now - timedelta(minutes=5)),
        ]
    }

    monkeypatch.setattr(
        jira_tool,
        "settings",
        SimpleNamespace(
            jira_base_url="https://example.atlassian.net",
            jira_email="user@example.com",
            jira_api_token="token",
            jira_project_key="SCRUM",
            jira_status_in_progress="",
            jira_status_cache_ttl_seconds=300,
        ),
    )

    tool = JiraTool()
    statuses_payload = [
        {
            "statuses": [
                {"name": "To Do", "statusCategory": {"key": "new"}},
                {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
                {"name": "On Hold", "statusCategory": {"key": "indeterminate"}},
                {"name": "Done", "statusCategory": {"key": "done"}},
            ]
        }
    ]
    monkeypatch.setattr(tool, "_client", lambda: _FakeClient(payload, statuses_payload))

    results = tool.list_non_terminal_stuck(120)

    assert [item["key"] for item in results] == ["SCRUM-OLD"]
    assert results[0]["assignee_name"] == "Assignee"
    assert results[0]["status_category"] == "indeterminate"
