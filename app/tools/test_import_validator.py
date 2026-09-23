"""Deterministic syntax and code-under-test import validation for generated tests."""
from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Any


class TestImportValidationError(ValueError):
    """A generated test cannot safely proceed to pytest."""

    __test__ = False


@dataclass(frozen=True)
class SourceModule:
    path: str
    modules: frozenset[str]
    symbols: frozenset[str]


SymbolResolver = Callable[[str], list[dict[str, Any]]]


def repair_missing_test_imports(
    checkout: Path,
    test_path: str,
    source_paths: list[str],
    resolve_symbol: SymbolResolver,
) -> tuple[str, list[str]]:
    """Insert imports for uniquely resolved, AST-verified missing symbols.

    The normal validator remains authoritative. This helper repairs only a bare
    symbol that resolves to exactly one real repository module, then validates
    the resulting file using that module as code-under-test evidence.
    """
    target = (checkout / test_path).resolve()
    content = target.read_text(encoding="utf-8")
    tree = ast.parse(content, filename=test_path)
    sources = [source for path in source_paths if (source := _source_module(checkout, path))]
    imported = _locally_bound_names(tree)
    loaded = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    candidates = sorted(loaded - imported - set(dir(builtins)))
    repairs: list[tuple[str, SourceModule]] = []
    for symbol in candidates:
        definitions = resolve_symbol(symbol)
        resolved: dict[str, SourceModule] = {}
        for definition in definitions:
            path = str(definition.get("path", "")).removeprefix("./")
            if path == test_path:
                continue
            source = _source_module(checkout, path)
            if source and symbol in source.symbols:
                resolved[source.path] = source
        if len(resolved) == 1:
            repairs.append((symbol, next(iter(resolved.values()))))

    if repairs:
        imports = [f"from {sorted(source.modules, key=lambda value: (value.count('.'), len(value), value))[0]} import {symbol}"
                   for symbol, source in repairs]
        content = _insert_imports(content, tree, imports)
        target.write_text(content, encoding="utf-8")
        sources.extend(source for _, source in repairs if source not in sources)

    validate_test_imports(checkout, test_path, list(dict.fromkeys(source.path for source in sources)))
    return content, [symbol for symbol, _ in repairs]


