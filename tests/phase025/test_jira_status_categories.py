from types import SimpleNamespace

from app.tools import jira_tool
from app.tools.jira_tool import JiraTool


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self) -> None:
        self.posts: list[dict] = []

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, path: str, params: dict | None = None) -> _FakeResponse:
        if path == "/rest/api/3/project/SCRUM/statuses":
            return _FakeResponse([
                {
                    "statuses": [
                        {"name": "Backlog", "statusCategory": {"key": "new"}},
                        {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
                        {"name": "On Hold", "statusCategory": {"key": "indeterminate"}},
                        {"name": "Done", "statusCategory": {"key": "done"}},
                    ]
                }
            ])
        if path == "/rest/api/3/issue/SCRUM-1/transitions":
            return _FakeResponse({
                "transitions": [
                    {
                        "id": "11",
                        "name": "Start work",
                        "to": {
                            "name": "In Progress",
                            "statusCategory": {"key": "indeterminate"},
                        },
                    },
                    {
                        "id": "12",
                        "name": "Park it",
                        "to": {
                            "name": "On Hold",
                            "statusCategory": {"key": "indeterminate"},
                        },
                    },
                ]
            })
        if path == "/rest/api/3/issue/SCRUM-1":
            return _FakeResponse({
                "fields": {
                    "summary": "Example",
                    "description": None,
                    "status": {"name": "Backlog", "statusCategory": {"key": "new"}},
                }
            })
        raise AssertionError(path)

    def post(self, path: str, json: dict) -> _FakeResponse:
        self.posts.append({"path": path, "json": json})
        return _FakeResponse({})


def test_fetch_project_statuses_returns_category_map(monkeypatch) -> None:
    monkeypatch.setattr(
        jira_tool,
        "settings",
        SimpleNamespace(
            jira_base_url="https://example.atlassian.net",
            jira_email="user@example.com",
            jira_api_token="token",
            jira_project_key="SCRUM",
            jira_status_cache_ttl_seconds=300,
        ),
    )
    tool = JiraTool()
    monkeypatch.setattr(tool, "_client", _FakeClient)

    assert tool.fetch_project_statuses() == {
        "Backlog": "new",
        "In Progress": "indeterminate",
        "On Hold": "indeterminate",
        "Done": "done",
    }


def test_set_status_picks_target_category_without_name_mapping(monkeypatch) -> None:
    monkeypatch.setattr(
        jira_tool,
        "settings",
        SimpleNamespace(
            jira_base_url="https://example.atlassian.net",
            jira_email="user@example.com",
            jira_api_token="token",
            jira_project_key="SCRUM",
            jira_status_cache_ttl_seconds=300,
            jira_status_in_progress="",
            jira_status_awaiting_approval="",
            jira_status_in_review="",
            jira_status_blocked="",
            jira_status_done="",
            jira_status_fallbacks={},
        ),
    )
    fake_client = _FakeClient()
    tool = JiraTool()
    monkeypatch.setattr(tool, "_client", lambda: fake_client)

    result = tool.set_status("SCRUM-1", "in_progress")

    assert result["applied"] is True
    assert result["to"] == "In Progress"
    assert fake_client.posts == [
        {
            "path": "/rest/api/3/issue/SCRUM-1/transitions",
            "json": {"transition": {"id": "11"}},
        }
    ]
