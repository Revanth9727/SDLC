from types import SimpleNamespace

from app.tools import jira_tool
from app.tools.jira_tool import JiraTool


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def get(self, path: str, params: dict | None = None) -> _FakeResponse:
        if path == "/rest/api/3/issue/SCRUM-1":
            assert params == {"fields": "*all"}
            return _FakeResponse(
                {
                    "fields": {
                        "summary": "Fix crash",
                        "description": {
                            "type": "doc",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "See https://github.com/owner/from-description",
                                        }
                                    ],
                                }
                            ],
                        },
                        "environment": {
                            "type": "doc",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {
                                            "type": "inlineCard",
                                            "attrs": {
                                                "url": "https://github.com/owner/from-environment"
                                            },
                                        }
                                    ],
                                }
                            ],
                        },
                        "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                    }
                }
            )
        if path == "/rest/api/3/issue/SCRUM-1/comment":
            return _FakeResponse(
                {
                    "comments": [
                        {
                            "body": {
                                "type": "doc",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": "Also https://github.com/owner/from-comment",
                                            }
                                        ],
                                    }
                                ],
                            }
                        }
                    ]
                }
            )
        raise AssertionError(path)


def test_extract_plain_text_recurses_through_adf() -> None:
    adf = {
        "type": "doc",
        "content": [
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "nested text"}],
                            }
                        ],
                    }
                ],
            }
        ],
    }

    assert JiraTool.extract_plain_text(adf) == "nested text"


def test_get_issue_text_fields_includes_description_environment_and_comments(monkeypatch) -> None:
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

    fields = tool.get_issue_text_fields("SCRUM-1")

    assert "Fix crash" in fields["summary"]
    assert "github.com/owner/from-description" in fields["description"]
    assert "github.com/owner/from-environment" in fields["environment"]
    assert "github.com/owner/from-comment" in fields["comments"]


def test_recent_comments_orders_oldest_first_and_marks_the_tools_own_messages(monkeypatch) -> None:
    monkeypatch.setattr(
        jira_tool,
        "settings",
        SimpleNamespace(
            jira_base_url="https://example.atlassian.net",
            jira_email="user@example.com",
            jira_api_token="token",
            jira_project_key="SCRUM",
            jira_status_cache_ttl_seconds=300,
            jira_bot_account_id="bot-1",
        ),
    )

    class _ThreadClient(_FakeClient):
        def get(self, path: str, params: dict | None = None) -> _FakeResponse:
            if path == "/rest/api/3/issue/SCRUM-1/comment":
                assert params == {"maxResults": 12, "orderBy": "-created"}
                return _FakeResponse(
                    {
                        "comments": [
                            {
                                "id": "2",
                                "author": {"accountId": "bot-1", "displayName": "Agent"},
                                "body": {"type": "doc", "content": [
                                    {"type": "paragraph", "content": [{"type": "text", "text": "Plan ready"}]}]},
                                "created": "2026-01-02T00:00:00.000+0000",
                            },
                            {
                                "id": "1",
                                "author": {"accountId": "human-1", "displayName": "Alex"},
                                "body": {"type": "doc", "content": [
                                    {"type": "paragraph", "content": [{"type": "text", "text": "go ahead"}]}]},
                                "created": "2026-01-01T00:00:00.000+0000",
                            },
                        ]
                    }
                )
            raise AssertionError(path)

    tool = JiraTool()
    monkeypatch.setattr(tool, "_client", _ThreadClient)

    thread = tool.recent_comments("SCRUM-1", limit=12)

    assert [item["body"] for item in thread] == ["go ahead", "Plan ready"]  # oldest first
    assert [item["from_tool"] for item in thread] == [False, True]
    assert thread[1]["author"] == "Agent"
