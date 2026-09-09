from app.tools import repo_resolver
from app.tools.repo_tool import RepoAccessRequired


class _FakeJiraTool:
    def get_issue(self, key: str) -> dict:
        return {"summary": "Summary", "description": "Description"}

    def get_issue_text_fields(self, key: str) -> dict[str, str]:
        return {
            "summary": "No repo here",
            "description": "No repo here either",
            "environment": "env has https://github.com/owner/from-env",
            "comments": "comment has https://github.com/owner/from-comment",
        }


class _FakeGithubTool:
    def get_repo(self, full_name: str):
        return object()


class _FakeRepoTool:
    def clone_or_pull(self, full_name: str, subtask_id: str):
        return object()

    def cleanup_workspace(self, subtask_id: str) -> None:
        pass


def test_resolve_repos_scans_all_ticket_text_fields(monkeypatch) -> None:
    monkeypatch.setattr(repo_resolver, "JiraTool", _FakeJiraTool)
    monkeypatch.setattr(repo_resolver, "RepoTool", _FakeRepoTool)
    monkeypatch.setattr(repo_resolver, "_step1_remote_links", lambda _key: [])
    monkeypatch.setattr(repo_resolver, "_refresh_local_ticket_content", lambda _key: None)

    result = repo_resolver.resolve_repos("SCRUM-1")

    assert result == {
        "source": "ticket_text",
        "candidates": ["owner/from-env", "owner/from-comment"],
    }


def test_resolve_repos_refuses_unreachable_candidate(monkeypatch) -> None:
    class _RejectingRepoTool:
        def clone_or_pull(self, full_name: str, subtask_id: str):
            raise RuntimeError("not found")

        def cleanup_workspace(self, subtask_id: str) -> None:
            pass

    monkeypatch.setattr(repo_resolver, "JiraTool", _FakeJiraTool)
    monkeypatch.setattr(repo_resolver, "RepoTool", _RejectingRepoTool)
    monkeypatch.setattr(repo_resolver, "_step1_remote_links", lambda _key: [])
    monkeypatch.setattr(repo_resolver, "_refresh_local_ticket_content", lambda _key: None)

    result = repo_resolver.resolve_repos("SCRUM-1")

    assert result["source"] == "invalid_repo"
    assert result["candidates"] == []
    assert result["invalid_repos"] == ["owner/from-env", "owner/from-comment"]
    assert "Validating repository owner/from-env failed (RuntimeError): not found" in result["error"]
    assert "Validating repository owner/from-comment failed (RuntimeError): not found" in result["error"]


def test_resolve_repos_prompts_for_private_repo_token(monkeypatch) -> None:
    class _PrivateRepoTool:
        def clone_or_pull(self, full_name: str, subtask_id: str):
            raise RepoAccessRequired(full_name)

        def cleanup_workspace(self, subtask_id: str) -> None:
            pass

    monkeypatch.setattr(repo_resolver, "JiraTool", _FakeJiraTool)
    monkeypatch.setattr(repo_resolver, "RepoTool", _PrivateRepoTool)
    monkeypatch.setattr(repo_resolver, "_step1_remote_links", lambda _key: [])
    monkeypatch.setattr(repo_resolver, "_refresh_local_ticket_content", lambda _key: None)

    result = repo_resolver.resolve_repos("SCRUM-1")

    assert result == {
        "source": "private_repo",
        "candidates": ["owner/from-env", "owner/from-comment"],
        "error": "this repo is private — enter a token to access it",
        "private_repos": ["owner/from-env", "owner/from-comment"],
    }
