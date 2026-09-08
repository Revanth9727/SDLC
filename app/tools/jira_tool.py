"""Deterministic Jira integration tool (no LLM — R-5, R-6).

All credentials come from settings (R-17). Every public method is logged with
its input and output (R-7).
"""

import logging
from typing import TypedDict

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_JQL_OPEN = "project = {project_key} AND statusCategory != Done ORDER BY created DESC"


class JiraIssue(TypedDict):
    key: str
    summary: str
    description: str
    status: str  # Jira status name, e.g. "To Do", "In Progress"

# TODO(1.4): add get_transitions(key) and set_status(key, internal_stage) here


class JiraTool:
    """Thin, typed wrapper around the Jira Cloud REST API v3."""

    def __init__(self) -> None:
        self._base = settings.jira_base_url.rstrip("/")
        self._auth = (settings.jira_email, settings.jira_api_token)
        self._project = settings.jira_project_key
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._base,
            auth=self._auth,
            headers=self._headers,
            timeout=15,
        )

    def _extract(self, fields: dict) -> tuple[str, str]:
        """Pull summary and plain-text description from an issue's fields dict."""
        summary = fields.get("summary", "")
        desc_root = fields.get("description") or {}
        lines: list[str] = []
        for block in desc_root.get("content", []):
            for inline in block.get("content", []):
                if inline.get("type") == "text":
                    lines.append(inline.get("text", ""))
        description = " ".join(lines).strip()
        return summary, description

    def list_open_issues(self) -> list[JiraIssue]:
        """Return all open issues for the configured project."""
        jql = _JQL_OPEN.format(project_key=self._project)
        logger.info("jira.list_open_issues jql=%r", jql)
        with self._client() as client:
            resp = client.get(
                "/rest/api/3/search",
                params={"jql": jql, "fields": "summary,description,status", "maxResults": 50},
            )
            resp.raise_for_status()
        issues: list[JiraIssue] = []
        for item in resp.json().get("issues", []):
            fields = item.get("fields", {})
            summary, description = self._extract(fields)
            status = (fields.get("status") or {}).get("name", "")
            issues.append(
                JiraIssue(key=item["key"], summary=summary, description=description, status=status)
            )
        logger.info("jira.list_open_issues -> %d issues", len(issues))
        return issues

    def get_issue(self, key: str) -> JiraIssue:
        """Fetch a single Jira issue by key (e.g. 'SANDBOX-1')."""
        logger.info("jira.get_issue key=%r", key)
        with self._client() as client:
            resp = client.get(
                f"/rest/api/3/issue/{key}",
                params={"fields": "summary,description,status"},
            )
            resp.raise_for_status()
        fields = resp.json().get("fields", {})
        summary, description = self._extract(fields)
        status = (fields.get("status") or {}).get("name", "")
        result = JiraIssue(key=key, summary=summary, description=description, status=status)
        logger.info("jira.get_issue -> %r", result)
        return result

    def comment(self, key: str, body: str) -> None:
        """Post a plain-text comment to the given Jira issue."""
        logger.info("jira.comment key=%r body_len=%d", key, len(body))
        payload = {
            "body": {
                "version": 1,
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": body}],
                    }
                ],
            }
        }
        with self._client() as client:
            resp = client.post(f"/rest/api/3/issue/{key}/comment", json=payload)
            resp.raise_for_status()
        logger.info("jira.comment -> posted to %r", key)
