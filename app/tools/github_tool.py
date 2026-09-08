"""Deterministic GitHub integration tool (no LLM — R-5, R-6).

Repos are resolved per-ticket at runtime (architecture.md §5a, R-26) and confirmed
by the human before use. GITHUB_REPO in settings is the sandbox for local testing
only — never assume it is the target in real runs.
All credentials come from settings (R-17). Every public method is logged (R-7).
"""

import logging
from typing import Optional

from github import Auth, Github
from github.Repository import Repository

from app.config import settings

logger = logging.getLogger(__name__)

# Allowlist hook: if non-empty, only repos in this set are permitted.
# Populated at startup from env in a later phase; empty = allow all (safe during dev).
REPO_ALLOWLIST: set[str] = set()


def _sandbox_full_name() -> str:
    return f"{settings.github_owner}/{settings.github_repo}"


class GitHubTool:
    """Thin, typed wrapper around PyGithub.

    All methods accept a ``full_name`` ("owner/repo") argument. When omitted,
    they default to the sandbox repo from settings — useful for local testing and
    the debug routes. In production runs the resolved, confirmed repo is always
    passed explicitly.
    """

    def __init__(self) -> None:
        self._client = Github(auth=Auth.Token(settings.github_token))

    def _check_allowlist(self, full_name: str) -> None:
        """Allowlist hook (R-19). Logs a warning if the repo isn't in the list.

        Does NOT raise — the allowlist is enforced at the confirm-gate level (1.5).
        Here it only warns so callers are never silently blocked during dev.
        """
        if REPO_ALLOWLIST and full_name not in REPO_ALLOWLIST:
            logger.warning(
                "github: repo %r is not in REPO_ALLOWLIST %r — proceeding (allowlist "
                "enforcement belongs at the confirm-gate, not here)",
                full_name,
                REPO_ALLOWLIST,
            )

    def get_repo(self, full_name: Optional[str] = None) -> Repository:
        """Return the Repository object for ``full_name`` ("owner/repo").

        Defaults to the sandbox repo from settings when ``full_name`` is omitted.
        """
        target = full_name or _sandbox_full_name()
        logger.info("github.get_repo repo=%r", target)
        self._check_allowlist(target)
        repo = self._client.get_repo(target)
        logger.info("github.get_repo -> default_branch=%r", repo.default_branch)
        return repo

    def create_branch(self, full_name: str, base: str, new_branch: str) -> None:
        """Create ``new_branch`` off ``base`` in the given repo.

        Args:
            full_name: "owner/repo" of the target repository.
            base: Branch name or commit SHA to branch from.
            new_branch: Name for the new branch.
        """
        logger.info("github.create_branch repo=%r base=%r new=%r", full_name, base, new_branch)
        repo = self.get_repo(full_name)
        source = repo.get_branch(base)
        repo.create_git_ref(
            ref=f"refs/heads/{new_branch}",
            sha=source.commit.sha,
        )
        logger.info("github.create_branch -> created %r from %r", new_branch, base)

    def open_pr(self, full_name: str, branch: str, title: str, body: str) -> str:
        """Open a pull request from ``branch`` → default branch in the given repo.

        Args:
            full_name: "owner/repo" of the target repository.
            branch: Head branch to open the PR from.
            title: PR title.
            body: PR description body.

        Returns:
            The HTML URL of the newly created PR.
        """
        logger.info("github.open_pr repo=%r branch=%r title=%r", full_name, branch, title)
        repo = self.get_repo(full_name)
        pr = repo.create_pull(
            title=title,
            body=body,
            head=branch,
            base=repo.default_branch,
        )
        logger.info("github.open_pr -> pr_url=%r", pr.html_url)
        return pr.html_url
