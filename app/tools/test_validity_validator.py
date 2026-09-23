"""Deterministic internal-consistency and requirement-alignment checks for generated Python tests."""
from __future__ import annotations

import ast
import re
from typing import Any, Literal

from pydantic import BaseModel, Field


class TestScenario(BaseModel):
    """One lexical result binding; evidence is captured when its call is assigned."""

    __test__ = False

    test_name: str
    result_binding: str
    source_line: int
    call: str | None = None
    literal_inputs: list[Any] = Field(default_factory=list)
    setup_evidence: list[str] = Field(default_factory=list)
    recognized_outcomes: list[Literal["PASS", "FAIL"]] = Field(default_factory=list)
    assertion_lines: list[int] = Field(default_factory=list)
    test_behavior: str = "unresolved"
    internally_consistent: bool = True
    requirement_mapping_established: bool = False
    alignment: Literal["ALIGNED", "CONTRADICTION", "UNKNOWN"] = "UNKNOWN"
    requirement_evidence: list[str] = Field(default_factory=list)
    contradiction_reason: str | None = None


class RequirementAlignmentResult(BaseModel):
    """Structured result from the Layer 2 requirement alignment check (R-32f)."""

    __test__ = False

    # Legacy acceptance flag: True means no proven contradiction, not proven alignment.
    aligned: bool
    alignment: Literal["ALIGNED", "CONTRADICTION", "UNKNOWN"] = "UNKNOWN"
    scenarios: list[TestScenario] = Field(default_factory=list)
    requirement_behavior: str
    # Summary of recognized outcomes: PASS, FAIL, MIXED, or unresolved.
    test_behavior: str
    recognized_outcomes: list[Literal["PASS", "FAIL"]] = Field(default_factory=list)
    unique_outcomes: list[Literal["PASS", "FAIL"]] = Field(default_factory=list)
    internally_consistent: bool = True
    contradiction_reason: str | None = None


class RequirementContradictionError(ValueError):
    """Generated test contradicts the approved ticket requirement (R-32f).

    Classified as a reasoning failure (R-8b): regenerate the TEST, never modify
    production code to satisfy a requirement-contradicting assertion.
    """

    __test__ = False


class TestValidityResult(BaseModel):
    __test__ = False

    status: Literal["consistent", "invalid"]
    issues: list[str] = Field(default_factory=list)
    alignment: RequirementAlignmentResult | None = None


class InvalidGeneratedTestError(ValueError):
    """A generated test contradicts its own configured requirement."""

    __test__ = False


_DENIAL_MARKERS = ("forbid", "forbidden", "deny", "denied", "block", "disallow", "reject")
_SUCCESS_ATTRS = {"passed", "pass", "ok", "valid", "success", "allowed"}

_PASS_REQUIREMENT_TOKENS = (
    "should be ignored", "should be allowed", "should be permitted",
    "should be accepted", "should pass", "should not fail",
    "no effect",
)
_FAIL_REQUIREMENT_TOKENS = (
    "should fail", "must fail", "should be rejected", "should be blocked",
    "should be denied", "should be disallowed",
)


def validate_test_validity(content: str, requirement: str) -> TestValidityResult:
    """Two-gate validation: internal consistency (Layer 1) then requirement alignment (Layer 2).

    Layer 1: deterministic facts provable from the test's own AST (self-contradiction check).
    Layer 2: the behavior asserted by the test agrees with the approved ticket requirement (R-32f).
    The Critic separately reviews subtler requirement semantics before publication.
    """
    try:
        tree = ast.parse(content, filename="generated test")
    except SyntaxError as exc:
        raise InvalidGeneratedTestError(f"Generated test is not valid Python: {exc.msg}") from exc

    # Check both layers with the same scenario evidence; internal failures take priority.
    alignment = validate_requirement_alignment(content, requirement, _tree=tree)
    if not alignment.internally_consistent:
        raise InvalidGeneratedTestError(alignment.contradiction_reason)
    if not alignment.aligned:
        msg = alignment.contradiction_reason or "Generated test contradicts the approved requirement"
        raise RequirementContradictionError(msg)

    return TestValidityResult(status="consistent", alignment=alignment)


