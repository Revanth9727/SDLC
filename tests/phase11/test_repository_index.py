import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.db.connection import SessionLocal, engine
from app.db.models import (
    Repository, RepositoryCodeChunk, RepositoryFile, RepositoryGraphEdge,
    RepositoryIndexJob, RepositoryReference,
    RepositorySnapshot, RepositorySQLAccess, RepositorySymbol,
)
from app.repo_intelligence.indexer import RepositoryIndexer
from app.repo_intelligence.store import active_snapshot, snapshot_facts
from app.tools.repo_tool import RepoTool


class LocalRepo(RepoTool):
    def __init__(self, remote: Path, root: Path):
        super().__init__(workspace_root=root)
        self.remote = remote

    def _public_remote_url(self, _name):
        return self.remote.as_uri()


@pytest.fixture
def indexed_repo(tmp_path: Path):
    for model in (
        RepositoryIndexJob, RepositorySQLAccess, RepositoryGraphEdge,
        RepositoryReference, RepositoryCodeChunk, RepositorySymbol,
        RepositoryFile, RepositorySnapshot, Repository,
    ):
        model.__table__.create(engine, checkfirst=True)
    remote = tmp_path / "remote"
    remote.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=remote, check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (remote / ".gitignore").write_text("ignored/\n")
    (remote / "orders.py").write_text(
        "def load_order(db, order_id):\n"
        "    return db.execute('SELECT * FROM orders WHERE id = ?', order_id)\n\n"
        "def checkout(db, order_id):\n"
        "    return load_order(db, order_id)\n"
    )
    (remote / "unchanged.py").write_text("def stable():\n    return True\n")
    (remote / "requirements.txt").write_text("fastapi==0.115\nprivate @ https://token@example.invalid/pkg\n")
    (remote / "node_modules").mkdir()
    (remote / "node_modules" / "vendor.js").write_text("function vendored() {}\n")
    (remote / "asset.bin").write_bytes(b"\0binary")
    git("add", "-f", ".")
    git("commit", "-m", "initial")
    full_name = f"local/index-{uuid.uuid4().hex}"
    yield LocalRepo(remote, tmp_path / "workspaces"), git, remote, full_name
    with SessionLocal() as db:
        repository = db.scalar(select(Repository).where(Repository.full_name == full_name))
        if repository:
            db.delete(repository)
            db.commit()


