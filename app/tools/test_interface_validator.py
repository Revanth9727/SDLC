"""Deterministic grounding and validation of Python test-facing interfaces."""
from __future__ import annotations

import ast
from collections import deque
from pathlib import PurePosixPath
from typing import Any, Callable

from pydantic import BaseModel, Field


class TestInterfaceValidationError(ValueError):
    """A generated test contradicts a verified code-under-test interface."""

    __test__ = False


class UnresolvedTestInterfaceError(TestInterfaceValidationError):
    """The real interface required for a test could not be proven from source."""


SymbolFinder = Callable[[str], list[dict[str, Any]]]
FileReader = Callable[[str], dict[str, Any]]


class TestInterfaceValidationResult(BaseModel):
    """Interface checks that could not be proven, without treating them as invalid."""

    __test__ = False
    unresolved_types: list[str] = Field(default_factory=list)
    skipped_checks: list[str] = Field(default_factory=list)


_MANIFEST_META = "__meta__"
_NON_REPO_TYPES = {
    "Any", "None", "NoneType", "bool", "bytes", "dict", "float", "int", "list",
    "object", "set", "str", "tuple",
}


def resolve_test_interfaces(
    source_paths: list[str],
    find_symbol: SymbolFinder,
    get_file: FileReader,
) -> dict[str, dict[str, Any]]:
    """Build a source-verified interface manifest, following returned repo types."""
    interfaces: dict[str, dict[str, Any]] = {}
    unresolved_types: set[str] = set()
    queued_types: deque[str] = deque()
    for path in dict.fromkeys(source_paths):
        if PurePosixPath(path).suffix != ".py":
            continue
        try:
            tree = _read_tree(path, get_file)
        except (KeyError, OSError, SyntaxError, TypeError, ValueError) as exc:
            raise UnresolvedTestInterfaceError(
                f"Cannot resolve the real test interface in {path}: {exc}"
            ) from exc
        classes = _classes_in(tree, path)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            definitions = _definitions(find_symbol, node.name)
            if not any(str(item.get("path", "")).removeprefix("./") == path for item in definitions):
                raise UnresolvedTestInterfaceError(
                    f"Cannot verify symbol {node.name!r} against its real module {path}"
                )
            if isinstance(node, ast.ClassDef):
                interfaces[node.name] = classes[node.name]
            else:
                interface = _callable_interface(node, path, drop_first=False)
                returned = _returned_class(node, classes)
                interface["returns"] = returned or interface.get("returns")
                interfaces[node.name] = interface
            queued_types.extend(_returned_types(interfaces[node.name]))
    if not interfaces:
        raise UnresolvedTestInterfaceError(
            "No resolvable Python code-under-test interfaces were found for this test"
        )

    # A return type may live in an imported module. Resolve it through the same
    # deterministic symbol/file tools used for the directly targeted source.
    visited_types: set[str] = set()
    while queued_types and len(visited_types) < 50:
        type_name = queued_types.popleft()
        if type_name in interfaces or type_name in visited_types or type_name in _NON_REPO_TYPES:
            continue
        visited_types.add(type_name)
        resolved = _resolve_class(type_name, find_symbol, get_file)
        if resolved is None:
            unresolved_types.add(type_name)
            continue
        _, interface = resolved
        interfaces[type_name] = interface
        queued_types.extend(_returned_types(interface))

    interfaces[_MANIFEST_META] = {"unresolved_types": sorted(unresolved_types)}
    return interfaces


