"""Deterministic local repository reader (no LLM — R-5, R-6).

Repos are passed explicitly as ``owner/repo``. The sandbox repo from settings is
only a convenience for manual testing before the Planner assigns ``SubtaskState.repo``.
"""

import logging
import hashlib
import os
import shutil
import subprocess
import tempfile
from base64 import b64encode
from pathlib import Path

from app.config import settings
from app.tools.github_tool import GitHubTool
from app.tools.repo_tokens import RepoTokenStore

logger = logging.getLogger(__name__)


class RepoAccessRequired(RuntimeError):
    """Raised when a repo needs a private token before it can be cloned."""

    def __init__(self, full_name: str) -> None:
        self.full_name = full_name
        super().__init__(f"this repo is private — enter a token to access it: {full_name}")


class RepoTool:
    """Clone, refresh, list, and read files from GitHub repos."""

    def __init__(
        self,
        github: GitHubTool | None = None,
        token_store: RepoTokenStore | None = None,
        workspace_root: str | Path | None = None,
        cache_root: str | Path | None = None,
    ) -> None:
        self.github = github or GitHubTool()
        self.token_store = token_store
        configured = settings.workspace_root.strip()
        legacy = settings.repo_cache_dir.strip()
        root = (
            workspace_root
            or cache_root
            or configured
            or legacy
            or Path(tempfile.gettempdir()) / "agentic-workspaces"
        )
        self.workspace_root = Path(root).expanduser().resolve()

    def clone_or_pull(self, full_name: str | None = None, subtask_id: str | None = None) -> Path:
        """Return a fresh shallow checkout for this repo in a subtask workspace."""
        target = full_name or f"{settings.github_owner}/{settings.github_repo}"
        workspace_id = self._workspace_id(subtask_id)
        self._validate_full_name(target)
        checkout = self._checkout_path(target, workspace_id)
        checkout.parent.mkdir(parents=True, exist_ok=True)

        if not (checkout / ".git").exists():
            if checkout.exists():
                shutil.rmtree(checkout)
            logger.info("repo.clone.public repo=%r path=%s", target, checkout)
            try:
                self._clone_public(target, checkout)
            except RuntimeError as public_error:
                token = self._stored_token(target)
                if not token:
                    raise RepoAccessRequired(target) from public_error
                logger.info("repo.clone.private repo=%r path=%s token=<masked>", target, checkout)
                self._clone_with_token(target, checkout, token)
                self._git(["remote", "set-url", "origin", self._public_remote_url(target)], cwd=checkout)
        else:
            logger.info("repo.refresh.public repo=%r path=%s", target, checkout)
            try:
                self._git(["pull", "--ff-only", "--depth", "1", "origin"], cwd=checkout)
            except RuntimeError as public_error:
                token = self._stored_token(target)
                if not token:
                    raise RepoAccessRequired(target) from public_error
                logger.info("repo.refresh.private repo=%r path=%s token=<masked>", target, checkout)
                self._git(
                    [
                        "-c",
                        self._auth_header(token),
                        "pull",
                        "--ff-only",
                        "--depth",
                        "1",
                        "origin",
                    ],
                    cwd=checkout,
                )

        return checkout

    def cleanup_workspace(self, subtask_id: str) -> None:
        """Delete one subtask's isolated workspace directory."""
        workspace = self._workspace_path(self._workspace_id(subtask_id))
        if workspace.exists():
            logger.info("repo.cleanup_workspace subtask_id=%r path=%s", subtask_id, workspace)
            shutil.rmtree(workspace)

    def list_files(self, full_name: str | None = None, subtask_id: str | None = None) -> list[str]:
        """List tracked files in the local checkout."""
        checkout = self.clone_or_pull(full_name, subtask_id)
        result = self._git(["ls-files"], cwd=checkout)
        files = [line for line in result.stdout.splitlines() if line.strip()]
        logger.info("repo.list_files repo=%r subtask_id=%r -> %d file(s)", full_name, subtask_id, len(files))
        return files

    def read_file(self, full_name: str | None, subtask_id: str | None, path: str) -> str:
        """Read a UTF-8 text file from within the checkout."""
        checkout = self.clone_or_pull(full_name, subtask_id)
        file_path = self._safe_path(checkout, path)
        if not file_path.is_file():
            raise FileNotFoundError(f"{path!r} is not a file in {full_name!r}")
        text = file_path.read_text(encoding="utf-8")
        logger.info(
            "repo.read_file repo=%r subtask_id=%r path=%r bytes=%d",
            full_name,
            subtask_id,
            path,
            len(text.encode("utf-8")),
        )
        return text

    def _workspace_path(self, subtask_id: str) -> Path:
        return self.workspace_root / subtask_id

    def _checkout_path(self, full_name: str, subtask_id: str) -> Path:
        safe_name = full_name.replace("/", "__")
        return self._workspace_path(subtask_id) / safe_name

    def _clone_public(self, full_name: str, checkout: Path) -> None:
        self._git(
            [
                "clone",
                "--depth",
                "1",
                self._public_remote_url(full_name),
                str(checkout),
            ],
            cwd=checkout.parent,
        )

    def _clone_with_token(self, full_name: str, checkout: Path, token: str) -> None:
        self._git(
            [
                "-c",
                self._auth_header(token),
                "clone",
                "--depth",
                "1",
                self._public_remote_url(full_name),
                str(checkout),
            ],
            cwd=checkout.parent,
        )

    def _public_remote_url(self, full_name: str) -> str:
        return f"https://github.com/{full_name}.git"

    def _stored_token(self, full_name: str) -> str | None:
        if self.token_store is None:
            try:
                self.token_store = RepoTokenStore()
            except ValueError as exc:
                logger.warning("repo.token_store unavailable for repo=%r: %s", full_name, exc)
                return None
        return self.token_store.get(full_name)

    @staticmethod
    def _auth_header(token: str) -> str:
        value = b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        return f"http.https://github.com/.extraheader=AUTHORIZATION: basic {value}"

    def _git(self, args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        cmd = ["git", *args]
        safe_cmd = [
            "git",
            *[
                "<auth-header>" if "AUTHORIZATION:" in arg else "<remote>" if "x-access-token:" in arg else arg
                for arg in args
            ],
        ]
        logger.debug("repo.git cwd=%s cmd=%r", cwd, safe_cmd)
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            stderr = result.stderr.replace(settings.github_token, "***")
            for arg in args:
                if "AUTHORIZATION:" in arg:
                    stderr = stderr.replace(arg, "<auth-header>")
            raise RuntimeError(f"git {' '.join(safe_cmd)} failed: {stderr.strip()}")
        return result

    def _safe_path(self, checkout: Path, rel_path: str) -> Path:
        candidate = (checkout / rel_path).resolve()
        if candidate != checkout and checkout not in candidate.parents:
            raise ValueError(f"path escapes checkout: {rel_path!r}")
        return candidate

    @staticmethod
    def _validate_full_name(full_name: str) -> None:
        import re
        parts = full_name.split("/")
        if len(parts) != 2 or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", p) for p in parts):
            raise ValueError("repo full_name must be 'owner/repo'")

    @staticmethod
    def _workspace_id(subtask_id: str | None) -> str:
        if not subtask_id:
            raise ValueError("subtask_id is required for isolated workspaces")
        if "/" in subtask_id or "\\" in subtask_id or subtask_id in {".", ".."}:
            raise ValueError("subtask_id must be a simple directory name")
        return subtask_id

    def revision(self, full_name: str, subtask_id: str) -> str:
        checkout = self._checkout_path(full_name, self._workspace_id(subtask_id))
        return self._git(["rev-parse", "HEAD"], cwd=checkout).stdout.strip()

    def file_fingerprints(self, full_name: str, subtask_id: str,
                          paths: list[str]) -> dict[str, str | None]:
        """Hash selected files in the current isolated checkout; missing is explicit."""
        checkout = self._checkout_path(full_name, self._workspace_id(subtask_id))
        result: dict[str, str | None] = {}
        for path in dict.fromkeys(paths):
            target = self._safe_path(checkout, path)
            result[path] = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        return result

    def verify_freshness(self, full_name: str, subtask_id: str,
                         expected: dict[str, str | None]) -> tuple[str, list[str]]:
        """Refresh default branch and report only diagnosed target files that drifted."""
        self.clone_or_pull(full_name, subtask_id)
        current = self.file_fingerprints(full_name, subtask_id, list(expected))
        drifted = [path for path, fingerprint in expected.items() if current.get(path) != fingerprint]
        return self.revision(full_name, subtask_id), drifted

    def prepare_execution(self, full_name: str, subtask_id: str, base_commit: str,
                          changes: dict[str, str]) -> Path:
        """Rebuild only this disposable workspace from its checkpoint artifacts."""
        if not base_commit:
            raise ValueError("No diagnosis revision recorded; run diagnosis again before editing")
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.workspace_root).free < settings.workspace_min_free_mb * 1024 * 1024:
            raise RuntimeError("Insufficient workspace disk space")
        self.cleanup_workspace(subtask_id)
        checkout = self.clone_or_pull(full_name, subtask_id)
        if self.revision(full_name, subtask_id) != base_commit:
            raise ValueError("Repository changed since diagnosis; a fresh plan and approval are required")
        self._git(["checkout", "-b", self.branch_name(subtask_id)], cwd=checkout)
        for path, content in changes.items():
            if content is None:
                self.delete_execution_file(checkout, path)
            else:
                self.write_execution_file(checkout, path, content)
        return checkout

    @staticmethod
    def branch_name(subtask_id: str) -> str:
        import uuid
        return f"sdlc/{uuid.UUID(subtask_id)}"

    def execution_file(self, checkout: Path, path: str) -> Path:
        from app.agents.planning import Step
        Step(step_id="path", intent="validate", target_file=path)
        if any(part in {'.git', '.env'} for part in Path(path).parts):
            raise ValueError("Editing git metadata or secrets is forbidden")
        candidate = self._safe_path(checkout, path)
        probe = checkout / path
        while probe != checkout:
            if probe.is_symlink():
                raise ValueError("Editing symlinks is not supported")
            probe = probe.parent
        return candidate

    def write_execution_file(self, checkout: Path, path: str, content: str) -> None:
        target = self.execution_file(checkout, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')

    def delete_execution_file(self, checkout: Path, path: str) -> None:
        target = self.execution_file(checkout, path)
        if target.exists():
            target.unlink()

    def push_changes(self, full_name: str, subtask_id: str, base_commit: str,
                     changes: dict[str, str]) -> str:
        """Rebuild approved tested artifacts, commit only those paths, push a scoped branch."""
        if not changes:
            raise ValueError("No tested changes to publish")
        checkout = self.prepare_execution(full_name, subtask_id, base_commit, changes)
        branch = self.branch_name(subtask_id)
        written = [path for path, content in changes.items() if content is not None]
        deleted = [path for path, content in changes.items() if content is None]
        if written:
            self._git(["add", "--", *written], cwd=checkout)
        if deleted:
            self._git(["rm", "--ignore-unmatch", "--", *deleted], cwd=checkout)
        self._git(["-c", f"user.name={settings.git_author_name}", "-c",
                   f"user.email={settings.git_author_email}", "commit", "-m",
                   f"Apply approved SDLC subtask {subtask_id}"], cwd=checkout)
        token = self._stored_token(full_name) or settings.github_token
        # A retry after a successful push verifies the existing tree; it never
        # force-pushes or overwrites a branch someone else changed.
        remote = self._git(["-c", self._auth_header(token), "ls-remote", "--heads", "origin", branch], cwd=checkout)
        if remote.stdout.strip():
            self._git(["-c", self._auth_header(token), "fetch", "--depth", "1", "origin", branch], cwd=checkout)
            local_tree = self._git(["rev-parse", "HEAD^{tree}"], cwd=checkout).stdout.strip()
            remote_tree = self._git(["rev-parse", "FETCH_HEAD^{tree}"], cwd=checkout).stdout.strip()
            if local_tree != remote_tree:
                raise ValueError("Existing subtask branch differs from the tested change; refusing to overwrite")
        else:
            self._git(["-c", self._auth_header(token), "push", "origin", f"HEAD:refs/heads/{branch}"], cwd=checkout)
        return branch
