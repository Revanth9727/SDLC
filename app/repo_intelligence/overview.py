"""Lightweight repository shape for ticket-level planning (R-54)."""
from __future__ import annotations

from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from app.db.models import RepositoryFile
from app.repo_intelligence.indexer import _access_scope, _git, _policy, _repository, _select_files
from app.repo_intelligence.store import active_snapshot, snapshot_facts
from app.tools.repo_tool import RepoTool


def build_repo_overview(full_name: str, workspace_id: str, *, repo_tool: RepoTool | None = None) -> dict[str, Any]:
    """Return inventory and structure without parsing, embedding, or graph construction."""
    tool = repo_tool or RepoTool()
    checkout = tool.clone_or_pull(full_name, workspace_id)
    commit = tool.revision(full_name, workspace_id)
    ref = _git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True) or "HEAD"
    scope = _access_scope(full_name)

    snapshot = active_snapshot(full_name, ref, scope)
    if snapshot is not None and snapshot.commit_sha == commit:
        paths = sorted(item.path for item in snapshot_facts(snapshot.id, RepositoryFile))
        excluded = snapshot.excluded_count
        source = "ready_snapshot"
        snapshot_id = str(snapshot.id)
    else:
        repository = _repository(full_name, scope)
        paths, excluded = _select_files(checkout, _policy(repository.id))
        source = "checkout_inventory"
        snapshot_id = None

    return {
        "repo": full_name,
        "ref": ref,
        "commit_sha": commit,
        "source": source,
        "snapshot_id": snapshot_id,
        "file_count": len(paths),
        "excluded_count": excluded,
        "files": paths,
        "directories": _directory_summary(paths),
        "modules": _module_summary(paths),
    }


def build_repo_overviews(repos: list[str], workspace_id: str, *, repo_tool: RepoTool | None = None) -> list[dict[str, Any]]:
    tool = repo_tool or RepoTool()
    return [build_repo_overview(repo, workspace_id, repo_tool=tool) for repo in repos]


def _directory_summary(paths: list[str]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for path in paths:
        parent = str(PurePosixPath(path).parent)
        counts["." if parent == "." else parent] += 1
    return [{"path": path, "file_count": count} for path, count in sorted(counts.items())]


def _module_summary(paths: list[str]) -> list[str]:
    modules: set[str] = set()
    for path in paths:
        parts = PurePosixPath(path).parts
        if len(parts) > 1:
            modules.add(parts[0])
        elif Path(path).suffix:
            modules.add(".")
    return sorted(modules)