def validate_test_interfaces(
    content: str, interfaces: dict[str, dict[str, Any]],
) -> TestInterfaceValidationResult:
    """Reject proven contradictions and report interface checks that cannot be proven."""
    try:
        tree = ast.parse(content, filename="generated test")
    except SyntaxError as exc:
        raise TestInterfaceValidationError(f"Generated test is not valid Python: {exc.msg}") from exc

    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in interfaces:
                    imported[alias.asname or alias.name] = alias.name

    variable_types: dict[str, str] = {}
    unresolved_variables: dict[str, str] = {}
    errors: list[str] = []
    unresolved_types = set(interfaces.get(_MANIFEST_META, {}).get("unresolved_types", []))
    skipped_checks: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            inferred = _expression_type(value, imported, variable_types, interfaces)
            if inferred:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        variable_types[target.id] = inferred
            elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                    and value.func.id in imported:
                symbol = imported[value.func.id]
                if interfaces[symbol]["kind"] == "function" and not interfaces[symbol].get("returns"):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            unresolved_variables[target.id] = symbol

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in imported:
            symbol = imported[node.func.id]
            _check_call(node, interfaces[symbol], symbol, errors)
        elif isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id in unresolved_variables:
                owner = unresolved_variables[node.func.value.id]
                unresolved_types.add(owner)
                skipped_checks.append(f"{owner}.{node.func.attr}()")
                continue
            owner = _expression_type(node.func.value, imported, variable_types, interfaces)
            if not owner:
                continue
            interface = interfaces.get(owner)
            if interface is None:
                unresolved_types.add(owner)
                skipped_checks.append(f"{owner}.{node.func.attr}()")
                continue
            methods = interface.get("methods", {}) if interface else {}
            if node.func.attr not in methods:
                errors.append(f"{owner!r} has no method {node.func.attr!r} in the real source")
            else:
                _check_call(node, methods[node.func.attr], f"{owner}.{node.func.attr}", errors)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or isinstance(node.ctx, ast.Store):
            continue
        if isinstance(node.value, ast.Name) and node.value.id in unresolved_variables:
            owner = unresolved_variables[node.value.id]
            unresolved_types.add(owner)
            skipped_checks.append(f"{owner}.{node.attr}")
            continue
        owner = _expression_type(node.value, imported, variable_types, interfaces)
        if not owner:
            continue
        interface = interfaces.get(owner)
        if interface is None:
            unresolved_types.add(owner)
            skipped_checks.append(f"{owner}.{node.attr}")
            continue
        allowed = set(interface.get("attributes", [])) | set(interface.get("methods", {}))
        if interface and node.attr not in allowed:
            errors.append(f"{owner!r} has no attribute {node.attr!r} in the real source")

    if errors:
        raise TestInterfaceValidationError(
            "Generated test does not match the real code interface: " + "; ".join(dict.fromkeys(errors))
        )
    return TestInterfaceValidationResult(
        unresolved_types=sorted(unresolved_types),
        skipped_checks=list(dict.fromkeys(skipped_checks)),
    )


def _read_tree(path: str, get_file: FileReader) -> ast.Module:
    payload = get_file(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
        raise TypeError("get_file returned no readable content")
    return ast.parse(payload["content"], filename=path)


def _definitions(find_symbol: SymbolFinder, symbol: str) -> list[dict[str, Any]]:
    result = find_symbol(symbol)
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, dict) and item.get("path")]


def _classes_in(tree: ast.Module, path: str) -> dict[str, dict[str, Any]]:
    return {
        node.name: _class_interface(node, path)
        for node in tree.body if isinstance(node, ast.ClassDef)
    }


def _resolve_class(
    type_name: str, find_symbol: SymbolFinder, get_file: FileReader,
) -> tuple[str, dict[str, Any]] | None:
    matches: list[tuple[str, dict[str, Any]]] = []
    for definition in _definitions(find_symbol, type_name):
        path = str(definition["path"]).removeprefix("./")
        if PurePosixPath(path).suffix != ".py":
            continue
        try:
            tree = _read_tree(path, get_file)
        except (KeyError, OSError, SyntaxError, TypeError, ValueError):
            continue
        node = next(
            (item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == type_name),
            None,
        )
        if node is not None:
            matches.append((path, _class_interface(node, path)))
    unique = {path: interface for path, interface in matches}
    if len(unique) != 1:
        return None
    path, interface = next(iter(unique.items()))
    return path, interface


def _returned_types(interface: dict[str, Any]) -> list[str]:
    returned: list[str] = []
    direct = interface.get("returns")
    if direct:
        returned.append(str(direct))
    for method in interface.get("methods", {}).values():
        if method.get("returns"):
            returned.append(str(method["returns"]))
    return returned


def _class_interface(node: ast.ClassDef, path: str) -> dict[str, Any]:
    methods: dict[str, dict[str, Any]] = {}
    attributes: set[str] = set()
    constructor = None
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            method = _callable_interface(child, path, drop_first=True)
            methods[child.name] = method
            if child.name == "__init__":
                constructor = method
            for nested in ast.walk(child):
                if isinstance(nested, (ast.Assign, ast.AnnAssign)):
                    targets = nested.targets if isinstance(nested, ast.Assign) else [nested.target]
                    for target in targets:
                        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                                and target.value.id in {"self", "cls"}:
                            attributes.add(target.attr)
        elif isinstance(child, (ast.Assign, ast.AnnAssign)):
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            attributes.update(target.id for target in targets if isinstance(target, ast.Name))

    field_names = [
        child.target.id for child in node.body
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)
    ]
    attributes.update(field_names)
    if constructor is None and field_names and _data_class_like(node):
        required = [
            child.target.id for child in node.body
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name) and child.value is None
        ]
        constructor = {
            "required_positional": required,
            "positional": field_names,
            "keyword_only": [],
            "accepts_varargs": False,
            "accepts_kwargs": False,
        }
    if constructor is None and not node.bases:
        constructor = {
            "required_positional": [], "positional": [], "keyword_only": [],
            "accepts_varargs": False, "accepts_kwargs": False,
        }
    return {
        "kind": "class", "path": path, "constructor": constructor,
        "methods": methods, "attributes": sorted(attributes),
    }