def test_cold_then_incremental_snapshot_with_provenance(indexed_repo):
    tool, git, remote, full_name = indexed_repo
    events = []
    indexer = RepositoryIndexer(tool, events.append, embedder=_embedding)
    first = indexer.ensure(full_name, "subtask-a")

    with SessionLocal() as db:
        files = list(db.scalars(select(RepositoryFile).where(RepositoryFile.snapshot_id == first.id)))
        assert {row.path for row in files} == {".gitignore", "orders.py", "requirements.txt", "unchanged.py"}
        assert all(row.commit_sha == first.commit_sha and row.evidence_kind == "PROVEN" for row in files)
        requirements = next(row for row in files if row.path == "requirements.txt")
        assert requirements.artifact_type == "build_config"
        assert requirements.artifact_metadata["dependencies"] == ["fastapi", "private"]
        assert "token@example" not in str(requirements.artifact_metadata)
        assert db.scalar(select(RepositorySymbol).where(
            RepositorySymbol.snapshot_id == first.id,
            RepositorySymbol.name == "checkout",
        ))
        assert db.scalar(select(RepositoryReference).where(
            RepositoryReference.snapshot_id == first.id,
            RepositoryReference.target_symbol == "load_order",
        ))
        assert db.scalar(select(RepositorySQLAccess).where(
            RepositorySQLAccess.snapshot_id == first.id,
            RepositorySQLAccess.relation_name == "orders",
        ))
        assert db.scalar(select(RepositoryGraphEdge).where(
            RepositoryGraphEdge.snapshot_id == first.id,
            RepositoryGraphEdge.relation_kind == "CALLS",
            RepositoryGraphEdge.target_node == "symbol:load_order",
        ))
        assert db.scalar(select(RepositoryGraphEdge).where(
            RepositoryGraphEdge.snapshot_id == first.id,
            RepositoryGraphEdge.relation_kind == "CALLED_BY",
            RepositoryGraphEdge.source_node == "symbol:load_order",
        ))
        assert db.scalar(select(RepositoryGraphEdge).where(
            RepositoryGraphEdge.snapshot_id == first.id,
            RepositoryGraphEdge.relation_kind == "READS",
            RepositoryGraphEdge.target_node == "table:orders",
        ))
        chunks = list(db.scalars(select(RepositoryCodeChunk).where(
            RepositoryCodeChunk.snapshot_id == first.id,
        )))
        assert {chunk.source_file for chunk in chunks} == {
            ".gitignore", "orders.py", "requirements.txt", "unchanged.py",
        }
        assert all(chunk.extractor == "structural_chunker" for chunk in chunks)

    old_sha = first.commit_sha
    (remote / "orders.py").write_text((remote / "orders.py").read_text() + "\ndef cancel():\n    return True\n")
    git("add", "orders.py")
    git("commit", "-m", "change one file")
    second = indexer.ensure(full_name, "subtask-b")

    assert second.id != first.id
    assert second.base_snapshot_id == first.id
    incremental = next(event for event in events if event["stage"] == "incremental_diff")
    assert incremental["changed"] == ["orders.py"]
    assert incremental["reparsed"] == 1
    with SessionLocal() as db:
        old = db.get(RepositorySnapshot, first.id)
        assert old.index_status == "STALE" and not old.is_active
        assert db.get(RepositorySnapshot, second.id).is_active
        unchanged = db.scalar(select(RepositoryFile).where(
            RepositoryFile.snapshot_id == second.id,
            RepositoryFile.path == "unchanged.py",
        ))
        assert unchanged.commit_sha == old_sha
        changed = db.scalar(select(RepositoryFile).where(
            RepositoryFile.snapshot_id == second.id,
            RepositoryFile.path == "orders.py",
        ))
        assert changed.commit_sha == second.commit_sha
    assert active_snapshot(full_name, "main", "public").id == second.id
    assert snapshot_facts(first.id, RepositoryFile) == []
    assert snapshot_facts(second.id, RepositoryFile)


def test_two_tickets_share_one_index_job(indexed_repo, monkeypatch):
    tool, _git_cmd, _remote, full_name = indexed_repo
    original = RepositoryIndexer._extract

    def slow_extract(self, *args):
        time.sleep(.03)
        return original(self, *args)

    monkeypatch.setattr(RepositoryIndexer, "_extract", slow_extract)
    events = [[], []]
    results = [None, None]

    def run(position):
        results[position] = RepositoryIndexer(tool, events[position].append, embedder=_embedding).ensure(
            full_name, f"subtask-{position}"
        )

    threads = [threading.Thread(target=run, args=(position,)) for position in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert results[0].id == results[1].id
    assert sum(any(event["stage"] == "reused" for event in stream) for stream in events) == 1
    with SessionLocal() as db:
        repository = db.scalar(select(Repository).where(Repository.full_name == full_name))
        jobs = list(db.scalars(select(RepositoryIndexJob).where(RepositoryIndexJob.repo_id == repository.id)))
        assert len(jobs) == 1
        assert jobs[0].status == "READY"


def _embedding(text: str) -> list[float]:
    vector = [0.0] * 1536
    lowered = text.lower()
    if "purchase" in lowered or "checkout" in lowered:
        vector[0] = 1.0
    elif "load_order" in lowered:
        vector[3] = 1.0
    elif "stable" in lowered:
        vector[1] = 1.0
    else:
        vector[2] = 1.0
    return vector
