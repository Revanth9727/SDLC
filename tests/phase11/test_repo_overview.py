from pathlib import Path
from types import SimpleNamespace

from app.agents.planner import PlannerAgent
from app.agents.state import SubtaskState
from app.repo_intelligence import overview as module


class FakeRepoTool:
    def __init__(self, checkout: Path):
        self.checkout = checkout

    def clone_or_pull(self, _repo, _workspace):
        return self.checkout

    def revision(self, _repo, _workspace):
        return "abc123"


def test_overview_reuses_ready_inventory_without_scanning(monkeypatch, tmp_path):
    snapshot = SimpleNamespace(id="snapshot-1", commit_sha="abc123", excluded_count=2)
    monkeypatch.setattr(module, "_git", lambda *args, **kwargs: "main")
    monkeypatch.setattr(module, "_access_scope", lambda _repo: "public")
    monkeypatch.setattr(module, "active_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(module, "snapshot_facts", lambda *args: [
        SimpleNamespace(path="orders/api.py"), SimpleNamespace(path="tests/test_api.py")
    ])
    monkeypatch.setattr(module, "_select_files", lambda *args: (_ for _ in ()).throw(
        AssertionError("a ready inventory must be reused")
    ))

    result = module.build_repo_overview("owner/repo", "ticket-overview", repo_tool=FakeRepoTool(tmp_path))

    assert result["source"] == "ready_snapshot"
    assert result["files"] == ["orders/api.py", "tests/test_api.py"]
    assert result["modules"] == ["orders", "tests"]


def test_overview_fallback_is_inventory_only(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(module, "_git", lambda *args, **kwargs: "main")
    monkeypatch.setattr(module, "_access_scope", lambda _repo: "public")
    monkeypatch.setattr(module, "active_snapshot", lambda *args: None)
    monkeypatch.setattr(module, "_repository", lambda *args: SimpleNamespace(id="repo-1"))
    monkeypatch.setattr(module, "_policy", lambda _repo_id: {"inventory": True})
    monkeypatch.setattr(module, "_select_files", lambda checkout, policy: (
        calls.append((checkout, policy)) or (["src/orders.py", "pyproject.toml"], 4)
    ))

    result = module.build_repo_overview("owner/repo", "ticket-overview", repo_tool=FakeRepoTool(tmp_path))

    assert calls == [(tmp_path, {"inventory": True})]
    assert result["source"] == "checkout_inventory"
    assert result["snapshot_id"] is None
    assert result["directories"] == [{"path": ".", "file_count": 1}, {"path": "src", "file_count": 1}]
    assert result["modules"] == [".", "src"]


def test_planner_receives_repository_overview():
    class LLM:
        def __init__(self):
            self.user = ""

        def complete_json(self, _system, user, _schema, **_kwargs):
            self.user = user
            return {"subtasks": [{"spec_id": "1", "type": "bug", "description": "Fix it",
                                  "repo": "owner/repo", "depends_on": []}],
                    "reasoning": "The src module owns it", "CannotDecompose": None}

        def get_usage(self, _ticket_id):
            return {"calls": 1, "tokens": 1, "est_cost_usd": 0, "elapsed_seconds": 0}

    llm = LLM()
    state = SubtaskState(ticket_id="ticket", subtask_id="coordinator", subtask_type="bug",
                         description="Fix orders", repo="owner/repo", confirmed_repos=["owner/repo"],
                         repo_overview=[{"repo": "owner/repo", "files": ["src/orders.py"],
                                        "directories": [{"path": "src", "file_count": 1}],
                                        "modules": ["src"]}])

    PlannerAgent(llm=llm).run(state)

    assert '"repository_overview"' in llm.user
    assert 'src/orders.py' in llm.user
