"""Bounded, deterministic symbol-impact checks shared by execution and review."""
from __future__ import annotations

import ast
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agents.state import SubtaskState
from app.tools.code_search import CodeSearchTool


class SymbolImpact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    target_file: str
    callers: list[dict[str, Any]] = Field(default_factory=list, max_length=25)
    references: list[dict[str, Any]] = Field(default_factory=list, max_length=25)
    uncovered_paths: list[str] = Field(default_factory=list, max_length=25)
    checked: bool = True


def symbols_touched(path: str, source: str, searches: list[str], context: dict[str, Any]) -> list[str]:
    """Resolve edited symbols from real source spans, with verified context fallback."""
    names: list[str] = []
    if path.endswith(".py"):
        try:
            tree = ast.parse(source)
            offsets = [source.find(search) for search in searches if search and source.find(search) >= 0]
            line_offsets = [source.count("\n", 0, offset) + 1 for offset in offsets]
            candidates = [node for node in ast.walk(tree)
                          if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            for line in line_offsets:
                enclosing = [node for node in candidates
                             if node.lineno <= line <= getattr(node, "end_lineno", node.lineno)]
                if enclosing:
                    names.append(min(enclosing, key=lambda node: getattr(node, "end_lineno", node.lineno) - node.lineno).name)
        except SyntaxError:
            pass
    verified_paths = {
        item.get("path") for item in context.get("verified_evidence", []) if item.get("verified")
    }
    if path in verified_paths:
        for name in context.get("relevant_functions", []):
            clean = str(name).rsplit(".", 1)[-1]
            if clean.isidentifier() and (clean in source or not names):
                names.append(clean)
    return list(dict.fromkeys(names))[:8]


def inspect_impacts(state: SubtaskState, target_file: str, source: str, searches: list[str],
                    tool: CodeSearchTool, *, limit: int = 25) -> list[SymbolImpact]:
    return inspect_symbols(
        state, target_file, symbols_touched(target_file, source, searches, state.code_context), tool, limit=limit
    )


def inspect_symbols(state: SubtaskState, target_file: str, symbols: list[str],
                    tool: CodeSearchTool, *, limit: int = 25) -> list[SymbolImpact]:
    planned = {step.target_file for step in state.plan}
    impacts = []
    for symbol in list(dict.fromkeys(symbols))[:8]:
        callers = tool.get_callers(state.repo, state.subtask_id, symbol, limit=limit)
        references = tool.get_references(state.repo, state.subtask_id, symbol, limit=limit)
        external = {
            item.get("path") for item in callers + references
            if item.get("path") and item.get("path") != target_file
        }
        impacts.append(SymbolImpact(
            symbol=symbol,
            target_file=target_file,
            callers=callers[:limit],
            references=references[:limit],
            uncovered_paths=sorted(path for path in external if path not in planned)[:limit],
        ))
    return impacts


def merge_impacts(state: SubtaskState, impacts: list[SymbolImpact]) -> None:
    retained = [item for item in state.code_impacts
                if (item.get("symbol"), item.get("target_file")) not in
                {(impact.symbol, impact.target_file) for impact in impacts}]
    state.code_impacts = retained + [impact.model_dump(mode="json") for impact in impacts]


def impact_warning(impacts: list[SymbolImpact]) -> str | None:
    affected = [(impact.symbol, impact.uncovered_paths) for impact in impacts if impact.uncovered_paths]
    if not affected:
        return None
    details = "; ".join(f"{symbol} -> {', '.join(paths)}" for symbol, paths in affected)
    return f"Changed symbol has callers/references outside the approved plan: {details}"
