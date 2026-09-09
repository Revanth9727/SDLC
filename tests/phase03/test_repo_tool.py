from pathlib import Path
from types import SimpleNamespace

import pytest

from app.tools.repo_tool import RepoAccessRequired, RepoTool


class _FakeGithub:
    def __init__(self, branch: str = "main") -> None:
        self.branch = branch
        self.seen: list[str] = []

    def get_repo(self, full_name: str):
        self.seen.append(full_name)
        return SimpleNamespace(default_branch=self.branch)


class _FakeCompleted:
    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout


class _FakeRepoTool(RepoTool):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(github=_FakeGithub(), workspace_root=tmp_path)
        self.git_calls: list[tuple[list[str], Path]] = []

    def _git(self, args: list[str], *, cwd: Path):
        self.git_calls.append((args, cwd))
        if args[0] == "clone":
            checkout = Path(args[-1])
            (checkout / ".git").mkdir(parents=True)
            (checkout / "app.py").write_text("print('hello')\n", encoding="utf-8")
            (checkout / "pkg").mkdir()
            (checkout / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
            return _FakeCompleted()
        if args == ["ls-files"]:
            return _FakeCompleted("app.py\npkg/mod.py\n")
        return _FakeCompleted()


def test_clone_or_pull_clones_default_branch_into_cache(tmp_path: Path) -> None:
    tool = _FakeRepoTool(tmp_path)

    checkout = tool.clone_or_pull("owner/repo", "subtask-1")

    assert checkout.exists()
    assert checkout == tmp_path / "subtask-1" / "owner__repo"
    assert tool.git_calls[0][0][:3] == ["clone", "--depth", "1"]
    assert tool.git_calls[0][0][3] == "https://github.com/owner/repo.git"
    assert "x-access-token" not in " ".join(tool.git_calls[0][0])


def test_clone_or_pull_refreshes_existing_checkout(tmp_path: Path) -> None:
    tool = _FakeRepoTool(tmp_path)
    tool.clone_or_pull("owner/repo", "subtask-1")
    tool.git_calls.clear()

    tool.clone_or_pull("owner/repo", "subtask-1")

    assert [call[0][0] for call in tool.git_calls] == ["pull"]


def test_list_files_and_read_file(tmp_path: Path) -> None:
    tool = _FakeRepoTool(tmp_path)

    assert tool.list_files("owner/repo", "subtask-1") == ["app.py", "pkg/mod.py"]
    assert tool.read_file("owner/repo", "subtask-1", "app.py") == "print('hello')\n"


def test_read_file_rejects_path_traversal(tmp_path: Path) -> None:
    tool = _FakeRepoTool(tmp_path)

    with pytest.raises(ValueError, match="escapes checkout"):
        tool.read_file("owner/repo", "subtask-1", "../secret.txt")


def test_cleanup_workspace_deletes_only_subtask_dir(tmp_path: Path) -> None:
    tool = _FakeRepoTool(tmp_path)
    tool.clone_or_pull("owner/repo", "subtask-1")
    other = tmp_path / "subtask-2"
    other.mkdir()

    tool.cleanup_workspace("subtask-1")

    assert not (tmp_path / "subtask-1").exists()
    assert other.exists()


def test_subtask_id_is_required(tmp_path: Path) -> None:
    tool = _FakeRepoTool(tmp_path)

    with pytest.raises(ValueError, match="subtask_id is required"):
        tool.clone_or_pull("owner/repo")


def test_private_repo_without_saved_token_requests_token(tmp_path: Path) -> None:
    class _FailingPublicCloneTool(_FakeRepoTool):
        def _git(self, args: list[str], *, cwd: Path):
            self.git_calls.append((args, cwd))
            if args[0] == "clone":
                raise RuntimeError("authentication failed")
            return _FakeCompleted()

    tool = _FailingPublicCloneTool(tmp_path)

    with pytest.raises(RepoAccessRequired, match="enter a token"):
        tool.clone_or_pull("owner/private", "subtask-1")


def test_private_repo_with_saved_token_retries_with_auth_header(tmp_path: Path) -> None:
    class _TokenStore:
        def get(self, full_name: str) -> str:
            return "private token"

    class _FailingPublicCloneTool(_FakeRepoTool):
        def _git(self, args: list[str], *, cwd: Path):
            self.git_calls.append((args, cwd))
            if args[0] == "clone":
                raise RuntimeError("authentication failed")
            if args[0] == "-c" and args[2] == "clone":
                checkout = Path(args[-1])
                (checkout / ".git").mkdir(parents=True)
                return _FakeCompleted()
            return _FakeCompleted()

    tool = _FailingPublicCloneTool(tmp_path)
    tool.token_store = _TokenStore()

    checkout = tool.clone_or_pull("owner/private", "subtask-1")

    assert checkout.exists()
    assert tool.git_calls[1][0][0] == "-c"
    assert "AUTHORIZATION: basic" in tool.git_calls[1][0][1]
