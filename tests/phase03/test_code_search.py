import subprocess
from pathlib import Path

import pytest

from app.tools.code_search import CodeSearchTool
from app.tools.repo_tool import RepoTool


@pytest.fixture
def checkout(tmp_path: Path):
    root = tmp_path / "subtask-1" / "owner__repo"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    (root / "orders.py").write_text(
        "def convert_currency(value):\n    return value\n\n"
        "def calculate_total(items):\n    subtotal = sum(items)\n    return convert_currency(subtotal)\n",
        encoding="utf-8",
    )
    (root / "api.py").write_text(
        "from orders import calculate_total\n\ndef checkout(items):\n    return calculate_total(items)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base"],
        cwd=root, check=True,
    )
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    return tmp_path, root, base


def tool_for(root: Path, events=None):
    return CodeSearchTool(RepoTool(workspace_root=root), event_sink=events.append if events is not None else None)


def test_exact_symbol_and_relationship_search(checkout):
    root, _repo, _base = checkout
    events = []
    tool = tool_for(root, events)

    assert tool.search_exact("owner/repo", "subtask-1", "calculate_total")[0]["path"] in {"api.py", "orders.py"}
    assert tool.find_symbol("owner/repo", "subtask-1", "calculate_total") == [
        {"path": "orders.py", "line": 4, "column": 5, "text": "def calculate_total(items):"}
    ]
    assert tool.get_callers("owner/repo", "subtask-1", "calculate_total")[0]["path"] == "api.py"
    assert tool.get_callees("owner/repo", "subtask-1", "calculate_total") == [
        {"symbol": "sum", "path": "orders.py", "line": 5},
        {"symbol": "convert_currency", "path": "orders.py", "line": 6},
    ]
    assert any(event["tool"] == "get_callees" for event in events)


def test_file_slice_and_commit_diff_are_bounded(checkout):
    root, repo, base = checkout
    tool = tool_for(root)
    sliced = tool.get_file("owner/repo", "subtask-1", "orders.py", 4, 5)
    assert sliced["content"].startswith("def calculate_total")
    assert "convert_currency(subtotal)" not in sliced["content"]

    (repo / "orders.py").write_text((repo / "orders.py").read_text() + "\nTAX = 0.1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "tax"],
        cwd=repo, check=True,
    )
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert "+TAX = 0.1" in tool.get_diff("owner/repo", "subtask-1", base, head)


def test_file_slice_rejects_path_traversal(checkout):
    root, _repo, _base = checkout
    with pytest.raises(ValueError, match="escapes checkout"):
        tool_for(root).get_file("owner/repo", "subtask-1", "../secret", 1, 2)