def normalize_assertion_outcome(node: ast.AST) -> Literal["PASS", "FAIL", "unresolved"]:
    """Semantic normalizer for an assertion's test node (R-32e).

    Covers all canonical forms and equivalent success attributes
    (passed, ok, valid, success, allowed):

    PASS:  ``assert result.passed``
    PASS:  ``assert result.passed is True``
    PASS:  ``assert result.passed == True``
    FAIL:  ``assert not result.passed``
    FAIL:  ``assert result.passed is False``
    FAIL:  ``assert result.passed == False``
    """
    # Direct attribute access: `result.passed` → PASS
    if (isinstance(node, ast.Attribute) and node.attr.lower() in _SUCCESS_ATTRS
            and isinstance(node.value, ast.Name)):
        return "PASS"

    # Negated: `not result.passed` → FAIL
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = normalize_assertion_outcome(node.operand)
        if inner == "PASS":
            return "FAIL"

    # Compare: `result.passed is/== True/False`
    if (isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and len(node.comparators) == 1
            and isinstance(node.ops[0], (ast.Is, ast.Eq))):
        left = normalize_assertion_outcome(node.left)
        if left in ("PASS", "FAIL"):
            try:
                comparator = ast.literal_eval(node.comparators[0])
            except (ValueError, TypeError):
                return "unresolved"
            if comparator is True:
                return left                                 # `X is True` preserves polarity
            if comparator is False:
                return "FAIL" if left == "PASS" else "PASS"  # `X is False` inverts

    return "unresolved"


def validate_requirement_alignment(
    content: str,
    requirement: str,
    *,
    _tree: ast.AST | None = None,
) -> RequirementAlignmentResult:
    """Layer 2: confirm the test's asserted behavior agrees with the approved requirement (R-32f).

    Only catches unambiguous contradictions deterministically. The Critic handles subtler cases.
    Unknown requirement intent skips requirement comparison, but not internal consistency.
    """
    req_outcome = _requirement_expected_outcome(requirement)
    requirement_behavior = f"{req_outcome} expected" if req_outcome else "unknown"
    if _tree is None:
        try:
            _tree = ast.parse(content, filename="generated test")
        except SyntaxError:
            return RequirementAlignmentResult(
                aligned=True, requirement_behavior=requirement_behavior, test_behavior="unresolved"
            )

    scenarios = _test_scenarios(_tree)
    outcomes = [outcome for scenario in scenarios for outcome in scenario.recognized_outcomes]
    unique = list(dict.fromkeys(outcomes))
    for scenario in scenarios:
        scenario.test_behavior = _summary(scenario.recognized_outcomes)
        if len(set(scenario.recognized_outcomes)) > 1:
            scenario.internally_consistent = False
            scenario.contradiction_reason = (
                "Generated test contains contradictory expected outcomes: PASS and FAIL "
                f"for the same result {scenario.result_binding!r} in {scenario.test_name} "
                f"at line {scenario.source_line}. Regenerate the TEST with consistent assertions. "
                "Do NOT modify the production implementation."
            )
        # Setup evidence is local to the call, never borrowed from another function/binding.
        if "PASS" in scenario.recognized_outcomes and scenario.literal_inputs:
            tested = scenario.literal_inputs[0]
            matches = [value for value in scenario.setup_evidence if value and value in tested]
            if matches:
                scenario.internally_consistent = False
                scenario.contradiction_reason = (
                    f"Test asserts success for {scenario.result_binding!r} in {scenario.test_name} "
                    f"at line {scenario.source_line}, but its input contains configured forbidden "
                    f"value(s): {matches!r}. Regenerate the TEST."
                )
        expected = _scenario_requirement(scenario, requirement)
        if expected is not None:
            scenario.requirement_mapping_established = True
            scenario.alignment = "ALIGNED"
            if any(outcome != expected for outcome in scenario.recognized_outcomes):
                scenario.alignment = "CONTRADICTION"
                if scenario.internally_consistent:
                    scenario.contradiction_reason = (
                        f"Approved requirement implies {expected}: '{requirement.strip()}'. "
                        f"Previous generated test expressed: {', '.join(scenario.recognized_outcomes)}. "
                        f"Scenario {scenario.test_name}, result {scenario.result_binding!r}, "
                        f"line {scenario.source_line}, call {scenario.call}: these contradict. "
                        "Regenerate the TEST so it verifies the approved behavior. "
                        "Do NOT modify the production implementation."
                    )
    internal = next((s for s in scenarios if not s.internally_consistent), None)
    conflict = next((s for s in scenarios if s.alignment == "CONTRADICTION"), None)
    reason = internal or conflict
    return RequirementAlignmentResult(
        aligned=reason is None,
        alignment="CONTRADICTION" if conflict else (
            "ALIGNED" if scenarios and all(s.alignment == "ALIGNED" for s in scenarios) else "UNKNOWN"
        ),
        requirement_behavior=requirement_behavior,
        test_behavior=_summary(outcomes),
        recognized_outcomes=outcomes,
        unique_outcomes=unique,
        internally_consistent=internal is None,
        contradiction_reason=reason.contradiction_reason if reason else None,
        scenarios=scenarios,
    )