def _callable_interface(node: ast.FunctionDef | ast.AsyncFunctionDef, path: str,
                        *, drop_first: bool) -> dict[str, Any]:
    positional = [arg.arg for arg in (*node.args.posonlyargs, *node.args.args)]
    defaults = len(node.args.defaults)
    required_count = len(positional) - defaults
    if drop_first and positional and positional[0] in {"self", "cls"}:
        positional = positional[1:]
        required_count = max(0, required_count - 1)
    required = positional[:required_count]
    keyword_only = [arg.arg for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is None]
    returned = _annotation_type(node.returns)
    return {
        "kind": "function", "path": path,
        "required_positional": required,
        "positional": positional,
        "keyword_only": keyword_only,
        "accepts_varargs": node.args.vararg is not None,
        "accepts_kwargs": node.args.kwarg is not None,
        "returns": returned,
    }


def _annotation_type(annotation: ast.expr | None) -> str | None:
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    if isinstance(annotation, ast.Subscript):
        candidates = [
            item.id for item in ast.walk(annotation.slice)
            if isinstance(item, ast.Name) and item.id not in _NON_REPO_TYPES
        ]
        return candidates[0] if candidates else None
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotation_type(annotation.left) or _annotation_type(annotation.right)
    return None


def _returned_class(node: ast.FunctionDef | ast.AsyncFunctionDef,
                    classes: dict[str, dict[str, Any]]) -> str | None:
    if isinstance(node.returns, ast.Name) and node.returns.id in classes:
        return node.returns.id
    returned = {
        value.func.id for value in (item.value for item in ast.walk(node) if isinstance(item, ast.Return))
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in classes
    }
    return next(iter(returned)) if len(returned) == 1 else None


def _expression_type(node: ast.AST | None, imported: dict[str, str],
                     variables: dict[str, str], interfaces: dict[str, dict[str, Any]]) -> str | None:
    if isinstance(node, ast.Name):
        return variables.get(node.id)
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name) and node.func.id in imported:
        symbol = imported[node.func.id]
        interface = interfaces[symbol]
        return symbol if interface["kind"] == "class" else interface.get("returns")
    if isinstance(node.func, ast.Attribute):
        owner = _expression_type(node.func.value, imported, variables, interfaces)
        method = interfaces.get(owner, {}).get("methods", {}).get(node.func.attr) if owner else None
        return method.get("returns") if method else None
    return None


def _check_call(node: ast.Call, interface: dict[str, Any], label: str, errors: list[str]) -> None:
    spec = interface.get("constructor", interface)
    if spec is None:
        raise UnresolvedTestInterfaceError(
            f"Cannot prove inherited constructor arguments for {label!r} from the resolved source"
        )
    positional_count = len(node.args)
    keywords = {item.arg for item in node.keywords if item.arg is not None}
    required = set(spec.get("required_positional", [])) | set(spec.get("keyword_only", []))
    supplied_positionally = set(spec.get("positional", [])[:positional_count])
    missing = sorted(required - supplied_positionally - keywords)
    if missing:
        errors.append(f"{label} is missing required argument(s): {', '.join(missing)}")
    positional = spec.get("positional", [])
    if positional_count > len(positional) and not spec.get("accepts_varargs"):
        errors.append(f"{label} accepts at most {len(positional)} positional argument(s), got {positional_count}")
    allowed_keywords = set(positional) | set(spec.get("keyword_only", []))
    unknown = sorted(keywords - allowed_keywords) if not spec.get("accepts_kwargs") else []
    if unknown:
        errors.append(f"{label} has no argument(s): {', '.join(unknown)}")


def _data_class_like(node: ast.ClassDef) -> bool:
    decorators = {ast.unparse(item) for item in node.decorator_list}
    bases = {ast.unparse(item) for item in node.bases}
    return "dataclass" in decorators or bool(bases & {"BaseModel", "NamedTuple"})
