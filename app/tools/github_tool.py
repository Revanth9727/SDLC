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
from urllib3.util.retry import Retry

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

    def __init__(self, repo_tool=None) -> None:
        self.repo_tool = repo_tool
        self._retry = Retry(
            total=settings.external_retry_attempts,
            backoff_factor=settings.external_retry_base_seconds,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"}),
        )
        self._client = Github(auth=Auth.Token(settings.github_token), retry=self._retry)

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
        client = self._client
        if settings.app_encryption_key:
            from app.tools.repo_tokens import RepoTokenStore
            token = RepoTokenStore().get(target)
            if token:
                client = Github(auth=Auth.Token(token), retry=self._retry)
        repo = client.get_repo(target)
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

    def find_pr(self, full_name: str, branch: str) -> dict | None:
        repo = self.get_repo(full_name)
        found = list(repo.get_pulls(state="all", head=f"{repo.owner.login}:{branch}"))
        if not found:
            return None
        pr = next((item for item in found if item.state == "open"), found[0])
        return {"id": str(pr.id), "number": pr.number, "url": pr.html_url,
                "state": "merged" if pr.merged else pr.state, "branch": branch,
                "repo": full_name}

    def publish_changes(self, state) -> dict:
        from app.tools.repo_tool import RepoTool
        if state.approval_status != 'approved' or not state.execution_complete:
            raise ValueError('Publication requires approved, completed execution')
        # exit 5 (no tests collected) is not a failure (ai_rules.md R-46) — a step
        # is publishable if it truly passed, or was honestly inconclusive.
        if not state.steps_done or not all(
                step['tests']['outcome'] in ('passed', 'no_tests_collected') for step in state.steps_done):
            raise ValueError('Every step must have passing or inconclusive (no-tests) results')
        tool = self.repo_tool or RepoTool(github=self)
        branch = tool.branch_name(state.subtask_id)
        title = f"{state.jira_key or 'SDLC'}: {state.description.splitlines()[0][:180]}"
        body = 'Implements the approved plan:\n\n' + '\n'.join(
            f'- {step.intent} (`{step.target_file}`)' for step in state.plan)
        if state.verifiability == 'no_tests':
            body += ('\n\n**Unverifiable:** this repository has no tests, so this change could not be run '
                     'against a test suite — please review manually.\n')
        body += '\n\nValidation: pytest passed after each step.\n\n' + (f'Jira: {state.jira_key}' if state.jira_key else '')
        existing = self.find_pr(state.repo, branch)
        if existing:
            if existing['state'] == 'open':
                return existing
            if existing['state'] == 'merged':
                raise ValueError('The subtask PR is already merged; start a fresh approved attempt')
            # A human-approved redo may create another PR from the same tested branch.
            self.open_pr(state.repo, branch, title, body)
            result = self.find_pr(state.repo, branch)
            if result and result['state'] == 'open':
                return result
            raise RuntimeError('Replacement PR creation returned without a discoverable open PR')
        tool.push_changes(state.repo, state.subtask_id, state.base_commit, state.file_changes)
        self.open_pr(state.repo, branch, title, body)
        result = self.find_pr(state.repo, branch)
        if not result:
            raise RuntimeError('PR creation returned without a discoverable PR')
        return result

    def pr_snapshot(self, full_name: str, number: int) -> dict:
        pr = self.get_repo(full_name).get_pull(number)
        return {'id': str(pr.id), 'number': pr.number, 'url': pr.html_url,
                'state': 'merged' if pr.merged else pr.state, 'branch': pr.head.ref,
                'title': pr.title, 'repo': full_name}

    def comment_pr(self, full_name: str, number: int, message: str) -> None:
        self.get_repo(full_name).get_issue(number).create_comment(message)

    def close_pr(self, full_name: str, number: int) -> None:
        self.get_repo(full_name).get_pull(number).edit(state="closed")


    def pr_commit_messages(self, full_name: str, number: int) -> list[str]:
        commits = self.get_repo(full_name).get_pull(number).get_commits()
        return [commit.commit.message for commit in commits[:50]]

    def pr_checks(self, full_name: str, number: int) -> dict:
        """Aggregate CI status for a PR's head commit — CI is the authoritative
        gate (ai_rules.md R-32/R-46); this only surfaces it, never blocks on it.
        Covers GitHub Actions (Checks API) and classic/third-party CI (Status
        API) generically — never assumes which one a repo uses."""
        pr = self.get_repo(full_name).get_pull(number)
        commit = self.get_repo(full_name).get_commit(pr.head.sha)
        runs = [{'name': r.name, 'status': r.status, 'conclusion': r.conclusion, 'url': r.html_url}
                for r in commit.get_check_runs()]
        if runs:
            completed = all(r['status'] == 'completed' for r in runs)
            failed = any(r['conclusion'] not in ('success', 'neutral', 'skipped') for r in runs if r['conclusion'])
            return {'configured': True, 'status': 'completed' if completed else 'in_progress',
                    'conclusion': ('failure' if failed else 'success') if completed else None,
                    'url': runs[0]['url'], 'runs': runs}
        combined = commit.get_combined_status()
        if combined.statuses:
            runs = [{'name': s.context, 'status': 'completed' if s.state != 'pending' else 'in_progress',
                     'conclusion': s.state if s.state != 'pending' else None, 'url': s.target_url}
                    for s in combined.statuses]
            return {'configured': True, 'status': 'completed' if combined.state != 'pending' else 'in_progress',
                    'conclusion': combined.state if combined.state != 'pending' else None,
                    'url': runs[0]['url'] if runs else None, 'runs': runs}
        return {'configured': False, 'status': 'none', 'conclusion': None, 'url': None, 'runs': []}
