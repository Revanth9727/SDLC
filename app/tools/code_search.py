"""Deterministic lexical code navigation over an isolated repository checkout."""
from __future__ import annotations

import ast
import hashlib
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.db.connection import SessionLocal
from app.db.models import (
    RepoToken, Repository, RepositoryCodeChunk, RepositoryGraphEdge,
    RepositorySnapshot, RepositorySymbol,
)
from app.repo_intelligence.indexer import RepositoryIndexer
from app.tools.repo_tool import RepoTool


ToolEventSink = Callable[[dict[str, Any]], None]
_SYMBOL = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_DEFINITION_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+{symbol}\b"),
    re.compile(r"^\s*class\s+{symbol}\b"),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+{symbol}\b"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+{symbol}\b"),
)


class CodeSearchTool:
    """Fast, bounded search helpers. No method makes an LLM or network call."""

    def __init__(self, repo_tool: RepoTool | None = None, event_sink: ToolEventSink | None = None,
                 embedder: Callable[[str], list[float]] | None = None) -> None:
        self.repo_tool = repo_tool or RepoTool()
        self.event_sink = event_sink
        self.embedder = embedder

    def search_exact(self, full_name: str, subtask_id: str, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        checkout = self._checkout(full_name, subtask_id)
        clean = query.strip()
        if not clean:
            raise ValueError("search query cannot be empty")
        cap = _bounded_limit(limit)
        command = [
            "rg", "--line-number", "--column", "--no-heading", "--color", "never",
            "--fixed-strings", "--glob", "!.git/**", "--glob", "!.env", "--glob", "!.env.*",
            "--", clean, ".",
        ]
        try:
            result = subprocess.run(command, cwd=checkout, capture_output=True, text=True, check=False, timeout=30)
        except FileNotFoundError:
            result = subprocess.run(
                ["git", "grep", "-n", "--column", "-F", "--", clean, ".",
                 ":(exclude).env", ":(exclude).env.*"], cwd=checkout,
                capture_output=True, text=True, check=False, timeout=30,
            )
        if result.returncode not in {0, 1}:
            raise RuntimeError(f"lexical search failed: {result.stderr.strip()}")
        matches = _parse_matches(result.stdout, cap)
        self._emit("search_exact", {"query": clean, "limit": cap}, {"matches": matches})
        return matches

    def find_symbol(self, full_name: str, subtask_id: str, symbol: str, *, limit: int = 20) -> list[dict[str, Any]]:
        clean = _validate_symbol(symbol)
        candidates = self.search_exact(full_name, subtask_id, clean, limit=max(limit * 5, 50))
        definitions = [match for match in candidates if _is_definition(match["text"], clean)][:_bounded_limit(limit)]
        self._emit("find_symbol", {"symbol": clean, "limit": limit}, {"definitions": definitions})
        return definitions

    def search_semantic(self, full_name: str, subtask_id: str, query: str, *,
                        ref: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Retrieve meaning-similar spans from the active, access-scoped snapshot."""
        clean = query.strip()
        if not clean:
            raise ValueError("semantic search query cannot be empty")
        cap = _bounded_limit(limit)
        snapshot = RepositoryIndexer(
            self.repo_tool, event_sink=self._index_event, embedder=self.embedder
        ).ensure(full_name, subtask_id, ref=ref)
        vector = self._embed(clean)
        distance = RepositoryCodeChunk.embedding.cosine_distance(vector)
        symbol_terms = [term for term in _query_terms(clean) if _SYMBOL.fullmatch(term)][:8]
        symbol_match = case(
            (or_(*(func.strpos(func.lower(RepositoryCodeChunk.label), term.lower()) > 0
                   for term in symbol_terms)), 0),
            else_=1,
        ) if symbol_terms else literal(1)
        with SessionLocal() as db:
            rows = db.execute(
                select(RepositoryCodeChunk, distance.label("distance"))
                .where(RepositoryCodeChunk.snapshot_id == snapshot.id)
                .order_by(
                    distance,
                    symbol_match,
                    RepositoryCodeChunk.source_file,
                    RepositoryCodeChunk.start_line,
                    RepositoryCodeChunk.end_line,
                    RepositoryCodeChunk.id,
                )
                .limit(cap)
            ).all()
        checkout = self._checkout(full_name, subtask_id)
        matches = []
        for chunk, raw_distance in rows:
            target = self._safe_code_path(checkout, chunk.source_file)
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            content = "\n".join(lines[chunk.start_line - 1:chunk.end_line])
            # A changed checkout is never represented as indexed evidence.
            if _content_hash(content) != chunk.content_hash:
                continue
            matches.append({
                "path": chunk.source_file,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "line": chunk.start_line,
                "label": chunk.label,
                "language": chunk.language,
                "similarity": round(max(0.0, 1.0 - float(raw_distance)), 6),
                "text": content[:1000],
                "snapshot_id": str(snapshot.id),
                "commit_sha": chunk.commit_sha,
                "chunk_id": str(chunk.id),
                "provenance": {
                    "extractor": chunk.extractor,
                    "confidence": chunk.confidence,
                    "evidence_kind": chunk.evidence_kind,
                },
            })
        self._emit("search_semantic", {"query": clean, "limit": cap}, {"matches": matches})
        return matches

    def search_hybrid(self, full_name: str, subtask_id: str, query: str, *,
                      exact_queries: list[str] | None = None, ref: str | None = None,
                      limit: int = 20) -> list[dict[str, Any]]:
        """Fuse lexical and semantic candidates with a deterministic weighted score."""
        cap = _bounded_limit(limit)
        terms = exact_queries if exact_queries is not None else _query_terms(query)
        lexical: list[dict[str, Any]] = []
        for term in terms[:8]:
            lexical.extend(self.search_exact(full_name, subtask_id, term, limit=cap))
        semantic = self.search_semantic(full_name, subtask_id, query, ref=ref, limit=max(cap, 30))
        candidates: dict[tuple[str, int, int], dict[str, Any]] = {}

        for rank, hit in enumerate(semantic, 1):
            key = (hit["path"], hit["start_line"], hit["end_line"])
            candidates[key] = {
                **hit, "semantic_score": hit["similarity"],
                "lexical_score": 0.0, "sources": ["semantic"],
                "semantic_rank": rank,
            }
        for rank, hit in enumerate(lexical, 1):
            key = next((candidate_key for candidate_key in candidates
                        if candidate_key[0] == hit["path"]
                        and candidate_key[1] <= hit["line"] <= candidate_key[2]),
                       (hit["path"], hit["line"], hit["line"]))
            candidate = candidates.setdefault(key, {
                **hit, "start_line": hit["line"], "end_line": hit["line"],
                "label": "lexical match", "language": _language_for_path(hit["path"]),
                "semantic_score": 0.0, "sources": [],
            })
            candidate["lexical_score"] = max(candidate.get("lexical_score", 0.0), 1.0 / rank)
            if "lexical" not in candidate["sources"]:
                candidate["sources"].append("lexical")

        symbol_paths = self._symbol_paths(
            semantic[0].get("snapshot_id") if semantic else None, _query_terms(query)
        )
        query_tokens = set(_query_terms(query))
        for candidate in candidates.values():
            searchable = f"{candidate['path']} {candidate.get('label', '')} {candidate.get('text', '')}".lower()
            token_overlap = len(query_tokens & set(_query_terms(searchable))) / max(1, len(query_tokens))
            symbol_score = 1.0 if candidate["path"] in symbol_paths else 0.0
            file_score = _file_type_score(candidate["path"])
            candidate["score_components"] = {
                "semantic": round(candidate.get("semantic_score", 0.0), 6),
                "lexical": round(candidate.get("lexical_score", 0.0), 6),
                "token_overlap": round(token_overlap, 6),
                "symbol_match": symbol_score,
                "file_type": file_score,
            }
            candidate["score"] = round(
                .55 * candidate.get("semantic_score", 0.0)
                + .20 * candidate.get("lexical_score", 0.0)
                + .10 * token_overlap + .10 * symbol_score + .05 * file_score,
                6,
            )
        ranked = sorted(candidates.values(), key=lambda item: (
            -item["score"],
            -item["score_components"]["symbol_match"],
            item["path"],
            item["start_line"],
            item["end_line"],
            item.get("chunk_id", ""),
        ))[:cap]
        self._emit("search_hybrid", {"query": query, "terms": terms[:8], "limit": cap},
                   {"matches": ranked, "reranker": "weighted_v1", "llm_calls": 0})
        return ranked

    def get_references(self, full_name: str, subtask_id: str, symbol: str, *, limit: int = 50) -> list[dict[str, Any]]:
        clean = _validate_symbol(symbol)
        graph = self._graph_lookup(
            full_name, subtask_id, clean, {"REFERENCES", "IMPORTS", "CALLS"},
            direction="target", limit=limit,
        )
        if graph:
            self._emit("get_references_graph", {"symbol": clean, "limit": limit},
                       {"references": graph, "verified": len(graph)})
            return graph
        matches = self.search_exact(full_name, subtask_id, clean, limit=max(limit * 2, 50))
        references = [match for match in matches if not _is_definition(match["text"], clean)][:_bounded_limit(limit)]
        self._emit("get_references", {"symbol": clean, "limit": limit}, {"references": references})
        return references

    def get_callers(self, full_name: str, subtask_id: str, symbol: str, *, limit: int = 50) -> list[dict[str, Any]]:
        clean = _validate_symbol(symbol)
        graph = self._graph_lookup(
            full_name, subtask_id, clean, {"CALLED_BY"}, direction="source", limit=limit,
        )
        if graph:
            self._emit("get_callers_graph", {"symbol": clean, "limit": limit},
                       {"callers": graph, "verified": len(graph)})
            return graph
        references = self.search_exact(full_name, subtask_id, f"{clean}(", limit=max(limit * 2, 50))
        callers = [match for match in references if not _is_definition(match["text"], clean)][:_bounded_limit(limit)]
        self._emit("get_callers", {"symbol": clean, "limit": limit}, {"callers": callers})
        return callers

    def get_callees(self, full_name: str, subtask_id: str, symbol: str, *, limit: int = 50) -> list[dict[str, Any]]:
        clean = _validate_symbol(symbol)
        graph = self._graph_lookup(
            full_name, subtask_id, clean, {"CALLS"}, direction="source", limit=limit,
        )
        if graph:
            self._emit("get_callees_graph", {"symbol": clean, "limit": limit},
                       {"callees": graph, "verified": len(graph)})
            return graph
        checkout = self._checkout(full_name, subtask_id)
        definitions = self.find_symbol(full_name, subtask_id, clean, limit=10)
        calls: list[dict[str, Any]] = []
        for definition in definitions:
            path = self._safe_code_path(checkout, definition["path"])
            if path.suffix != ".py":
                continue
            calls.extend(_python_callees(path, definition["path"], clean))
        calls = calls[:_bounded_limit(limit)]
        self._emit("get_callees", {"symbol": clean, "limit": limit}, {"callees": calls})
        return calls

    def get_reads_writes(self, full_name: str, subtask_id: str, name: str, *,
                         limit: int = 50) -> list[dict[str, Any]]:
        """Trace proven SQL table/column reads and writes in either direction."""
        clean = name.strip()
        if not clean or not re.fullmatch(r"[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?", clean):
            raise ValueError(f"invalid symbol, table, or column name: {name!r}")
        snapshot_id = self._graph_snapshot_id(full_name, subtask_id)
        if snapshot_id is None:
            self._emit("get_reads_writes_graph", {"name": clean, "limit": limit},
                       {"edges": [], "verified": 0, "index_available": False})
            return []
        patterns = [f"table:{clean}", f"column:{clean}", f"symbol:{clean}"]
        with SessionLocal() as db:
            rows = list(db.scalars(
                select(RepositoryGraphEdge).where(
                    RepositoryGraphEdge.snapshot_id == snapshot_id,
                    RepositoryGraphEdge.relation_kind.in_({"READS", "WRITES"}),
                    or_(
                        RepositoryGraphEdge.source_node.in_(patterns),
                        RepositoryGraphEdge.target_node.in_(patterns),
                        RepositoryGraphEdge.source_node.endswith(f".{clean}"),
                        RepositoryGraphEdge.target_node.endswith(f".{clean}"),
                    ),
                ).order_by(
                    RepositoryGraphEdge.source_file,
                    RepositoryGraphEdge.start_line,
                    RepositoryGraphEdge.relation_kind,
                ).limit(_bounded_limit(limit))
            ))
        verified = self._verify_edges(full_name, subtask_id, rows)
        self._emit("get_reads_writes_graph", {"name": clean, "limit": limit},
                   {"edges": verified, "verified": len(verified)})
        return verified

    def get_file(self, full_name: str, subtask_id: str, path: str, start_line: int = 1,
                 end_line: int | None = None) -> dict[str, Any]:
        checkout = self._checkout(full_name, subtask_id)
        target = self._safe_code_path(checkout, path)
        if not target.is_file():
            raise FileNotFoundError(f"{path!r} is not a file in {full_name!r}")
        if start_line < 1:
            raise ValueError("start_line must be at least 1")
        lines = target.read_text(encoding="utf-8").splitlines()
        final = min(end_line if end_line is not None else start_line + 199, len(lines))
        if final < start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        payload = {"path": path, "start_line": start_line, "end_line": final,
                   "content": "\n".join(lines[start_line - 1:final])}
        self._emit("get_file", {"path": path, "start_line": start_line, "end_line": end_line},
                   {"path": path, "start_line": start_line, "end_line": final, "chars": len(payload["content"])})
        return payload

    def get_diff(self, full_name: str, subtask_id: str, base_commit: str, head_commit: str,
                 *, path: str | None = None, max_chars: int = 100_000) -> str:
        checkout = self._checkout(full_name, subtask_id)
        base = self._commit(checkout, base_commit)
        head = self._commit(checkout, head_commit)
        command = ["git", "diff", "--no-ext-diff", "--unified=3", base, head, "--"]
        if path:
            self._safe_code_path(checkout, path)
            command.append(path)
        result = subprocess.run(command, cwd=checkout, capture_output=True, text=True, check=False, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
        diff = result.stdout[:max(1, min(max_chars, 500_000))]
        self._emit("get_diff", {"base_commit": base_commit, "head_commit": head_commit, "path": path},
                   {"chars": len(diff), "truncated": len(result.stdout) > len(diff)})
        return diff

    def _emit(self, tool: str, inputs: dict[str, Any], output: dict[str, Any]) -> None:
        if self.event_sink:
            self.event_sink({"tool": tool, "input": inputs, "output": output})

    def _index_event(self, event: dict[str, Any]) -> None:
        if self.event_sink:
            self.event_sink({"tool": "repository_index", "input": {}, "output": event})

    def _embed(self, text: str) -> list[float]:
        if self.embedder:
            return self.embedder(text)
        from app.agents.llm import LLMClient
        return LLMClient().embed(text)

    @staticmethod
    def _symbol_paths(snapshot_id: str | None, terms: list[str]) -> set[str]:
        identifiers = [term for term in terms if _SYMBOL.fullmatch(term)]
        if not snapshot_id or not identifiers:
            return set()
        with SessionLocal() as db:
            rows = db.scalars(select(RepositorySymbol.source_file).where(
                RepositorySymbol.snapshot_id == snapshot_id,
                RepositorySymbol.name.in_(identifiers),
            ))
            return set(rows)

    def _graph_lookup(self, full_name: str, subtask_id: str, symbol: str,
                      relations: set[str], *, direction: str,
                      limit: int) -> list[dict[str, Any]]:
        snapshot_id = self._graph_snapshot_id(full_name, subtask_id)
        if snapshot_id is None:
            return []
        node = RepositoryGraphEdge.source_node if direction == "source" else RepositoryGraphEdge.target_node
        with SessionLocal() as db:
            rows = list(db.scalars(
                select(RepositoryGraphEdge).where(
                    RepositoryGraphEdge.snapshot_id == snapshot_id,
                    RepositoryGraphEdge.relation_kind.in_(relations),
                    or_(node == f"symbol:{symbol}", node.endswith(f".{symbol}")),
                ).order_by(
                    RepositoryGraphEdge.source_file,
                    RepositoryGraphEdge.start_line,
                    RepositoryGraphEdge.relation_kind,
                ).limit(_bounded_limit(limit))
            ))
        return self._verify_edges(full_name, subtask_id, rows)

    def _graph_snapshot_id(self, full_name: str, subtask_id: str):
        checkout = self._checkout(full_name, subtask_id)
        commit = self._commit(checkout, "HEAD")
        try:
            with SessionLocal() as db:
                private = db.get(RepoToken, full_name) is not None
                scope = (
                    f"private:{hashlib.sha256(full_name.encode()).hexdigest()[:24]}"
                    if private else "public"
                )
                return db.scalar(
                    select(RepositorySnapshot.id)
                    .join(Repository, Repository.id == RepositorySnapshot.repo_id)
                    .where(
                        Repository.full_name == full_name,
                        Repository.access_scope == scope,
                        RepositorySnapshot.commit_sha == commit,
                        RepositorySnapshot.index_status == "READY",
                        RepositorySnapshot.is_active.is_(True),
                    )
                )
        except SQLAlchemyError:
            return None

    def _verify_edges(self, full_name: str, subtask_id: str,
                      rows: list[RepositoryGraphEdge]) -> list[dict[str, Any]]:
        checkout = self._checkout(full_name, subtask_id)
        verified = []
        for edge in rows:
            target = self._safe_code_path(checkout, edge.source_file)
            if not target.is_file():
                continue
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            evidence = "\n".join(lines[edge.start_line - 1:edge.end_line])
            token = _edge_evidence_token(edge)
            if token and token not in evidence:
                continue
            verified.append({
                "path": edge.source_file,
                "line": edge.start_line,
                "start_line": edge.start_line,
                "end_line": edge.end_line,
                "source": edge.source_node,
                "target": edge.target_node,
                "relation": edge.relation_kind,
                "text": evidence[:1000],
                "verified": True,
                "provenance": {
                    "extractor": edge.extractor,
                    "commit_sha": edge.commit_sha,
                    "confidence": edge.confidence,
                    "evidence_kind": edge.evidence_kind,
                },
            })
        return verified

    def _checkout(self, full_name: str, subtask_id: str) -> Path:
        self.repo_tool._validate_full_name(full_name)
        workspace_id = self.repo_tool._workspace_id(subtask_id)
        checkout = self.repo_tool._checkout_path(full_name, workspace_id)
        return checkout if (checkout / ".git").is_dir() else self.repo_tool.clone_or_pull(full_name, subtask_id)

    def _safe_code_path(self, checkout: Path, path: str) -> Path:
        if any(part == ".git" or part == ".env" or part.startswith(".env.") for part in Path(path).parts):
            raise ValueError("code search cannot read repository metadata or environment files")
        return self.repo_tool._safe_path(checkout, path)

    @staticmethod
    def _commit(checkout: Path, revision: str) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"], cwd=checkout,
            capture_output=True, text=True, check=False, timeout=30,
        )
        if result.returncode != 0:
            raise ValueError(f"unknown git revision: {revision!r}")
        return result.stdout.strip()


def _bounded_limit(limit: int) -> int:
    return max(1, min(int(limit), 200))


def _query_terms(text: str) -> list[str]:
    stop = {"about", "after", "before", "could", "from", "into", "should", "that", "their", "there", "these", "this", "where", "which", "with"}
    seen: set[str] = set()
    terms = []
    for token in re.findall(r"[A-Za-z_$][A-Za-z0-9_$-]{2,}", text.lower()):
        if token not in stop and token not in seen:
            seen.add(token)
            terms.append(token)
    return terms


def _content_hash(content: str) -> str:
    import hashlib
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def _language_for_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".py": "python", ".js": "javascript", ".jsx": "javascript",
        ".ts": "typescript", ".tsx": "typescript", ".go": "go",
        ".rs": "rust", ".java": "java", ".rb": "ruby", ".php": "php",
    }.get(suffix, "unknown")


def _file_type_score(path: str) -> float:
    lower = path.lower()
    if any(part in lower for part in ("/test", "test_", ".spec.", ".test.")):
        return .7
    return 1.0 if _language_for_path(path) != "unknown" else .4


def _edge_evidence_token(edge: RepositoryGraphEdge) -> str:
    node = edge.source_node if edge.relation_kind == "CALLED_BY" else edge.target_node
    value = node.split(":", 1)[-1]
    return value.rsplit(".", 1)[-1]


def _validate_symbol(symbol: str) -> str:
    clean = symbol.strip()
    if not _SYMBOL.fullmatch(clean):
        raise ValueError(f"invalid symbol name: {symbol!r}")
    return clean


def _parse_matches(output: str, limit: int) -> list[dict[str, Any]]:
    matches = []
    for raw in output.splitlines():
        parts = raw.removeprefix("./").split(":", 3)
        if len(parts) != 4 or not parts[1].isdigit() or not parts[2].isdigit():
            continue
        matches.append({"path": parts[0], "line": int(parts[1]), "column": int(parts[2]), "text": parts[3].strip()[:500]})
        if len(matches) >= limit:
            break
    return matches


def _is_definition(line: str, symbol: str) -> bool:
    escaped = re.escape(symbol)
    return any(re.compile(pattern.pattern.format(symbol=escaped)).search(line) for pattern in _DEFINITION_PATTERNS)


def _python_callees(path: Path, relative_path: str, symbol: str) -> list[dict[str, Any]]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    target = next((node for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol), None)
    if target is None:
        return []
    calls = []
    for node in ast.walk(target):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
        if name:
            calls.append({"symbol": name, "path": relative_path, "line": node.lineno})
    return calls
