"""Deterministic GitHub integration tool (no LLM — R-5, R-6).

Hard-guarded to only ever touch the single repo named in settings (R-19).
All credentials come from settings (R-17). Every public method is logged (R-7).
"""

import logging

from github import Auth, Github
from github.Repository import Repository

from app.config import settings

logger = logging.getLogger(__name__)


class GitHubTool:
    """Thin, typed wrapper around PyGithub, scoped to one repo only."""

    def __init__(self) -> None:
        self._client = Github(auth=Auth.Token(settings.github_token))
        self._owner = settings.github_owner
        self._repo_name = settings.github_repo

    def _allowed_repo(self, name: str) -> None:
        """Hard guard — raise immediately if caller tries to access any other repo (R-19)."""
        expected = f"{self._owner}/{self._repo_name}"
        if name != expected and name != self._repo_name:
            raise ValueError(
                f"GitHubTool is locked to '{expected}'; access to '{name}' is forbidden."
            )

    def get_repo(self) -> Repository:
        """Return the Repository object for the configured repo."""
        full_name = f"{self._owner}/{self._repo_name}"
        logger.info("github.get_repo repo=%r", full_name)
        self._allowed_repo(full_name)
        repo = self._client.get_repo(full_name)
        logger.info("github.get_repo -> default_branch=%r", repo.default_branch)
        return repo

    def create_branch(self, base: str, new_branch: str) -> None:
        """Create ``new_branch`` off ``base`` in the configured repo.

        Args:
            base: Name of the branch or commit SHA to branch from.
            new_branch: Name for the new branch.
        """
        logger.info("github.create_branch base=%r new=%r", base, new_branch)
        repo = self.get_repo()
        source = repo.get_branch(base)
        repo.create_git_ref(
            ref=f"refs/heads/{new_branch}",
            sha=source.commit.sha,
        )
        logger.info("github.create_branch -> created %r from %r", new_branch, base)

    def open_pr(self, branch: str, title: str, body: str) -> str:
        """Open a pull request from ``branch`` → default branch.

        Returns:
            The HTML URL of the newly created PR.
        """
        logger.info("github.open_pr branch=%r title=%r", branch, title)
        repo = self.get_repo()
        pr = repo.create_pull(
            title=title,
            body=body,
            head=branch,
            base=repo.default_branch,
        )
        logger.info("github.open_pr -> pr_url=%r", pr.html_url)
        return pr.html_url