def _summary(outcomes: list[str]) -> str:
    unique = set(outcomes)
    return next(iter(unique)) if len(unique) == 1 else "MIXED" if unique else "unresolved"


def _scenario_requirement(scenario: TestScenario, requirement: str) -> str | None:
    """Small explicit grammar, not keyword similarity or a global fallback.

    A complete clause must name the supported input/setup and a supported outcome.
    Other prose (including test names), extra conditions and complex calls stay UNKNOWN.
    """
    if not scenario.literal_inputs or _requirement_expected_outcome(requirement) is None:
        return None
    tested = scenario.literal_inputs[0]
    matched: list[tuple[str, str]] = []
    outcomes = "|".join(re.escape(token) for token in (*_PASS_REQUIREMENT_TOKENS, *_FAIL_REQUIREMENT_TOKENS))
    for clause in re.split(r"[.\n;]+", requirement.lower()):
        clause = clause.strip()
        match = re.fullmatch(
            rf"(empty strings?|blank strings?|empty/blank values?|empty or blank values?|empty forbidden phrases?) ({outcomes})",
            clause,
        )
        if not match:
            continue
        subject, _ = match.groups()
        if subject.startswith("empty forbidden"):
            relevant = scenario.setup_evidence == [""]
        elif subject.startswith("empty string"):
            relevant = tested == ""
        else:
            relevant = tested.strip() == ""
        expected = _requirement_expected_outcome(clause)
        if relevant and expected:
            matched.append((clause, expected))
    if len({expected for _, expected in matched}) != 1:
        return None
    scenario.requirement_evidence = [clause for clause, _ in matched]
    return matched[0][1]


