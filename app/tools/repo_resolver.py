"""Deterministic repo resolver — no LLM (R-5, R-6), R-26.

CASCADE (stops at the first step that yields ≥1 candidate):
  1. Remote links attached to the Jira issue (web/remote link objects).
  2. github.com/owner/repo URLs embedded in the issue description text.
  3. Reporter's public GitHub repos (BEST-EFFORT — skipped on any error).
  4. None found → source="needs_paste" so the UI can ask the user to paste a URL.

Each step is independently guarded; failure of one never blocks the next (R-11).
All Jira calls use the same auth as JiraTool (R-17).
"""

import logging
import re
from typing import Literal, TypedDict

import httpx
from github.GithubException import UnknownObjectException

from app.config import settings
from app.tools.github_tool import GitHubTool

logger = logging.getLogger(__name__)

# Captures "owner/repo" from any github.com/… URL.
# Stops before query-strings, fragments, path continuations, quotes, whitespace.
_GITHUB_RE = re.compile(
    r"github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+?)(?:[/?#\"'\s]|\.git\b|$)"
)


class ResolveResult(TypedDict):
    source: Literal["links", "description", "reporter", "needs_paste"]
    candidates: list[str]  # ["owner/repo", ...]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _jira_client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.jira_base_url.rstrip("/"),
        auth=(settings.jira_email, settings.jira_api_token),
        headers={"Accept": "application/json"},
        timeout=15,
    )


def _extract_repos(text: str) -> list[str]:
    """Return deduplicated owner/repo strings found in ``text``."""
    seen: set[str] = set()
    result: list[str] = []
    for m in _GITHUB_RE.finditer(text):
        repo = m.group(1).removesuffix(".git").strip("/")
        if repo not in seen:
            seen.add(repo)
            result.append(repo)
    return result


def _step1_remote_links(key: str) -> list[str]:
    """Extract repos from Jira remote links (web links) on the issue."""
    try:
        with _jira_client() as client:
            resp = client.get(f"/rest/api/3/issue/{key}/remotelink")
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
        urls: list[str] = []
        for link in resp.json():
            obj = link.get("object", {})
            for field in ("url", "title"):
                val = obj.get(field, "")
                if val:
                    urls.append(val)
        repos = _extract_repos(" ".join(urls))
        logger.info("resolve.step1 key=%r remote_links -> %r", key, repos)
        return repos
    except Exception as exc:
        logger.warning("resolve.step1 key=%r error=%s; skipping", key, exc)
        return []


def _step2_description(key: str) -> list[str]:
    """Extract repos from github.com URLs in the issue description."""
    try:
        with _jira_client() as client:
            resp = client.get(
                f"/rest/api/3/issue/{key}", params={"fields": "description"}
            )
            resp.raise_for_status()
        lines: list[str] = []
        for block in (resp.json().get("fields", {}).get("description") or {}).get("content", []):
            for inline in block.get("content", []):
                if inline.get("type") == "text":
                    lines.append(inline.get("text", ""))
        repos = _extract_repos(" ".join(lines))
        logger.info("resolve.step2 key=%r description -> %r", key, repos)
        return repos
    except Exception as exc:
        logger.warning("resolve.step2 key=%r error=%s; skipping", key, exc)
        return []


def _step3_reporter(key: str, github: GitHubTool) -> list[str]:
    """Best-effort: derive a GitHub username from the reporter; list their repos."""
    try:
        with _jira_client() as client:
            resp = client.get(
                f"/rest/api/3/issue/{key}", params={"fields": "reporter"}
            )
            resp.raise_for_status()
        reporter = (resp.json().get("fields", {}).get("reporter") or {})
        display_name: str = reporter.get("displayName", "").strip()
        if not display_name:
            return []
        # Try display name as-is, then collapsed (spaces removed).
        for username in dict.fromkeys([display_name, display_name.replace(" ", "")]):
            try:
                user = github._client.get_user(username)
                repos = [r.full_name for r in list(user.get_repos())[:5]]
                if repos:
                    logger.info(
                        "resolve.step3 key=%r username=%r -> %r", key, username, repos
                    )
                    return repos
            except UnknownObjectException:
                continue
        return []
    except Exception as exc:
        logger.warning("resolve.step3 key=%r error=%s; skipping", key, exc)
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_repos(issue_key: str) -> ResolveResult:
    """Resolve candidate GitHub repos for a Jira issue via cascade.

    Always returns a result — never raises (R-11).
    """
    github = GitHubTool()
    logger.info("resolve_repos start key=%r", issue_key)

    candidates = _step1_remote_links(issue_key)
    if candidates:
        return ResolveResult(source="links", candidates=candidates)

    candidates = _step2_description(issue_key)
    if candidates:
        return ResolveResult(source="description", candidates=candidates)

    candidates = _step3_reporter(issue_key, github)
    if candidates:
        return ResolveResult(source="reporter", candidates=candidates)

    logger.info("resolve_repos key=%r -> needs_paste", issue_key)
    return ResolveResult(source="needs_paste", candidates=[])
