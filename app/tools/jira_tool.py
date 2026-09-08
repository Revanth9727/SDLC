"""Deterministic Jira integration tool (no LLM — R-5, R-6).

All credentials come from settings (R-17). Every public method is logged with
its input and output (R-7).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_JQL_OPEN = "project = {project_key} AND statusCategory != Done ORDER BY created DESC"
_CATEGORY_NEW = "new"
_CATEGORY_ACTIVE = "indeterminate"
_CATEGORY_DONE = "done"
_KNOWN_CATEGORIES = {_CATEGORY_NEW, _CATEGORY_ACTIVE, _CATEGORY_DONE}


class JiraIssue(TypedDict):
    key: str
    summary: str
    description: str
    status: str  # Jira status name, e.g. "To Do", "In Progress"
    status_category: str | None  # Jira statusCategory.key: new | indeterminate | done


class JiraTransition(TypedDict):
    id: str
    name: str  # destination status name, e.g. "In Progress"
    transition_name: str  # Jira workflow transition label, e.g. "Start progress"
    category: str | None  # destination statusCategory.key


class JiraIssueDetail(TypedDict):
    """Richer issue shape used by the reconciliation loop."""
    key: str
    summary: str
    description: str
    status: str
    status_category: str | None
    updated: str           # ISO-8601 string from Jira
    assignee_id: str | None
    assignee_name: str | None
    reporter_id: str | None
    reporter_name: str | None




# Maps internal stage names to optional preferred Settings attributes.
_STAGE_MAP: dict[str, str] = {
    "in_progress":        "jira_status_in_progress",
    "awaiting_approval":  "jira_status_awaiting_approval",
    "in_review":          "jira_status_in_review",
    "blocked":            "jira_status_blocked",
    "done":               "jira_status_done",
}

_STAGE_CATEGORY_MAP: dict[str, str] = {
    "in_progress": _CATEGORY_ACTIVE,
    "awaiting_approval": _CATEGORY_ACTIVE,
    "in_review": _CATEGORY_ACTIVE,
    "blocked": _CATEGORY_ACTIVE,
    "done": _CATEGORY_DONE,
}


class JiraTool:
    """Thin, typed wrapper around the Jira Cloud REST API v3."""

    def __init__(self) -> None:
        self._base = settings.jira_base_url.rstrip("/")
        self._auth = (settings.jira_email, settings.jira_api_token)
        self._project = settings.jira_project_key
        self._status_cache: dict[str, str] | None = None
        self._status_cache_at: datetime | None = None
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _client(self, timeout_seconds: float | None = None) -> httpx.Client:
        return httpx.Client(
            base_url=self._base,
            auth=self._auth,
            headers=self._headers,
            timeout=timeout_seconds or 15,
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

    def _status_category_from_fields(self, fields: dict) -> str | None:
        status = fields.get("status") or {}
        category = status.get("statusCategory") or {}
        key = category.get("key")
        if isinstance(key, str) and key:
            return key
        status_name = status.get("name", "")
        return self.status_category(status_name)

    def fetch_project_statuses(
        self,
        force_refresh: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, str]:
        """Return {status_name: statusCategory.key} for the configured project.

        Jira's status category key is stable across custom workflow names:
        ``new`` (ready), ``indeterminate`` (active), and ``done`` (finished).
        The result is cached briefly and can be force-refreshed by the poller.
        """
        now = datetime.now(timezone.utc)
        ttl = timedelta(seconds=settings.jira_status_cache_ttl_seconds)
        if (
            not force_refresh
            and self._status_cache is not None
            and self._status_cache_at is not None
            and now - self._status_cache_at < ttl
        ):
            return dict(self._status_cache)

        logger.info("jira.fetch_project_statuses project=%r force=%s", self._project, force_refresh)
        mapping: dict[str, str] = {}
        client_cm = (
            self._client()
            if timeout_seconds is None
            else self._client(timeout_seconds=timeout_seconds)
        )
        with client_cm as client:
            resp = client.get(f"/rest/api/3/project/{self._project}/statuses")
            if resp.status_code == 200:
                payload = resp.json()
            else:
                logger.warning(
                    "jira.fetch_project_statuses project endpoint status=%s; falling back to /status",
                    resp.status_code,
                )
                fallback = client.get("/rest/api/3/status")
                fallback.raise_for_status()
                payload = [{"statuses": fallback.json()}]
        for issue_type in payload:
            for status in issue_type.get("statuses", []):
                name = status.get("name")
                category = (status.get("statusCategory") or {}).get("key")
                if isinstance(name, str) and isinstance(category, str):
                    mapping[name] = category

        self._status_cache = mapping
        self._status_cache_at = now
        logger.info("jira.fetch_project_statuses -> %r", mapping)
        return dict(mapping)

    def status_category(self, status_name: str) -> str | None:
        """Resolve a Jira status name to its category key."""
        if not status_name:
            return None
        mapping = self.fetch_project_statuses()
        for name, category in mapping.items():
            if name.lower() == status_name.lower():
                return category
        return None

    def is_ready_status(self, status_name: str) -> bool:
        return self.status_category(status_name) == _CATEGORY_NEW

    def is_active_status(self, status_name: str) -> bool:
        return self.status_category(status_name) == _CATEGORY_ACTIVE

    def is_done_status(self, status_name: str) -> bool:
        return self.status_category(status_name) == _CATEGORY_DONE

    def is_unknown_status(self, status_name: str) -> bool:
        category = self.status_category(status_name)
        return category not in _KNOWN_CATEGORIES

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
            category = self._status_category_from_fields(fields)
            issues.append(
                JiraIssue(
                    key=item["key"],
                    summary=summary,
                    description=description,
                    status=status,
                    status_category=category,
                )
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
        result = JiraIssue(
            key=key,
            summary=summary,
            description=description,
            status=status,
            status_category=self._status_category_from_fields(fields),
        )
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
            category = self._status_category_from_fields(fields)
            issues.append(
                JiraIssue(
                    key=item["key"],
                    summary=summary,
                    description=description,
                    status=status,
                    status_category=category,
                )
            )
        logger.info("jira.list_by_status status=%r -> %d issues", status_name, len(issues))
        return issues

    def list_by_category(self, category_key: str) -> list[JiraIssue]:
        """Return project issues whose current status has ``category_key``."""
        status_names = [
            name
            for name, category in self.fetch_project_statuses(force_refresh=True).items()
            if category == category_key
        ]
        if not status_names:
            logger.warning("jira.list_by_category category=%r has no project statuses", category_key)
            return []

        quoted = ", ".join(f'"{name}"' for name in status_names)
        jql = f"project = {self._project} AND status in ({quoted}) ORDER BY created ASC"
        logger.info("jira.list_by_category category=%r jql=%r", category_key, jql)
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
                JiraIssue(
                    key=item["key"],
                    summary=summary,
                    description=description,
                    status=status,
                    status_category=self._status_category_from_fields(fields),
                )
            )
        logger.info("jira.list_by_category category=%r -> %d issues", category_key, len(issues))
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
                category=(t.get("to", {}).get("statusCategory") or {}).get("key"),
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
        # 1. Resolve the target category and optional preferred status name.
        attr = _STAGE_MAP.get(internal_stage)
        target_category = _STAGE_CATEGORY_MAP.get(internal_stage)
        if not attr or not target_category:
            reason = f"unknown internal_stage {internal_stage!r}; must be one of {list(_STAGE_MAP)}"
            logger.warning("jira.set_status SKIPPED key=%r stage=%r reason=%r", key, internal_stage, reason)
            return {"applied": False, "from": "", "to": None, "reason": reason}

        preferred_name: str = getattr(settings, attr, "").strip()

        # Snapshot current status before the transition attempt.
        current_status = self.get_issue(key)["status"]

        # 2. Fetch allowed transitions from the issue's CURRENT state.
        transitions = self.get_transitions(key)
        logger.info(
            "jira.set_status MATCH_CHECK key=%r stage=%r target_category=%r preferred=%r current=%r transitions=%r",
            key,
            internal_stage,
            target_category,
            preferred_name,
            current_status,
            transitions,
        )

        transition_id: str | None = None
        desired_name: str | None = None

        # 3a. Prefer configured name only when it targets the desired category.
        if preferred_name:
            preferred = next(
                (t for t in transitions if t["name"].lower() == preferred_name.lower()),
                None,
            )
            if preferred and preferred.get("category") == target_category:
                transition_id = preferred["id"]
                desired_name = preferred["name"]

        # 3b. Try configured fallback name when present.
        if transition_id is None:
            fallback_name = settings.jira_status_fallbacks.get(internal_stage, "").strip()
            if fallback_name:
                fallback = next(
                    (t for t in transitions if t["name"].lower() == fallback_name.lower()),
                    None,
                )
                if fallback and fallback.get("category") == target_category:
                    transition_id = fallback["id"]
                    desired_name = fallback["name"]

        # 3c. Category-first default: pick any allowed transition into target category.
        if transition_id is None:
            category_match = next(
                (t for t in transitions if t.get("category") == target_category),
                None,
            )
            if category_match:
                transition_id = category_match["id"]
                desired_name = category_match["name"]

        logger.info(
            "jira.set_status MATCH_RESULT key=%r stage=%r target_category=%r desired=%r matched=%s transition_id=%r",
            key,
            internal_stage,
            target_category,
            desired_name,
            bool(transition_id),
            transition_id,
        )

        if transition_id is None:
            available = [(t["name"], t.get("category")) for t in transitions]
            reason = (
                f"no matching transition for category {target_category!r}; "
                f"preferred={preferred_name!r}; available={available}"
            )
            logger.warning(
                "jira.set_status SKIPPED key=%r stage=%r reason=%r",
                key, internal_stage, reason,
            )
            return {"applied": False, "from": current_status, "to": None, "reason": reason}

        # 4. POST the transition.
        logger.info(
            "jira.set_status APPLYING key=%r stage=%r transition_id=%r name=%r category=%r",
            key, internal_stage, transition_id, desired_name, target_category,
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
        category = self._status_category_from_fields(fields)
        updated = fields.get("updated", "")
        assignee = fields.get("assignee") or {}
        reporter = fields.get("reporter") or {}
        result = JiraIssueDetail(
            key=key,
            summary=summary,
            description=description,
            status=status,
            status_category=category,
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
                status_category=self._status_category_from_fields(f),
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

    def list_in_progress_stuck(self, threshold_minutes: int) -> list["JiraIssueDetail"]:
        """Return active-category issues whose ``updated`` timestamp is older than
        ``threshold_minutes``.

        Fetches all active-category issues and filters by parsed timestamp in Python
        rather than relying on JQL relative-date syntax (portability across Jira
        versions).  Capped at 100 results.
        """
        status_names = [
            name
            for name, category in self.fetch_project_statuses(force_refresh=True).items()
            if category == _CATEGORY_ACTIVE
        ]
        if not status_names:
            logger.info("jira.list_in_progress_stuck: no active-category statuses configured in Jira")
            return []

        quoted = ", ".join(f'"{name}"' for name in status_names)
        jql = (
            f"project = {self._project} AND status in ({quoted}) "
            f"ORDER BY updated ASC"
        )
        logger.info("jira.list_in_progress_stuck threshold_minutes=%d jql=%r", threshold_minutes, jql)
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

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)
        results: list[JiraIssueDetail] = []
        for item in resp.json().get("issues", []):
            f = item.get("fields", {})
            updated_str = f.get("updated", "")
            if updated_str:
                try:
                    updated_dt = datetime.fromisoformat(updated_str).astimezone(timezone.utc)
                except ValueError:
                    updated_dt = None
            else:
                updated_dt = None

            if updated_dt is not None and updated_dt > cutoff:
                continue  # updated recently — not stuck

            summary, description = self._extract(f)
            assignee = f.get("assignee") or {}
            reporter = f.get("reporter") or {}
            results.append(JiraIssueDetail(
                key=item["key"],
                summary=summary,
                description=description,
                status=(f.get("status") or {}).get("name", ""),
                status_category=self._status_category_from_fields(f),
                updated=updated_str,
                assignee_id=assignee.get("accountId"),
                assignee_name=assignee.get("displayName"),
                reporter_id=reporter.get("accountId"),
                reporter_name=reporter.get("displayName"),
            ))

        logger.info("jira.list_in_progress_stuck -> %d stuck issue(s)", len(results))
        return results

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
