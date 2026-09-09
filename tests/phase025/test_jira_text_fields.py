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
