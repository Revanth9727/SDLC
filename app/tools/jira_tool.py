"""Deterministic Jira integration tool (no LLM — R-5, R-6).

All credentials come from settings (R-17). Every public method is logged with
its input and output (R-7).
"""

import logging
from datetime import datetime
from typing import Any, TypedDict

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_JQL_OPEN = "project = {project_key} AND statusCategory != Done ORDER BY created DESC"


class JiraIssue(TypedDict):
    key: str
    summary: str
    description: str
    status: str  # Jira status name, e.g. "To Do", "In Progress"


class JiraTransition(TypedDict):
    id: str
    name: str  # destination status name, e.g. "In Progress"
    transition_name: str  # Jira workflow transition label, e.g. "Start progress"


class JiraIssueDetail(TypedDict):
    """Richer issue shape used by the reconciliation loop."""
    key: str
    summary: str
    description: str
    status: str
    updated: str           # ISO-8601 string from Jira
    assignee_id: str | None
    assignee_name: str | None
    reporter_id: str | None
    reporter_name: str | None




# Maps internal stage names to the matching Settings attribute.
_STAGE_MAP: dict[str, str] = {
    "in_progress":        "jira_status_in_progress",
    "awaiting_approval":  "jira_status_awaiting_approval",
    "in_review":          "jira_status_in_review",
    "blocked":            "jira_status_blocked",
    "done":               "jira_status_done",
}


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
                "/rest/api/3/search/jql",
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

    def list_by_status(self, status_name: str) -> list[JiraIssue]:
        """Return issues for the configured project with exactly ``status_name``."""
        jql = f'project = {self._project} AND status = "{status_name}" ORDER BY created ASC'
        logger.info("jira.list_by_status jql=%r", jql)
        with self._client() as client:
            resp = client.get(
                "/rest/api/3/search/jql",
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
        logger.info("jira.list_by_status status=%r -> %d issues", status_name, len(issues))
        return issues

    def get_transitions(self, key: str) -> list[JiraTransition]:
        """Return the transitions allowed from the issue's CURRENT status.

        Uses Jira's GET /rest/api/3/issue/{key}/transitions which only returns
        transitions valid from the current state — never the full workflow.
        """
        logger.info("jira.get_transitions key=%r", key)
        with self._client() as client:
            resp = client.get(f"/rest/api/3/issue/{key}/transitions")
            resp.raise_for_status()
        transitions = [
            JiraTransition(
                id=t["id"],
                name=t["to"]["name"],
                transition_name=t.get("name", ""),
            )
            for t in resp.json().get("transitions", [])
        ]
        logger.info("jira.get_transitions key=%r -> %r", key, transitions)
        return transitions

    def set_status(self, key: str, internal_stage: str) -> dict[str, Any]:
        """Transition a Jira issue to the status mapped from ``internal_stage``.

        Never raises — if no matching transition is found, logs a warning and
        returns applied=False (R-11).

        Args:
            key: Jira issue key, e.g. "AGT-1".
            internal_stage: One of "in_progress", "awaiting_approval",
                "in_review", "blocked", "done".
        """
        # 1. Resolve the desired status name from settings.
        attr = _STAGE_MAP.get(internal_stage)
        if not attr:
            reason = f"unknown internal_stage {internal_stage!r}; must be one of {list(_STAGE_MAP)}"
            logger.warning("jira.set_status SKIPPED key=%r stage=%r reason=%r", key, internal_stage, reason)
            return {"applied": False, "from": "", "to": None, "reason": reason}

        desired_name: str = getattr(settings, attr, "").strip()
        if not desired_name:
            reason = f"settings.{attr} is empty; transition for stage {internal_stage!r} skipped"
            logger.warning("jira.set_status SKIPPED key=%r stage=%r reason=%r", key, internal_stage, reason)
            return {"applied": False, "from": "", "to": None, "reason": reason}

        # Snapshot current status before the transition attempt.
        current_status = self.get_issue(key)["status"]

        # 2. Fetch allowed transitions from the issue's CURRENT state.
        transitions = self.get_transitions(key)
        by_name: dict[str, str] = {t["name"].lower(): t["id"] for t in transitions}
        logger.info(
            "jira.set_status MATCH_CHECK key=%r stage=%r desired=%r current=%r transitions=%r",
            key,
            internal_stage,
            desired_name,
            current_status,
            transitions,
        )

        # 3a. Match primary name (case-insensitive).
        transition_id = by_name.get(desired_name.lower())

        # 3b. Try configured fallback if primary not found.
        if transition_id is None:
            fallback_name = settings.jira_status_fallbacks.get(internal_stage, "").strip()
            if fallback_name:
                transition_id = by_name.get(fallback_name.lower())
                if transition_id:
                    desired_name = fallback_name

        logger.info(
            "jira.set_status MATCH_RESULT key=%r stage=%r desired=%r matched=%s transition_id=%r",
            key,
            internal_stage,
            desired_name,
            bool(transition_id),
            transition_id,
        )

        if transition_id is None:
            available = [t["name"] for t in transitions]
            reason = (
                f"no matching transition for {desired_name!r} "
                f"(fallback also missing); available={available}"
            )
            logger.warning(
                "jira.set_status SKIPPED key=%r stage=%r reason=%r",
                key, internal_stage, reason,
            )
            return {"applied": False, "from": current_status, "to": None, "reason": reason}

        # 4. POST the transition.
        logger.info(
            "jira.set_status APPLYING key=%r stage=%r transition_id=%r name=%r",
            key, internal_stage, transition_id, desired_name,
        )
        with self._client() as client:
            resp = client.post(
                f"/rest/api/3/issue/{key}/transitions",
                json={"transition": {"id": transition_id}},
            )
            resp.raise_for_status()
        logger.info(
            "jira.set_status APPLIED key=%r from=%r to=%r",
            key, current_status, desired_name,
        )
        return {"applied": True, "from": current_status, "to": desired_name, "reason": None}

    def get_issue_detail(self, key: str) -> JiraIssueDetail:
        """Fetch a single issue with assignee, reporter and updated time (for reconcile)."""
        logger.info("jira.get_issue_detail key=%r", key)
        with self._client() as client:
            resp = client.get(
                f"/rest/api/3/issue/{key}",
                params={"fields": "summary,description,status,assignee,reporter,updated"},
            )
            resp.raise_for_status()
        fields = resp.json().get("fields", {})
        summary, description = self._extract(fields)
        status = (fields.get("status") or {}).get("name", "")
        updated = fields.get("updated", "")
        assignee = fields.get("assignee") or {}
        reporter = fields.get("reporter") or {}
        result = JiraIssueDetail(
            key=key,
            summary=summary,
            description=description,
            status=status,
            updated=updated,
            assignee_id=assignee.get("accountId"),
            assignee_name=assignee.get("displayName"),
            reporter_id=reporter.get("accountId"),
            reporter_name=reporter.get("displayName"),
        )
        logger.info("jira.get_issue_detail -> status=%r", result["status"])
        return result

    def list_updated_since(self, since: datetime) -> list[JiraIssueDetail]:
        """Return all project issues updated at or after ``since`` (UTC)."""
        since_str = since.strftime("%Y-%m-%d %H:%M")
        jql = (
            f'project = {self._project} AND updated >= "{since_str}" '
            f'ORDER BY updated ASC'
        )
        logger.info("jira.list_updated_since since=%r jql=%r", since_str, jql)
        with self._client() as client:
            resp = client.get(
                "/rest/api/3/search/jql",
                params={
                    "jql": jql,
                    "fields": "summary,description,status,assignee,reporter,updated",
                    "maxResults": 100,
                },
            )
            resp.raise_for_status()
        results: list[JiraIssueDetail] = []
        for item in resp.json().get("issues", []):
            f = item.get("fields", {})
            summary, description = self._extract(f)
            assignee = f.get("assignee") or {}
            reporter = f.get("reporter") or {}
            results.append(JiraIssueDetail(
                key=item["key"],
                summary=summary,
                description=description,
                status=(f.get("status") or {}).get("name", ""),
                updated=f.get("updated", ""),
                assignee_id=assignee.get("accountId"),
                assignee_name=assignee.get("displayName"),
                reporter_id=reporter.get("accountId"),
                reporter_name=reporter.get("displayName"),
            ))
        logger.info("jira.list_updated_since -> %d issue(s)", len(results))
        return results

    def comment_mentioning(
        self,
        key: str,
        account_id: str | None,
        display_name: str | None,
        body_text: str,
    ) -> None:
        """Post a Jira comment, @mentioning the user if account_id is provided."""
        logger.info("jira.comment_mentioning key=%r mention=%r", key, display_name)
        if account_id:
            para_content = [
                {
                    "type": "mention",
                    "attrs": {
                        "id": account_id,
                        "text": f"@{display_name or account_id}",
                        "accessLevel": "",
                    },
                },
                {"type": "text", "text": f" {body_text}"},
            ]
        else:
            para_content = [{"type": "text", "text": body_text}]
        payload = {
            "body": {
                "version": 1,
                "type": "doc",
                "content": [{"type": "paragraph", "content": para_content}],
            }
        }
        with self._client() as client:
            resp = client.post(f"/rest/api/3/issue/{key}/comment", json=payload)
            resp.raise_for_status()
        logger.info("jira.comment_mentioning -> posted to %r", key)

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
