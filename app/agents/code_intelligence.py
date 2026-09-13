"""Single bounded investigator over deterministic repository-intelligence tools."""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.llm import LLMClient
from app.agents.router import model_tier
from app.agents.state import BudgetUsed, SubtaskState
from app.config import settings
from app.repo_intelligence.indexer import RepositoryIndexer
from app.tools.code_search import CodeSearchTool, ToolEventSink
from app.tools.repo_tool import RepoTool


class InvestigationPlan(BaseModel):
    investigation_type: Literal["bug_trace", "data_flow", "api_flow", "configuration", "general"]
    search_queries: list[str] = Field(min_length=1, max_length=4)
    symbols: list[str] = Field(default_factory=list, max_length=6)


class CodeContext(BaseModel):
    relevant_files: list[str] = Field(default_factory=list, max_length=12)
    relevant_functions: list[str] = Field(default_factory=list, max_length=20)
    execution_path: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)
    hypothesis: str = ""
    verified_evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    relevant_chunks: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    investigation_type: str = "general"
    repo_snapshot_id: str
    commit_sha: str
    steps_used: int = Field(default=0, ge=0)
    limit_reached: bool = False


class CodeIntelligenceAgent:
    """The one reasoning agent that investigates code; every capability it uses is a tool."""

    def __init__(self, llm: LLMClient | None = None, repo_tool: RepoTool | None = None,
                 search_tool: CodeSearchTool | None = None,
                 event_sink: ToolEventSink | None = None, max_steps: int | None = None,
                 indexer: RepositoryIndexer | None = None) -> None:
        self.llm = llm or LLMClient()
        self.repo_tool = repo_tool or RepoTool()
        self.search = search_tool or CodeSearchTool(self.repo_tool, event_sink=event_sink)
        self.max_steps = max_steps or settings.code_intelligence_max_steps
        self.indexer = indexer

    def run(self, state: SubtaskState) -> SubtaskState:
        checkout = self.repo_tool.clone_or_pull(state.repo, state.subtask_id)
        snapshot = (self.indexer or RepositoryIndexer(
            self.repo_tool, event_sink=self.search._index_event,
            embedder=self.search.embedder,
        )).ensure(state.repo, state.subtask_id)
        commit = self.repo_tool.revision(state.repo, state.subtask_id)
        state.repo_snapshot_id = str(snapshot.id)

        plan = self.llm.complete_json(
            _PLAN_SYSTEM, state.description, InvestigationPlan,
            tier=model_tier("code_intelligence"), ticket_id=state.ticket_id,
        )
        steps = 0
        limit_reached = False
        candidates: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        functions: set[str] = set()

        def available() -> bool:
            nonlocal limit_reached
            if steps >= self.max_steps:
                limit_reached = True
                return False
            return True

        for query in plan.search_queries:
            if not available():
                break
            hits = self.search.search_hybrid(
                state.repo, state.subtask_id, query, limit=12,
            )
            steps += 1
            candidates.extend(hits)

        candidates = _dedupe(candidates)[:12]
        for hit in candidates[:6]:
            if not available():
                break
            chunk = self.search.get_file(
                state.repo, state.subtask_id, hit["path"],
                hit.get("start_line", hit.get("line", 1)),
                hit.get("end_line", hit.get("line", 1) + 100),
            )
            steps += 1
            evidence.append({
                "kind": "source", "path": chunk["path"],
                "start_line": chunk["start_line"], "end_line": chunk["end_line"],
                "content": chunk["content"][:4000], "verified": True,
            })
            label = hit.get("label", "")
            match = re.match(r"(?:function|class)\s+([A-Za-z_$][\w$]*)", label)
            if match:
                functions.add(match.group(1))

        symbols = list(dict.fromkeys([*plan.symbols, *functions]))[:6]
        for symbol in symbols:
            if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", symbol):
                continue
            for relation, method in (
                ("definition", self.search.find_symbol),
                ("caller", self.search.get_callers),
                ("callee", self.search.get_callees),
                ("reference", self.search.get_references),
            ):
                if not available():
                    break
                rows = method(state.repo, state.subtask_id, symbol, limit=8)
                steps += 1
                for row in rows:
                    if row.get("verified"):
                        evidence.append({"kind": relation, "symbol": symbol, **row})

        evidence = _dedupe_evidence(evidence)[:40]
        result = self.llm.complete_json(
            _SYNTHESIS_SYSTEM,
            json.dumps({
                "ticket": state.description,
                "investigation_type": plan.investigation_type,
                "verified_source_evidence": evidence,
                "repo_snapshot_id": str(snapshot.id),
                "commit_sha": commit,
            }, default=str),
            CodeContext,
            tier=model_tier("code_intelligence"), ticket_id=state.ticket_id,
        )
        verified_paths = list(dict.fromkeys(
            item["path"] for item in evidence if item.get("verified") and item.get("path")
        ))
        result.relevant_files = [path for path in result.relevant_files if path in verified_paths]
        result.relevant_files.extend(path for path in verified_paths if path not in result.relevant_files)
        result.relevant_files = result.relevant_files[:12]
        graph_symbols = {
            node.split(":", 1)[-1].rsplit(".", 1)[-1]
            for item in evidence for node in (item.get("source", ""), item.get("target", ""))
            if node.startswith("symbol:")
        }
        verified_functions = functions | graph_symbols
        result.relevant_functions = [
            name for name in result.relevant_functions if name in verified_functions
        ]
        result.relevant_functions.extend(
            name for name in sorted(verified_functions) if name not in result.relevant_functions
        )
        result.relevant_functions = result.relevant_functions[:20]
        path_tokens = set(result.relevant_files) | verified_functions
        result.execution_path = [
            step for step in result.execution_path if any(token in step for token in path_tokens)
        ][:20]
        if not result.execution_path:
            result.execution_path = list(dict.fromkeys(
                f"{item['source']} -> {item['target']}"
                for item in evidence if item.get("source") and item.get("target")
            ))[:20]
        result.verified_evidence = evidence
        result.relevant_chunks = [
            {"path": item["path"], "start_line": item.get("start_line", item.get("line", 1)),
             "end_line": item.get("end_line", item.get("line", 1))}
            for item in evidence if item.get("kind") == "source"
        ][:20]
        result.investigation_type = plan.investigation_type
        result.repo_snapshot_id = str(snapshot.id)
        result.commit_sha = commit
        result.steps_used = steps
        result.limit_reached = limit_reached
        if not evidence:
            result.confidence = min(result.confidence, 0.2)
            result.hypothesis = result.hypothesis or "No source-backed hypothesis could be verified."
        elif limit_reached:
            result.confidence = min(result.confidence, 0.5)

        state.code_context = result.model_dump(mode="json")
        state.budget_used = BudgetUsed.model_validate(self.llm.get_usage(state.ticket_id))
        return state


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, int, int], dict[str, Any]] = {}
    for row in rows:
        key = (row.get("path", ""), row.get("start_line", row.get("line", 1)),
               row.get("end_line", row.get("line", 1)))
        if key not in unique or row.get("score", 0) > unique[key].get("score", 0):
            unique[key] = row
    return sorted(unique.values(), key=lambda row: (-row.get("score", 0), row.get("path", "")))


def _dedupe_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {}
    for row in rows:
        key = (row.get("kind"), row.get("path"), row.get("line", row.get("start_line")),
               row.get("source"), row.get("target"))
        unique.setdefault(key, row)
    return list(unique.values())


_PLAN_SYSTEM = """You are the single Code-Intelligence investigator. Decide how to investigate
this isolated subtask. Return a small set of natural-language semantic queries and concrete
symbols when the ticket names them. Do not diagnose or propose edits yet."""

_SYNTHESIS_SYSTEM = """You are the single Code-Intelligence investigator. Using ONLY the
verified source evidence provided, form a concise navigation hypothesis and execution path.
Never claim an unverified relationship. relevant_files must come from the evidence. Lower
confidence when evidence is incomplete. This is code location, not a fix plan."""