def validate_test_imports(checkout: Path, test_path: str, source_paths: list[str]) -> None:
    """Validate generated Python test syntax and imports against real repo files.

    ``source_paths`` is the code-under-test supplied to the Executor. Symbols from
    those modules that the test loads must be imported from the module that really
    defines them (directly or through a module-qualified reference).
    """
    target = (checkout / test_path).resolve()
    try:
        tree = ast.parse(target.read_text(encoding="utf-8"), filename=test_path)
    except SyntaxError as exc:
        location = f"line {exc.lineno}, column {exc.offset}" if exc.lineno else "unknown location"
        raise TestImportValidationError(
            f"Generated test {test_path} is not valid Python ({location}): {exc.msg}"
        ) from exc

    sources = [_source_module(checkout, path) for path in source_paths]
    sources = [source for source in sources if source is not None]
    if not sources:
        raise TestImportValidationError(
            f"Generated test {test_path} has no readable code-under-test module to validate imports against"
        )

    direct_imports: dict[str, tuple[SourceModule, str]] = {}
    module_imports: dict[str, SourceModule] = {}
    imported_origins: dict[str, tuple[str, str]] = {}
    module_origins: dict[str, str] = {}
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            source = _resolve_source(node.module, sources)
            for alias in node.names:
                if alias.name != "*":
                    imported_origins[alias.asname or alias.name] = (node.module, alias.name)
            if source:
                for alias in node.names:
                    if alias.name == "*":
                        errors.append(f"wildcard import from {node.module!r} cannot prove which code-under-test symbols are bound")
                    elif alias.name not in source.symbols:
                        errors.append(f"{alias.name!r} is not defined by {source.path}; import it from its real module")
                    else:
                        direct_imports[alias.asname or alias.name] = (source, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module_origins[alias.asname or alias.name.split(".")[0]] = alias.name
                source = _resolve_source(alias.name, sources)
                if source:
                    module_imports[alias.asname or alias.name.split(".")[0]] = source

    local_names = _locally_bound_names(tree)
    loaded_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    symbol_sources: dict[str, list[SourceModule]] = {}
    for source in sources:
        for symbol in source.symbols:
            symbol_sources.setdefault(symbol, []).append(source)

    for name in sorted(loaded_names - local_names - set(dir(builtins))):
        owners = symbol_sources.get(name, [])
        if owners and name not in direct_imports:
            locations = ", ".join(source.path for source in owners)
            errors.append(f"code-under-test symbol {name!r} is referenced but not imported from {locations}")
    for name in sorted(loaded_names & imported_origins.keys()):
        imported_module, imported_symbol = imported_origins[name]
        owners = symbol_sources.get(imported_symbol, [])
        if owners and name not in direct_imports:
            expected = ", ".join(sorted(module for owner in owners for module in owner.modules))
            errors.append(
                f"code-under-test symbol {imported_symbol!r} is imported from {imported_module!r}, "
                f"not its real repo module ({expected})"
            )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load):
            continue
        root, attributes = _attribute_chain(node)
        source = module_imports.get(root)
        if source:
            if attributes and attributes[0] not in source.symbols:
                errors.append(f"{attributes[0]!r} is not defined by imported module {source.path}")
        elif root in module_origins and attributes and attributes[0] in symbol_sources:
            owners = symbol_sources[attributes[0]]
            expected = ", ".join(sorted(module for owner in owners for module in owner.modules))
            errors.append(
                f"code-under-test symbol {attributes[0]!r} is referenced through module "
                f"{module_origins[root]!r}, not its real repo module ({expected})"
            )

    if errors:
        raise TestImportValidationError(
            f"Generated test {test_path} has invalid code-under-test imports: " + "; ".join(dict.fromkeys(errors))
        )


def _source_module(checkout: Path, path: str) -> SourceModule | None:
    candidate = (checkout / path).resolve()
    if not candidate.is_file() or candidate.suffix != ".py" or checkout.resolve() not in candidate.parents:
        return None
    try:
        tree = ast.parse(candidate.read_text(encoding="utf-8"), filename=path)
    except (OSError, SyntaxError):
        return None
    symbols = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                symbols.update(name.id for name in ast.walk(target) if isinstance(name, ast.Name))
    return SourceModule(path=path, modules=frozenset(_module_names(path)), symbols=frozenset(symbols))


def _module_names(path: str) -> set[str]:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    names = {".".join(parts)}
    # Common source roots are import roots rather than package names.
    if len(parts) > 1 and parts[0] in {"src", "lib", "python"}:
        names.add(".".join(parts[1:]))
    return {name for name in names if name}


def _resolve_source(module: str, sources: list[SourceModule]) -> SourceModule | None:
    return next((source for source in sources if module in source.modules), None)


def _locally_bound_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names if alias.name != "*")
    return names


def _attribute_chain(node: ast.Attribute) -> tuple[str | None, list[str]]:
    attributes = [node.attr]
    value = node.value
    while isinstance(value, ast.Attribute):
        attributes.append(value.attr)
        value = value.value
    return (value.id if isinstance(value, ast.Name) else None, list(reversed(attributes)))


def _insert_imports(content: str, tree: ast.Module, imports: list[str]) -> str:
    lines = content.splitlines(keepends=True)
    insert_after = 0
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        insert_after = body[0].end_lineno or body[0].lineno
    for node in body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            insert_after = max(insert_after, node.end_lineno or node.lineno)
    block = "".join(f"{statement}\n" for statement in imports)
    lines.insert(insert_after, block)
    return "".join(lines)
