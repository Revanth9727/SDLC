"""Cold and incremental repository indexing without LLM calls or code storage."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.db.connection import SessionLocal
from app.db.models import (
    RepoToken, Repository, RepositoryCodeChunk, RepositoryFile, RepositoryGraphEdge,
    RepositoryIndexJob, RepositoryIndexPolicy,
    RepositoryReference, RepositorySnapshot, RepositorySQLAccess, RepositorySymbol,
)
from app.tools.repo_tool import RepoTool

IndexEventSink = Callable[[dict[str, Any]], None]
INDEX_STATES = {"NEW", "INDEXING", "READY", "PARTIAL", "STALE", "FAILED", "UPDATING"}
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".rb", ".php", ".sql", ".sh"}
LOCK_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "cargo.lock", "gemfile.lock"}
CONFIG_NAMES = {"pom.xml", "package.json", "requirements.txt", "dockerfile", "application.yml", "application.yaml"}
LANGUAGES = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
    ".php": "php", ".sql": "sql", ".sh": "shell", ".yml": "yaml", ".yaml": "yaml",
    ".json": "json", ".xml": "xml", ".toml": "toml",
}
SQL_PATTERNS = (
    ("READ", re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.$]*)", re.I)),
    ("WRITE", re.compile(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([A-Za-z_][\w.$]*)", re.I)),
)
SQL_COLUMN_PATTERNS = (
    ("READ", re.compile(r"\bSELECT\s+(.+?)\s+FROM\s+([A-Za-z_][\w.$]*)", re.I | re.S)),
    ("WRITE", re.compile(r"\bINSERT\s+INTO\s+([A-Za-z_][\w.$]*)\s*\(([^)]+)\)", re.I | re.S)),
    ("WRITE", re.compile(r"\bUPDATE\s+([A-Za-z_][\w.$]*)\s+SET\s+(.+?)(?:\s+WHERE\b|$)", re.I | re.S)),
)


@dataclass
class ExtractedFile:
    file: dict[str, Any]
    chunks: list[dict[str, Any]] = field(default_factory=list)
    symbols: list[dict[str, Any]] = field(default_factory=list)
    references: list[dict[str, Any]] = field(default_factory=list)
    sql: list[dict[str, Any]] = field(default_factory=list)


class RepositoryIndexer:
    def __init__(self, repo_tool: RepoTool | None = None, event_sink: IndexEventSink | None = None,
                 embedder: Callable[[str], list[float]] | None = None) -> None:
        self.repo_tool = repo_tool or RepoTool()
        self.event_sink = event_sink
        self.embedder = embedder

    def ensure(self, full_name: str, subtask_id: str, *, ref: str | None = None,
               access_scope: str | None = None) -> RepositorySnapshot:
        checkout = self.repo_tool.clone_or_pull(full_name, subtask_id)
        commit = self.repo_tool.revision(full_name, subtask_id)
        branch = ref or _git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True) or "HEAD"
        scope = access_scope or _access_scope(full_name)
        repository = _repository(full_name, scope)
        lock_key = f"repo-index:{repository.id}:{branch}"
        self._emit("waiting", repo=full_name, ref=branch, commit=commit)
        with SessionLocal() as lock_db:
            lock_db.execute(text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"), {"key": lock_key})
            try:
                return self._under_lock(repository.id, full_name, branch, commit, checkout)
            finally:
                lock_db.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), {"key": lock_key})
                lock_db.commit()

    def _under_lock(self, repo_id: uuid.UUID, full_name: str, ref: str, commit: str,
                    checkout: Path) -> RepositorySnapshot:
        with SessionLocal() as db:
            ready = db.scalar(select(RepositorySnapshot).where(
                RepositorySnapshot.repo_id == repo_id, RepositorySnapshot.ref == ref,
                RepositorySnapshot.commit_sha == commit, RepositorySnapshot.index_status == "READY",
                RepositorySnapshot.is_active.is_(True)))
            if ready:
                db.expunge(ready)
                self._emit("reused", repo=full_name, ref=ref, commit=commit, snapshot_id=str(ready.id))
                return ready
            interrupted = list(db.scalars(select(RepositorySnapshot).where(
                RepositorySnapshot.repo_id == repo_id, RepositorySnapshot.ref == ref,
                RepositorySnapshot.index_status.in_(["INDEXING", "UPDATING"]))))
            for item in interrupted:
                item.index_status, item.is_active, item.error = "PARTIAL", False, "Previous index job did not complete"
            active = db.scalar(select(RepositorySnapshot).where(
                RepositorySnapshot.repo_id == repo_id, RepositorySnapshot.ref == ref,
                RepositorySnapshot.is_active.is_(True), RepositorySnapshot.index_status == "READY"))
            snapshot = db.scalar(select(RepositorySnapshot).where(
                RepositorySnapshot.repo_id == repo_id, RepositorySnapshot.ref == ref,
                RepositorySnapshot.commit_sha == commit))
            status = "UPDATING" if active else "INDEXING"
            if snapshot is None:
                snapshot = RepositorySnapshot(repo_id=repo_id, ref=ref, commit_sha=commit,
                    index_status=status, is_active=False, base_snapshot_id=active.id if active else None)
                db.add(snapshot)
            else:
                snapshot.index_status, snapshot.error = status, None
                snapshot.base_snapshot_id = active.id if active else None
            job = RepositoryIndexJob(repo_id=repo_id, ref=ref, target_sha=commit, status=status)
            db.add(job)
            db.commit()
            db.refresh(snapshot)
            db.refresh(job)
            active_id = active.id if active else None
            snapshot_id, job_id = snapshot.id, job.id

        self._emit("started", repo=full_name, ref=ref, commit=commit,
                   mode="incremental" if active_id else "cold", snapshot_id=str(snapshot_id))
        try:
            policy = _policy(repo_id)
            if active_id:
                old_commit = _snapshot_commit(active_id)
                _ensure_commit_history(self.repo_tool, checkout, full_name, old_commit)
                changed, deleted = _changed_paths(checkout, old_commit, commit)
                selected, excluded = _select_files(checkout, policy, only=changed)
                extracted = [self._extract(checkout, path, commit) for path in selected]
                self._embed_all(extracted, snapshot_id)
                self._emit("incremental_diff", changed=sorted(changed), deleted=sorted(deleted),
                           reparsed=len(extracted), snapshot_id=str(snapshot_id))
            else:
                selected, excluded = _select_files(checkout, policy)
                extracted = []
                for number, path in enumerate(selected, 1):
                    extracted.append(self._extract(checkout, path, commit))
                    if number == len(selected) or number % 100 == 0:
                        self._emit("progress", indexed=number, total=len(selected), snapshot_id=str(snapshot_id))
                self._embed_all(extracted, snapshot_id)
                changed, deleted = set(selected), set()
            self._swap(repo_id, ref, snapshot_id, job_id, active_id, commit, extracted,
                       changed | deleted, excluded)
        except Exception as exc:
            self._fail(snapshot_id, job_id, str(exc))
            self._emit("failed", repo=full_name, ref=ref, commit=commit, error=str(exc), snapshot_id=str(snapshot_id))
            raise
        with SessionLocal() as db:
            result = db.get(RepositorySnapshot, snapshot_id)
            db.expunge(result)
        self._emit("ready", repo=full_name, ref=ref, commit=commit,
                   files=result.file_count, excluded=result.excluded_count, snapshot_id=str(result.id))
        return result

    def _extract(self, checkout: Path, path: str, commit: str) -> ExtractedFile:
        target = (checkout / path).resolve()
        data = target.read_bytes()
        source = data.decode("utf-8", errors="replace")
        lines = source.splitlines() or [""]
        provenance = _provenance(path, 1, len(lines), "inventory", commit)
        artifact_type, metadata = _artifact(path, source)
        result = ExtractedFile(file={**provenance, "path": path, "language": _language(path),
            "file_hash": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
            "artifact_type": artifact_type, "artifact_metadata": metadata})
        if Path(path).suffix.lower() == ".py":
            _extract_python(source, path, commit, result)
        else:
            _extract_generic(source, path, commit, result)
        _extract_sql(source, path, commit, result)
        result.chunks = _structural_chunks(source, path, commit)
        return result

    def _embed_all(self, extracted: Sequence[ExtractedFile], snapshot_id: uuid.UUID) -> None:
        chunks = [chunk for item in extracted for chunk in item.chunks]
        if not chunks:
            return
        embed = self.embedder
        if embed is None:
            from app.agents.llm import LLMClient
            embed = LLMClient().embed
        for number, chunk in enumerate(chunks, 1):
            text_to_embed = chunk.pop("_text")
            chunk["embedding"] = embed(text_to_embed)
            if number == len(chunks) or number % 100 == 0:
                self._emit("embedding_progress", embedded=number, total=len(chunks),
                           snapshot_id=str(snapshot_id))

    def _swap(self, repo_id: uuid.UUID, ref: str, snapshot_id: uuid.UUID, job_id: uuid.UUID,
              active_id: uuid.UUID | None, commit: str, extracted: list[ExtractedFile],
              changed: set[str], excluded: int) -> None:
        with SessionLocal() as db:
            snapshot = db.get(RepositorySnapshot, snapshot_id)
            if active_id:
                self._copy_unchanged(db, active_id, snapshot_id, changed)
            for item in extracted:
                db.add(RepositoryFile(snapshot_id=snapshot_id, **item.file))
                db.add_all(RepositoryCodeChunk(snapshot_id=snapshot_id, **chunk) for chunk in item.chunks)
                db.add_all(RepositorySymbol(snapshot_id=snapshot_id, **fact) for fact in item.symbols)
                db.add_all(RepositoryReference(snapshot_id=snapshot_id, **fact) for fact in item.references)
                db.add_all(RepositorySQLAccess(snapshot_id=snapshot_id, **fact) for fact in item.sql)
                db.add_all(RepositoryGraphEdge(snapshot_id=snapshot_id, **edge)
                           for edge in _graph_edges(item))
            db.flush()
            count = db.scalar(select(text("count(*)")).select_from(RepositoryFile).where(
                RepositoryFile.snapshot_id == snapshot_id)) or 0
            if count == 0:
                raise ValueError("index validation failed: no eligible files")
            previous = db.scalar(select(RepositorySnapshot).where(
                RepositorySnapshot.repo_id == repo_id, RepositorySnapshot.ref == ref,
                RepositorySnapshot.is_active.is_(True)).with_for_update())
            if previous and previous.id != snapshot_id:
                previous.is_active, previous.index_status = False, "STALE"
                db.flush()
            snapshot.file_count, snapshot.excluded_count = count, excluded
            snapshot.index_status, snapshot.indexed_at, snapshot.is_active = "READY", datetime.now(timezone.utc), True
            job = db.get(RepositoryIndexJob, job_id)
            job.status, job.snapshot_id, job.finished_at = "READY", snapshot_id, datetime.now(timezone.utc)
            db.commit()

    @staticmethod
    def _copy_unchanged(db, old_id: uuid.UUID, new_id: uuid.UUID, changed: set[str]) -> None:
        for model in (
            RepositoryFile, RepositoryCodeChunk, RepositorySymbol, RepositoryGraphEdge,
            RepositoryReference, RepositorySQLAccess,
        ):
            for row in db.scalars(select(model).where(model.snapshot_id == old_id)):
                if row.source_file in changed:
                    continue
                values = {column.name: getattr(row, column.name) for column in model.__table__.columns
                          if column.name not in {"id", "snapshot_id"}}
                db.add(model(snapshot_id=new_id, **values))

    @staticmethod
    def _fail(snapshot_id: uuid.UUID, job_id: uuid.UUID, error: str) -> None:
        with SessionLocal() as db:
            snapshot, job = db.get(RepositorySnapshot, snapshot_id), db.get(RepositoryIndexJob, job_id)
            if snapshot:
                snapshot.index_status, snapshot.is_active, snapshot.error = "FAILED", False, error[:4000]
            if job:
                job.status, job.error, job.finished_at = "FAILED", error[:4000], datetime.now(timezone.utc)
            db.commit()

    def _emit(self, stage: str, **payload: Any) -> None:
        if self.event_sink:
            self.event_sink({"stage": stage, **payload})


def ensure_repository_index(full_name: str, subtask_id: str, **kwargs) -> RepositorySnapshot:
    return RepositoryIndexer().ensure(full_name, subtask_id, **kwargs)


def _repository(full_name: str, access_scope: str) -> Repository:
    with SessionLocal() as db:
        db.execute(insert(Repository).values(
            full_name=full_name,
            access_scope=access_scope,
        ).on_conflict_do_nothing(constraint="uq_repository_scope"))
        db.commit()
        row = db.scalar(select(Repository).where(
            Repository.full_name == full_name,
            Repository.access_scope == access_scope,
        ))
        db.expunge(row)
        return row


def _access_scope(full_name: str) -> str:
    with SessionLocal() as db:
        private = db.get(RepoToken, full_name) is not None
    digest = hashlib.sha256(full_name.encode()).hexdigest()[:24]
    return f"private:{digest}" if private else "public"


def _policy(repo_id: uuid.UUID) -> dict[str, Any]:
    with SessionLocal() as db:
        row = db.get(RepositoryIndexPolicy, repo_id)
        overrides = dict(row.overrides or {}) if row else {}
    excluded = set(settings.repo_index_excluded_dirs)
    excluded.update(map(str, overrides.get("excluded_dirs", [])))
    excluded.difference_update(map(str, overrides.get("included_dirs", [])))
    return {
        "excluded_dirs": excluded,
        "max_file_bytes": int(overrides.get("max_file_bytes", settings.repo_index_max_file_bytes)),
        "include_paths": set(map(str, overrides.get("include_paths", []))),
        "exclude_paths": set(map(str, overrides.get("exclude_paths", []))),
    }


def _snapshot_commit(snapshot_id: uuid.UUID) -> str:
    with SessionLocal() as db:
        return db.get(RepositorySnapshot, snapshot_id).commit_sha


def _changed_paths(checkout: Path, old: str, new: str) -> tuple[set[str], set[str]]:
    output = _git(checkout, "diff", "--name-status", "--find-renames", old, new, "--")
    changed: set[str] = set()
    deleted: set[str] = set()
    for line in output.splitlines():
        fields = line.split("\t")
        status = fields[0]
        if status.startswith("R") and len(fields) == 3:
            deleted.add(fields[1])
            changed.add(fields[2])
        elif len(fields) >= 2 and status == "D":
            deleted.add(fields[1])
        elif len(fields) >= 2:
            changed.add(fields[1])
    return changed, deleted


def _ensure_commit_history(repo_tool: RepoTool, checkout: Path, full_name: str, commit: str) -> None:
    if not _has_commit(checkout, commit):
        try:
            repo_tool._git(["fetch", "--deepen", "100", "origin"], cwd=checkout)
        except RuntimeError as public_error:
            token = repo_tool._stored_token(full_name)
            if not token:
                raise RuntimeError(
                    f"incremental index cannot reach prior commit {commit}; history deepen failed"
                ) from public_error
            repo_tool._git(
                ["-c", repo_tool._auth_header(token), "fetch", "--deepen", "100", "origin"],
                cwd=checkout,
            )
    if not _has_commit(checkout, commit):
        raise RuntimeError(
            f"incremental index requires prior commit {commit}, but it is outside fetched history"
        )


def _has_commit(checkout: Path, commit: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=checkout,
        capture_output=True,
        check=False,
        timeout=30,
    )
    return result.returncode == 0


def _select_files(checkout: Path, policy: dict[str, Any],
                  only: set[str] | None = None) -> tuple[list[str], int]:
    tracked = _git(checkout, "ls-files", "-co", "--exclude-standard", "-z").split("\0")
    ignored = _gitignored(checkout, tracked)
    selected: list[str] = []
    excluded = 0
    for path in sorted(item for item in tracked if item and (only is None or item in only)):
        forced = path in policy["include_paths"]
        if (path in ignored and not forced) or path in policy["exclude_paths"] or (
            set(PurePosixPath(path).parts) & policy["excluded_dirs"] and not forced
        ):
            excluded += 1
            continue
        try:
            data = (checkout / path).read_bytes()
        except OSError:
            excluded += 1
            continue
        if not forced and (
            len(data) > policy["max_file_bytes"]
            or b"\0" in data[:8192]
            or _is_lock(path)
            or _is_minified(path, data)
        ):
            excluded += 1
            continue
        selected.append(path)
    return selected, excluded


def _gitignored(checkout: Path, paths: list[str]) -> set[str]:
    candidates = [path for path in paths if path]
    if not candidates:
        return set()
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-z", "--stdin"],
        cwd=checkout,
        input="\0".join(candidates) + "\0",
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(f"git check-ignore failed: {result.stderr.strip()}")
    return {path for path in result.stdout.split("\0") if path}


def _is_lock(path: str) -> bool:
    return PurePosixPath(path).name.lower() in LOCK_NAMES


def _is_minified(path: str, data: bytes) -> bool:
    name = PurePosixPath(path).name.lower()
    lines = data.splitlines() or [data]
    return ".min." in name or (
        Path(path).suffix.lower() in {".js", ".css"}
        and len(data) > 20_000
        and len(data) / len(lines) > 500
    )


def _language(path: str) -> str:
    if PurePosixPath(path).name.lower() == "dockerfile":
        return "dockerfile"
    return LANGUAGES.get(Path(path).suffix.lower(), "unknown")


def _artifact(path: str, source: str) -> tuple[str, dict[str, Any]]:
    name = PurePosixPath(path).name.lower()
    is_ci = path.startswith(".github/workflows/") and Path(path).suffix.lower() in {".yml", ".yaml"}
    if name not in CONFIG_NAMES and not is_ci:
        kind = "source" if Path(path).suffix.lower() in SOURCE_SUFFIXES else "resource"
        return kind, {}
    metadata: dict[str, Any] = {"kind": "ci" if is_ci else "build_config"}
    if name == "package.json":
        try:
            payload = json.loads(source)
            dependencies = {**payload.get("dependencies", {}), **payload.get("devDependencies", {})}
            metadata.update({"dependencies": sorted(dependencies), "scripts": sorted(payload.get("scripts", {}))})
        except (ValueError, TypeError):
            metadata["parse_error"] = "invalid_json"
    elif name == "requirements.txt":
        metadata["dependencies"] = [
            match.group(1) for line in source.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
            for match in [re.match(r"\s*([A-Za-z0-9_.-]+)", line)]
            if match
        ][:500]
    elif name == "pom.xml":
        metadata["dependencies"] = re.findall(r"<artifactId>\s*([^<]+)\s*</artifactId>", source)[:500]
        metadata["versions"] = re.findall(r"<version>\s*([^<]+)\s*</version>", source)[:200]
    elif name == "dockerfile":
        metadata["commands"] = [
            _command_shape(line) for line in source.splitlines()
            if re.match(r"^\s*(FROM|RUN|CMD|ENTRYPOINT|COPY)\b", line, re.I)
        ][:200]
    else:
        metadata["commands"] = [
            _command_shape(line) for line in source.splitlines()
            if re.match(r"^\s*(run|script|command|image|uses)\s*:", line, re.I)
        ][:200]
        metadata["keys"] = [
            match.group(1) for match in
            (re.match(r"^\s*([A-Za-z_][\w.-]*)\s*:", line) for line in source.splitlines())
            if match
        ][:200]
    return "build_config", metadata


def _provenance(path: str, start: int, end: int, extractor: str, commit: str,
                confidence: float = 1.0, evidence_kind: str = "PROVEN") -> dict[str, Any]:
    return {
        "source_file": path,
        "start_line": max(1, start),
        "end_line": max(start, end),
        "extractor": extractor,
        "commit_sha": commit,
        "confidence": confidence,
        "evidence_kind": evidence_kind,
    }


def _structural_chunks(source: str, path: str, commit: str) -> list[dict[str, Any]]:
    """Partition every non-empty source line into bounded, structurally named spans."""
    lines = source.splitlines()
    if not lines:
        return []
    spans: list[tuple[int, int, str]] = []
    if Path(path).suffix.lower() == ".py":
        try:
            tree = ast.parse(source)
        except SyntaxError:
            pass
        else:
            nodes = sorted(
                (node for node in tree.body if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                )),
                key=lambda node: node.lineno,
            )
            cursor = 1
            for node in nodes:
                start, end = node.lineno, getattr(node, "end_lineno", node.lineno)
                if cursor < start:
                    spans.extend(_window_spans(cursor, start - 1, "module"))
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                spans.extend(_window_spans(start, end, f"{kind} {node.name}"))
                cursor = end + 1
            if cursor <= len(lines):
                spans.extend(_window_spans(cursor, len(lines), "module"))
    if not spans:
        spans = _window_spans(1, len(lines), "module")

    chunks: list[dict[str, Any]] = []
    for start, end, label in spans:
        body = "\n".join(lines[start - 1:end]).strip()
        if not body:
            continue
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        key = hashlib.sha256(f"{path}:{start}:{end}:{digest}".encode()).hexdigest()
        chunks.append({
            **_provenance(path, start, end, "structural_chunker", commit),
            "chunk_key": key,
            "label": label,
            "language": _language(path),
            "content_hash": digest,
            # Embedded transiently, then discarded before persistence. Search results
            # are always verified by reading these lines from the authorized checkout.
            "_text": f"file: {path}\nsection: {label}\n{body[:12000]}",
        })
    return chunks


def _window_spans(start: int, end: int, label: str, size: int = 120) -> list[tuple[int, int, str]]:
    spans = []
    cursor = start
    while cursor <= end:
        final = min(end, cursor + size - 1)
        spans.append((cursor, final, label))
        cursor = final + 1
    return spans


def _extract_python(source: str, path: str, commit: str, result: ExtractedFile) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        _extract_generic(source, path, commit, result)
        return
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            signature = None if kind == "class" else _signature(node)
            result.symbols.append({
                **_provenance(path, node.lineno, getattr(node, "end_lineno", node.lineno),
                              "python_ast", commit),
                "name": node.name,
                "qualified_name": _qualified(node, parents),
                "kind": kind,
                "signature": signature,
            })
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                result.references.append({
                    **_provenance(path, node.lineno, getattr(node, "end_lineno", node.lineno),
                                  "python_ast", commit),
                    "source_symbol": _enclosing(node, parents),
                    "target_symbol": alias.name,
                    "relation_kind": "IMPORTS",
                })
        elif isinstance(node, ast.Call):
            target = _call_name(node.func)
            if target:
                result.references.append({
                    **_provenance(path, node.lineno, getattr(node, "end_lineno", node.lineno),
                                  "python_ast", commit),
                    "source_symbol": _enclosing(node, parents),
                    "target_symbol": target,
                    "relation_kind": "CALLS",
                })
        elif isinstance(node, ast.Name) and not isinstance(parents.get(node), ast.Call):
            result.references.append({
                **_provenance(path, node.lineno, node.lineno, "python_ast", commit),
                "source_symbol": _enclosing(node, parents),
                "target_symbol": node.id,
                "relation_kind": "REFERENCES",
            })


def _extract_generic(source: str, path: str, commit: str, result: ExtractedFile) -> None:
    pattern = re.compile(
        r"^\s*(?:(?:export|public|private|protected|static|async)\s+)*"
        r"(class|def|function|func|fn)\s+([A-Za-z_$][\w$]*)",
        re.M,
    )
    for match in pattern.finditer(source):
        line = source.count("\n", 0, match.start()) + 1
        result.symbols.append({
            **_provenance(path, line, line, "lexical_symbols", commit, .8),
            "name": match.group(2),
            "qualified_name": match.group(2),
            "kind": "class" if match.group(1) == "class" else "function",
            "signature": None,
        })
    for line_number, line in enumerate(source.splitlines(), 1):
        for token in dict.fromkeys(re.findall(r"\b[A-Za-z_$][A-Za-z0-9_$]{2,}\b", line)):
            result.references.append({
                **_provenance(path, line_number, line_number, "lexical_references", commit, .7),
                "source_symbol": None,
                "target_symbol": token,
                "relation_kind": "REFERENCES",
            })
            if len(result.references) >= 2000:
                return


def _extract_sql(source: str, path: str, commit: str, result: ExtractedFile) -> None:
    for operation, pattern in SQL_PATTERNS:
        for match in pattern.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            result.sql.append({
                **_provenance(path, line, line, "sql_lexical", commit, .9),
                "operation": operation,
                "relation_name": match.group(1),
                "column_name": None,
                "enclosing_symbol": _symbol_at_line(result.symbols, line),
            })
    for operation, pattern in SQL_COLUMN_PATTERNS:
        for match in pattern.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            if operation == "READ":
                raw_columns, relation = match.group(1), match.group(2)
            else:
                relation, raw_columns = match.group(1), match.group(2)
            for column in _sql_columns(raw_columns):
                result.sql.append({
                    **_provenance(path, line, line, "sql_column_lexical", commit, .8),
                    "operation": operation,
                    "relation_name": relation,
                    "column_name": column,
                    "enclosing_symbol": _symbol_at_line(result.symbols, line),
                })


def _symbol_at_line(symbols: list[dict[str, Any]], line: int) -> str | None:
    containing = [fact for fact in symbols if fact["start_line"] <= line <= fact["end_line"]]
    if not containing:
        return None
    return min(containing, key=lambda fact: fact["end_line"] - fact["start_line"])["qualified_name"]


def _sql_columns(value: str) -> list[str]:
    columns = []
    for item in value.split(","):
        token = item.strip().split()[0].strip('"`[]') if item.strip() else ""
        token = token.split(".")[-1]
        if re.fullmatch(r"[A-Za-z_][\w$]*", token) and token != "*":
            columns.append(token)
    return list(dict.fromkeys(columns))[:200]


def _graph_edges(item: ExtractedFile) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    path = item.file["path"]
    for symbol in item.symbols:
        edges.append({
            **_fact_provenance(symbol),
            "source_node": f"file:{path}",
            "target_node": f"symbol:{symbol['qualified_name']}",
            "relation_kind": "CONTAINS",
        })
    for reference in item.references:
        source = f"symbol:{reference['source_symbol']}" if reference["source_symbol"] else f"file:{path}"
        target = f"symbol:{reference['target_symbol']}"
        relation = reference["relation_kind"]
        edges.append({**_fact_provenance(reference), "source_node": source,
                      "target_node": target, "relation_kind": relation})
        if relation == "CALLS":
            edges.append({**_fact_provenance(reference), "source_node": target,
                          "target_node": source, "relation_kind": "CALLED_BY"})
    for access in item.sql:
        source = f"symbol:{access['enclosing_symbol']}" if access["enclosing_symbol"] else f"file:{path}"
        table = f"table:{access['relation_name']}"
        edges.append({**_fact_provenance(access), "source_node": source,
                      "target_node": table, "relation_kind": f"{access['operation']}S"})
        if access.get("column_name"):
            edges.append({**_fact_provenance(access), "source_node": source,
                          "target_node": f"column:{access['relation_name']}.{access['column_name']}",
                          "relation_kind": f"{access['operation']}S"})
    return edges


def _fact_provenance(fact: dict[str, Any]) -> dict[str, Any]:
    return {key: fact[key] for key in (
        "source_file", "start_line", "end_line", "extractor",
        "commit_sha", "confidence", "evidence_kind",
    )}


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    names = [arg.arg for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]]
    if node.args.vararg:
        names.append("*" + node.args.vararg.arg)
    if node.args.kwarg:
        names.append("**" + node.args.kwarg.arg)
    return f"{node.name}({', '.join(names)})"


def _qualified(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names = [getattr(node, "name", "")]
    current = parents.get(node)
    while current:
        if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(names))


def _enclosing(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    current = parents.get(node)
    while current:
        if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            return _qualified(current, parents)
        current = parents.get(current)
    return None


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _command_shape(line: str) -> str:
    """Retain command structure without argument values that may contain secrets."""
    key, separator, value = line.strip().partition(":")
    if separator:
        executable = value.strip().split()[0] if value.strip() else ""
        return f"{key}: {executable}".rstrip()
    return " ".join(line.strip().split()[:2])


def _git(checkout: Path, *args: str, allow_failure: bool = False) -> str:
    result = subprocess.run(
        ["git", *args], cwd=checkout, capture_output=True, text=True,
        check=False, timeout=120,
    )
    if result.returncode and not allow_failure:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.rstrip("\n") if result.returncode == 0 else ""
