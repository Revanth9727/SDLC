"""Bounded, deterministic symbol-impact checks shared by execution and review."""
from __future__ import annotations

import ast
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.agents.state import SubtaskState
from app.tools.code_search import CodeSearchTool


class SymbolImpact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    target_file: str
    callers: list[dict[str, Any]] = Field(default_factory=list, max_length=25)
    references: list[dict[str, Any]] = Field(default_factory=list, max_length=25)
    uncovered_paths: list[str] = Field(default_factory=list, max_length=25)
    contract_changed: bool = False
    contract_changes: list[str] = Field(default_factory=list, max_length=10)
    checked: bool = True


class CodeNavigationHit(BaseModel):
    """One source-backed caller/reference returned by a code-search tool."""

    model_config = ConfigDict(extra="allow")
    path: str = Field(min_length=1)


class CodeNavigationContractError(ValueError):
    """A code-navigation operation did not satisfy its typed return contract."""


_NAVIGATION_HITS = TypeAdapter(list[CodeNavigationHit])


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
                    tool: CodeSearchTool, *, updated_source: str | None = None,
                    limit: int = 25) -> list[SymbolImpact]:
    impacts = inspect_symbols(
        state, target_file, symbols_touched(target_file, source, searches, state.code_context), tool, limit=limit
    )
    if updated_source is not None:
        for impact in impacts:
            impact.contract_changes = contract_changes(source, updated_source, impact.symbol)
            impact.contract_changed = bool(impact.contract_changes)
    return impacts


def inspect_symbols(state: SubtaskState, target_file: str, symbols: list[str],
                    tool: CodeSearchTool, *, limit: int = 25) -> list[SymbolImpact]:
    planned = {step.target_file for step in state.plan}
    impacts = []
    for symbol in list(dict.fromkeys(symbols))[:8]:
        callers = _navigation_hits(
            "get_callers",
            tool.get_callers(state.repo, state.subtask_id, symbol, limit=limit),
        )
        references = _navigation_hits(
            "get_references",
            tool.get_references(state.repo, state.subtask_id, symbol, limit=limit),
        )
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


def _navigation_hits(operation: str, result: Any) -> list[dict[str, Any]]:
    """Validate a code-navigation result before Executor/Critic consume it.

    The public tool contract uses ``[]`` for a successful lookup with no matches.
    ``None`` means the operation produced no usable result (for example, an
    unavailable adapter) and is a tool failure, not an empty successful lookup.
    """
    if result is None:
        raise CodeNavigationContractError(
            f"{operation} returned no result (None); return [] when no matches exist"
        )
    try:
        hits = _NAVIGATION_HITS.validate_python(result)
    except ValidationError as exc:
        raise CodeNavigationContractError(
            f"{operation} returned a malformed result: {exc.errors(include_url=False)}"
        ) from exc
    return [hit.model_dump(mode="python") for hit in hits]


def merge_impacts(state: SubtaskState, impacts: list[SymbolImpact]) -> None:
    retained = [item for item in state.code_impacts
                if (item.get("symbol"), item.get("target_file")) not in
                {(impact.symbol, impact.target_file) for impact in impacts}]
    state.code_impacts = retained + [impact.model_dump(mode="json") for impact in impacts]


def impact_warning(impacts: list[SymbolImpact]) -> str | None:
    affected = [
        (impact.symbol, impact.uncovered_paths, impact.contract_changes)
        for impact in impacts if impact.contract_changed and impact.uncovered_paths
    ]
    if not affected:
        return None
    details = "; ".join(
        f"{symbol} ({', '.join(changes)}) -> {', '.join(paths)}"
        for symbol, paths, changes in affected
    )
    return f"Contract changed; verify affected callers/references: {details}"


def contract_changes(before: str, after: str, symbol: str) -> list[str]:
    """Return source-proven public contract changes for one Python symbol."""
    old = _python_contract(before, symbol)
    new = _python_contract(after, symbol)
    if old is None or new is None:
        return []
    labels = {
        "signature": "signature changed",
        "return_annotation": "return type annotation changed",
        "return_shapes": "return shape changed",
        "raises": "raised exceptions changed",
        "docstring": "documented behavior changed",
    }
    return [label for field, label in labels.items() if old[field] != new[field]]


def _python_contract(source: str, symbol: str) -> dict[str, Any] | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    candidates = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == symbol
    ]
    if not candidates:
        return None
    node = min(candidates, key=lambda item: item.lineno)
    if isinstance(node, ast.ClassDef):
        initializer = next(
            (item for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
             and item.name == "__init__"), None
        )
        signature = _signature(initializer) if initializer else "class()"
        return_annotation = None
    else:
        signature = _signature(node)
        return_annotation = ast.unparse(node.returns) if node.returns else None
    return {
        "signature": signature,
        "return_annotation": return_annotation,
        "return_shapes": sorted({_return_shape(item.value) for item in ast.walk(node)
                                  if isinstance(item, ast.Return)}),
        "raises": sorted({_raise_type(item.exc) for item in ast.walk(node)
                          if isinstance(item, ast.Raise) and item.exc is not None}),
        "docstring": ast.get_docstring(node, clean=True),
    }


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef | None) -> str:
    if node is None:
        return ""
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    return f"{prefix}{node.name}({ast.unparse(node.args)})"


def _return_shape(value: ast.expr | None) -> str:
    if value is None or (isinstance(value, ast.Constant) and value.value is None):
        return "none"
    if isinstance(value, ast.Dict):
        return "mapping"
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return type(value).__name__.lower()
    if isinstance(value, ast.Constant):
        return type(value.value).__name__
    return "value"


def _raise_type(value: ast.expr) -> str:
    target = value.func if isinstance(value, ast.Call) else value
    return ast.unparse(target)
