"""Deterministic repo resolver — no LLM (R-5, R-6), R-26.

CASCADE (stops at the first step that yields ≥1 candidate):
  1. Remote links attached to the Jira issue (web/remote link objects).
  2. github.com/owner/repo URLs embedded in issue text fields or comments.
  3. Reporter's public GitHub repos (BEST-EFFORT — skipped on any error).
  4. None found → source="needs_paste" so the UI can ask the user to paste a URL.

Each step is independently guarded; failure of one never blocks the next (R-11).
All Jira calls use the same auth as JiraTool (R-17).
"""

import logging
import re
from typing import Literal, NotRequired, TypedDict

import httpx
from github.GithubException import UnknownObjectException

from app.config import settings
from app.db.connection import SessionLocal
from app.db.models import Ticket
from app.tools.jira_tool import JiraTool
from app.tools.repo_tool import RepoAccessRequired, RepoTool

logger = logging.getLogger(__name__)

# Captures "owner/repo" from any github.com/… URL.
# Stops before query-strings, fragments, path continuations, quotes, whitespace.
_GITHUB_RE = re.compile(
    r"github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+?)(?:[/?#\"'\s]|\.git\b|$)"
)


class ResolveResult(TypedDict):
    source: Literal["links", "ticket_text", "reporter", "needs_paste", "invalid_repo", "private_repo"]
    candidates: list[str]  # ["owner/repo", ...]
    error: NotRequired[str]
    invalid_repos: NotRequired[list[str]]
    private_repos: NotRequired[list[str]]


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
    """Extract repos from remote links AND issue links on the Jira issue.

    Checks two sources:
    - /rest/api/3/issue/{key}/remotelink  (web/URL links added via "Link > Web link")
    - fields.issuelinks                   (Jira-to-Jira links; serialised to text and
                                           scanned so any embedded GitHub URL is caught)
    """
    import json as _json

    try:
        urls: list[str] = []

        with _jira_client() as client:
            # Remote (web) links
            rl = client.get(f"/rest/api/3/issue/{key}/remotelink")
            if rl.status_code == 200:
                for link in rl.json():
                    obj = link.get("object", {})
                    for field in ("url", "title", "summary"):
                        val = obj.get(field, "")
                        if isinstance(val, str) and val:
                            urls.append(val)
                    gid = link.get("globalId", "")
                    if gid:
                        urls.append(gid)

            # Issue links (Jira-to-Jira) — serialise entire payload and scan for URLs
            il = client.get(f"/rest/api/3/issue/{key}", params={"fields": "issuelinks"})
            if il.status_code == 200:
                issuelinks = il.json().get("fields", {}).get("issuelinks") or []
                if issuelinks:
                    urls.append(_json.dumps(issuelinks))

        repos = _extract_repos(" ".join(urls))
        logger.info("resolve.step1 key=%r remote+issuelinks -> %r", key, repos)
        return repos
    except Exception as exc:
        logger.warning("resolve.step1 key=%r error=%s; skipping", key, exc)
        return []


def _step2_ticket_text(key: str) -> list[str]:
    """Extract repos from github.com URLs across current Jira text fields."""
    try:
        texts = JiraTool().get_issue_text_fields(key)
        repos = _extract_repos(" ".join(texts.values()))
        logger.info("resolve.step2 key=%r text_fields=%r -> %r", key, list(texts), repos)
        return repos
    except Exception as exc:
        logger.warning("resolve.step2 key=%r error=%s; skipping", key, exc)
        return []


def _step3_reporter(key: str) -> list[str]:
    """Best-effort: derive a GitHub username from the reporter; list their repos."""
    try:
        from app.tools.github_tool import GitHubTool

        github = GitHubTool()
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


def _validate_candidates(candidates: list[str], issue_key: str) -> ResolveResult | None:
    invalid: list[str] = []
    private: list[str] = []
    tool = RepoTool()
    for repo in candidates:
        subtask_id = f"resolve-{issue_key}-{repo.replace('/', '__')}"
        try:
            tool.clone_or_pull(repo, subtask_id)
            tool.cleanup_workspace(subtask_id)
        except RepoAccessRequired as exc:
            logger.warning("resolve.validate repo=%r private_or_inaccessible: %s", repo, exc)
            private.append(repo)
        except Exception as exc:
            logger.warning("resolve.validate repo=%r not reachable: %s", repo, exc)
            invalid.append(repo)

    if private:
        error = "this repo is private — enter a token to access it"
        return ResolveResult(source="private_repo", candidates=private, error=error, private_repos=private)

    if invalid:
        if len(invalid) == 1:
            error = f"repo {invalid[0]} not found or not accessible"
        else:
            error = f"repos {invalid} not found or not accessible"
        return ResolveResult(source="invalid_repo", candidates=[], error=error, invalid_repos=invalid)
    return None


def _refresh_local_ticket_content(issue_key: str) -> None:
    """Refresh only this Jira ticket's cached title/description before resolving."""
    try:
        issue = JiraTool().get_issue(issue_key)
    except Exception as exc:
        logger.warning("resolve.refresh_content key=%r error=%s; continuing", issue_key, exc)
        return

    with SessionLocal() as db:
        ticket = db.query(Ticket).filter(Ticket.external_key == issue_key).first()
        if ticket is None:
            return
        changed = (
            ticket.title != issue["summary"]
            or ticket.description != issue["description"]
        )
        if not changed:
            return
        ticket.title = issue["summary"]
        ticket.description = issue["description"]
        db.commit()
    logger.info("resolve.refresh_content key=%r updated local ticket content", issue_key)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_repos(issue_key: str) -> ResolveResult:
    """Resolve candidate GitHub repos for a Jira issue via cascade.

    Always returns a result — never raises (R-11).
    """
    logger.info("resolve_repos start key=%r", issue_key)
    _refresh_local_ticket_content(issue_key)

    candidates = _step1_remote_links(issue_key)
    if candidates:
        invalid = _validate_candidates(candidates, issue_key)
        if invalid:
            return invalid
        return ResolveResult(source="links", candidates=candidates)

    candidates = _step2_ticket_text(issue_key)
    if candidates:
        invalid = _validate_candidates(candidates, issue_key)
        if invalid:
            return invalid
        return ResolveResult(source="ticket_text", candidates=candidates)

    candidates = _step3_reporter(issue_key)
    if candidates:
        invalid = _validate_candidates(candidates, issue_key)
        if invalid:
            return invalid
        return ResolveResult(source="reporter", candidates=candidates)

    logger.info("resolve_repos key=%r -> needs_paste", issue_key)
    return ResolveResult(source="needs_paste", candidates=[])