def _test_scenarios(tree: ast.AST) -> list[TestScenario]:
    """Extract straight-line local evidence; opaque control flow never supplies mapping.

    No alias, fixture, interprocedural or symbolic evaluation. Each assignment creates
    a new binding. Branches are isolated and invalidate evidence after their boundary.
    """
    scenarios: list[TestScenario] = []

    def collect(body: list[ast.stmt], scope: str, mapping: bool = True):
        values: dict[str, Any] = {}
        denials: dict[str, set[str]] = {}
        bindings: dict[str, TestScenario] = {}
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                collect(node.body, f"{scope}.{node.name}")
                values.pop(node.name, None)
                denials.pop(node.name, None)
                bindings.pop(node.name, None)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if value is None:  # A bare annotation does not rebind the result.
                    continue
                if any(isinstance(child, ast.NamedExpr) for child in ast.walk(value)):
                    values.clear()
                    denials.clear()
                    bindings.clear()
                literal = _literal(value, values) if value is not None else None
                configured = _denials_from_call(value, values) if isinstance(value, ast.Call) else set()
                receiver = _receiver_name(value) if isinstance(value, ast.Call) else None
                setup = sorted(denials.get(receiver or "", set()))
                inputs = []
                if mapping and isinstance(value, ast.Call) and len(value.args) == 1 and not value.keywords:
                    tested = _literal(value.args[0], values)
                    if isinstance(tested, str):
                        inputs = [tested]
                if any(isinstance(child, ast.Call) for child in ast.walk(value)):
                    # Calls can mutate configuration objects. Only immutable string
                    # aliases survive; setup for this call was captured above.
                    values = {name: item for name, item in values.items() if isinstance(item, str)}
                    denials.clear()
                for target in targets:
                    if not isinstance(target, ast.Name):
                        # Destructuring/attribute/subscript writes may mutate any retained evidence.
                        values.clear()
                        denials.clear()
                        bindings.clear()
                        continue
                    name = target.id
                    values.pop(name, None)
                    denials.pop(name, None)
                    if literal is not None:
                        values[name] = literal
                    if configured:
                        denials[name] = configured
                    bindings[name] = TestScenario(
                        test_name=scope, result_binding=name, source_line=node.lineno,
                        call=ast.unparse(value) if isinstance(value, ast.Call) else None,
                        literal_inputs=inputs, setup_evidence=setup if inputs and mapping else [],
                    )
            elif isinstance(node, ast.Assert):
                outcome = normalize_assertion_outcome(node.test)
                if outcome == "unresolved":
                    if any(isinstance(child, (ast.NamedExpr, ast.Call)) for child in ast.walk(node.test)):
                        values.clear()
                        denials.clear()
                    if any(isinstance(child, ast.NamedExpr) for child in ast.walk(node.test)):
                        bindings.clear()
                    continue
                attribute = next(n for n in ast.walk(node.test) if isinstance(n, ast.Attribute))
                name = attribute.value.id
                scenario = bindings.setdefault(name, TestScenario(
                    test_name=scope, result_binding=name, source_line=node.lineno,
                ))
                if not scenario.recognized_outcomes:
                    scenarios.append(scenario)
                scenario.recognized_outcomes.append(outcome)
                scenario.assertion_lines.append(node.lineno)
            elif isinstance(node, ast.Expr):
                # Logging and other calls cannot rebind a local result name. Keep
                # its assertion group while discarding potentially mutable setup.
                values.clear()
                denials.clear()
                if any(isinstance(child, ast.NamedExpr) for child in ast.walk(node)):
                    bindings.clear()
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    if name == "*":
                        values.clear()
                        denials.clear()
                        bindings.clear()
                    else:
                        values.pop(name, None)
                        denials.pop(name, None)
                        bindings.pop(name, None)
            elif not isinstance(node, ast.Pass):
                # Do not flatten mutually exclusive control paths into one scenario.
                for _, children in ast.iter_fields(node):
                    if isinstance(children, list):
                        statements = [child for child in children if isinstance(child, ast.stmt)]
                        if statements:
                            collect(statements, scope, mapping=False)
                        for child in children:
                            if isinstance(child, (ast.ExceptHandler, ast.match_case)):
                                collect(child.body, scope, mapping=False)
                values.clear()
                denials.clear()
                bindings.clear()

    collect(getattr(tree, "body", []), "<module>")
    return scenarios


# ── private helpers ──────────────────────────────────────────────────────────


def _requirement_expected_outcome(requirement: str) -> Literal["PASS", "FAIL"] | None:
    """Deterministic keyword scan of the requirement text.

    Returns PASS or FAIL only when the signal is unambiguous (one side matches,
    the other does not). Returns None when both or neither match, leaving the
    alignment decision to the Critic.
    """
    lower = requirement.lower()
    has_pass = any(token in lower for token in _PASS_REQUIREMENT_TOKENS)
    has_fail = any(token in lower for token in _FAIL_REQUIREMENT_TOKENS)
    if has_pass and not has_fail:
        return "PASS"
    if has_fail and not has_pass:
        return "FAIL"
    return None


def _literal(node: ast.AST, values: dict[str, Any]) -> Any | None:
    if isinstance(node, ast.Name):
        return values.get(node.id)
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def _strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return {item for pair in value.items() for item in pair if isinstance(item, str)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {item for item in value if isinstance(item, str)}
    return set()


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _has_marker(value: str, markers: tuple[str, ...]) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in markers)


def _denials_from_call(call: ast.Call, values: dict[str, Any]) -> set[str]:
    denied: set[str] = set()
    callable_name = _name(call.func)
    for keyword in call.keywords:
        if keyword.arg and _has_marker(keyword.arg, _DENIAL_MARKERS):
            denied.update(_strings(_literal(keyword.value, values)))
    if _has_marker(callable_name, _DENIAL_MARKERS):
        for argument in call.args:
            denied.update(_strings(_literal(argument, values)))
    return denied


def _receiver_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        return call.func.value.id
    return None

