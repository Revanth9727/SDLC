"""Regression tests for R-32e (assertion semantics) and R-32f (requirement alignment).

Tests 1-7 are pure unit tests of the two-layer validator.
Tests 8-9 are executor integration tests (in test_execution.py).
Tests 10-11 are classification and regression assertions.
"""
from __future__ import annotations

import ast

import pytest

from app.tools.test_validity_validator import (
    InvalidGeneratedTestError,
    RequirementAlignmentResult,
    RequirementContradictionError,
    normalize_assertion_outcome,
    validate_requirement_alignment,
    validate_test_validity,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _expr(source: str) -> ast.AST:
    """Parse a single expression and return its AST node."""
    return ast.parse(source, mode="eval").body


# ── Test 4: assert result.passed → PASS ──────────────────────────────────────

def test_normalize_direct_passed_is_pass():
    assert normalize_assertion_outcome(_expr("result.passed")) == "PASS"


def test_normalize_passed_is_true_is_pass():
    assert normalize_assertion_outcome(_expr("result.passed is True")) == "PASS"


def test_normalize_passed_eq_true_is_pass():
    assert normalize_assertion_outcome(_expr("result.passed == True")) == "PASS"


# ── Test 3: assert not result.passed → FAIL ──────────────────────────────────

def test_normalize_not_passed_is_fail():
    assert normalize_assertion_outcome(_expr("not result.passed")) == "FAIL"


# ── Test 5: assert result.passed is False → FAIL ─────────────────────────────

def test_normalize_passed_is_false_is_fail():
    assert normalize_assertion_outcome(_expr("result.passed is False")) == "FAIL"


# ── Test 6: assert result.passed == False → FAIL ─────────────────────────────

def test_normalize_passed_eq_false_is_fail():
    assert normalize_assertion_outcome(_expr("result.passed == False")) == "FAIL"


# ── Test 7: equivalent success fields ────────────────────────────────────────

@pytest.mark.parametrize("field", ["passed", "allowed", "valid", "success", "ok"])
def test_normalize_success_field_direct_is_pass(field):
    assert normalize_assertion_outcome(_expr(f"result.{field}")) == "PASS"


@pytest.mark.parametrize("field", ["passed", "allowed", "valid", "success", "ok"])
def test_normalize_success_field_negated_is_fail(field):
    assert normalize_assertion_outcome(_expr(f"not result.{field}")) == "FAIL"


@pytest.mark.parametrize("field", ["passed", "allowed", "valid", "success", "ok"])
def test_normalize_success_field_is_false_is_fail(field):
    assert normalize_assertion_outcome(_expr(f"result.{field} is False")) == "FAIL"


@pytest.mark.parametrize("field", ["passed", "allowed", "valid", "success", "ok"])
def test_normalize_success_field_eq_false_is_fail(field):
    assert normalize_assertion_outcome(_expr(f"result.{field} == False")) == "FAIL"


def test_normalize_unrelated_expression_is_unresolved():
    assert normalize_assertion_outcome(_expr("len(items) == 3")) == "unresolved"


def test_normalize_plain_name_is_unresolved():
    assert normalize_assertion_outcome(_expr("result")) == "unresolved"


# ── Test 1: requirement says PASS; test asserts FAIL → rejected ───────────────

def test_validate_test_validity_rejects_fail_assertion_for_ignore_requirement():
    """The documented failure case: requirement says 'ignored' but test asserts not passed."""
    content = (
        "def test_empty_phrase():\n"
        "    checker = Check(forbidden=[''])\n"
        "    result = checker.check('Hello world')\n"
        "    assert not result.passed\n"
    )
    with pytest.raises(RequirementContradictionError) as exc_info:
        validate_test_validity(content, "Empty forbidden phrases should be ignored")
    msg = str(exc_info.value)
    assert "PASS" in msg
    assert "FAIL" in msg
    assert "Regenerate" in msg


# ── Test 2: same requirement; test asserts PASS → accepted ───────────────────

def test_validate_test_validity_accepts_pass_assertion_for_ignore_requirement():
    content = (
        "def test_empty_phrase():\n"
        "    checker = Check(forbidden=[''])\n"
        "    result = checker.check('Hello world')\n"
        "    assert result.passed\n"
    )
    result = validate_test_validity(content, "Empty forbidden phrases should be ignored")
    assert result.status == "consistent"
    assert result.alignment is not None
    assert result.alignment.aligned
    assert result.alignment.test_behavior == "PASS"
    assert result.alignment.requirement_behavior == "PASS expected"


# ── validate_requirement_alignment structured result ─────────────────────────

def test_validate_requirement_alignment_structured_contradiction():
    content = (
        "def test_empty_phrase():\n"
        "    checker = Check(forbidden=[''])\n"
        "    result = checker.check('Hello world')\n"
        "    assert not result.passed\n"
    )
    result = validate_requirement_alignment(content, "Empty forbidden phrases should be ignored")
    assert isinstance(result, RequirementAlignmentResult)
    assert not result.aligned
    assert result.requirement_behavior == "PASS expected"
    assert result.test_behavior == "FAIL"
    assert result.contradiction_reason is not None
    assert "Approved requirement implies PASS" in result.contradiction_reason
    assert "Previous generated test expressed: FAIL" in result.contradiction_reason
    assert "Regenerate the TEST" in result.contradiction_reason
    assert "Do NOT modify the production implementation" in result.contradiction_reason


def test_validate_requirement_alignment_unknown_requirement_returns_aligned():
    """No PASS/FAIL keyword signal in requirement → skip alignment check."""
    content = (
        "def test_foo():\n"
        "    result = something()\n"
        "    assert not result.passed\n"
    )
    result = validate_requirement_alignment(content, "Guard zero denominator")
    assert result.aligned
    assert result.requirement_behavior == "unknown"


def test_validate_requirement_alignment_no_recognizable_assertion_returns_aligned():
    """Test with no normalizable assertion → aligned (Critic handles)."""
    content = "def test_foo():\n    assert len(items) == 3\n"
    result = validate_requirement_alignment(content, "Empty forbidden phrases should be ignored")
    assert result.aligned
    assert result.test_behavior == "unresolved"


def test_validate_requirement_alignment_syntax_error_returns_aligned():
    result = validate_requirement_alignment("not python !!!@#", "Empty phrases should be ignored")
    assert result.aligned


# ── Test 11: Layer 1 regression — internal contradiction still fires ──────────

def test_layer1_still_rejects_pass_assertion_for_forbidden_input():
    """Layer 1 must reject a test asserting PASS when the input contains a forbidden value."""
    content = (
        "class Check:\n"
        "    def __init__(self, forbidden): self.forbidden = forbidden\n"
        "    def check(self, text): return type('Result', (), {'passed': False})()\n"
        "checker = Check(forbidden=['bad'])\n"
        "result = checker.check('contains bad')\n"
        "assert result.passed\n"
    )
    with pytest.raises(InvalidGeneratedTestError, match="configured forbidden"):
        validate_test_validity(content, "Configured forbidden phrases must fail")


def test_layer1_fires_before_layer2_when_both_would_reject():
    """When both layers would reject, Layer 1 takes priority."""
    content = (
        "class Check:\n"
        "    def __init__(self, forbidden): self.forbidden = forbidden\n"
        "    def check(self, text): return type('Result', (), {'passed': False})()\n"
        "checker = Check(forbidden=['bad'])\n"
        "result = checker.check('contains bad')\n"
        "assert result.passed\n"
    )
    with pytest.raises(InvalidGeneratedTestError):
        validate_test_validity(content, "Forbidden content must be rejected")


def test_layer2_does_not_fire_for_ambiguous_requirement():
    """Both PASS and FAIL tokens match → ambiguous → no alignment check → accepted."""
    content = (
        "def test_foo():\n"
        "    result = checker.check('x')\n"
        "    assert not result.passed\n"
    )
    # No bare-word tokens; neither multi-word phrase fires either → no signal → None
    result = validate_test_validity(content, "Empty phrases are ignored but bad words are rejected")
    assert result.status == "consistent"
    assert result.alignment is not None
    assert result.alignment.aligned


# ── Test 10: infrastructure vs reasoning classification ───────────────────────

def test_classify_requirement_contradiction_as_reasoning():
    from app.agents.executor import _classify_step_exception
    exc = RequirementContradictionError("contradiction")
    classification, _ = _classify_step_exception("validate_test_semantics", exc)
    assert classification == "reasoning"


def test_classify_invalid_generated_test_as_reasoning():
    from app.agents.executor import _classify_step_exception
    exc = InvalidGeneratedTestError("bad test")
    classification, _ = _classify_step_exception("validate_test_semantics", exc)
    assert classification == "reasoning"


def test_classify_runtime_error_from_validator_as_infrastructure():
    """An unexpected crash inside the validator is infrastructure — zero reasoning retries."""
    from app.agents.executor import _classify_step_exception
    classification, _ = _classify_step_exception("validate_test_semantics", RuntimeError("crash"))
    assert classification == "infrastructure"


def test_classify_os_error_from_validator_as_infrastructure():
    from app.agents.executor import _classify_step_exception
    classification, _ = _classify_step_exception("validate_test_semantics", OSError("disk"))
    assert classification == "infrastructure"


# ── Phase-1 safety fix: bare-token negation-inversion regression tests ────────

_FAIL_CONTENT = (
    "def test_foo():\n"
    "    result = checker.check('x')\n"
    "    assert not result.passed\n"
)

_PASS_CONTENT = (
    "def test_foo():\n"
    "    result = checker.check('x')\n"
    "    assert result.passed\n"
)


def test_should_not_be_ignored_returns_no_signal():
    """Bare 'ignored' removed: 'should not be ignored' must not infer PASS (R-32f)."""
    result = validate_requirement_alignment(_FAIL_CONTENT, "Empty phrases should not be ignored.")
    assert result.aligned
    assert result.requirement_behavior == "unknown"


def test_should_not_be_rejected_returns_no_signal():
    """Bare 'rejected' removed: 'should not be rejected' must not infer FAIL (R-32f)."""
    result = validate_requirement_alignment(_PASS_CONTENT, "Empty phrases should not be rejected.")
    assert result.aligned
    assert result.requirement_behavior == "unknown"


def test_do_not_ignore_returns_no_signal():
    """Bare 'ignore' removed: 'do not ignore' must not infer PASS."""
    result = validate_requirement_alignment(_FAIL_CONTENT, "Do not ignore empty phrases.")
    assert result.aligned
    assert result.requirement_behavior == "unknown"


def test_do_not_reject_returns_no_signal():
    """'do not reject' must not infer FAIL."""
    result = validate_requirement_alignment(_FAIL_CONTENT, "Do not reject empty phrases.")
    assert result.aligned
    assert result.requirement_behavior == "unknown"


def test_safe_pass_phrases_still_produce_pass_signal():
    """Safe multi-word PASS phrases remain effective after bare-token removal."""
    for req in (
        "Empty phrases should be ignored.",
        "Empty phrases should be allowed.",
        "Empty phrases should not fail.",
        "The check should pass for empty phrases.",
        "The check should have no effect for empty phrases.",
    ):
        result = validate_requirement_alignment(_PASS_CONTENT, req)
        assert result.requirement_behavior == "PASS expected", f"expected PASS signal for: {req!r}"


def test_safe_fail_phrases_still_produce_fail_signal():
    """Safe multi-word FAIL phrases remain effective after bare-token removal."""
    for req in (
        "Empty phrases should fail.",
        "Empty phrases must fail.",
        "Empty phrases should be rejected.",
        "Empty phrases should be blocked.",
        "Empty phrases should be denied.",
    ):
        result = validate_requirement_alignment(_FAIL_CONTENT, req)
        assert result.requirement_behavior == "FAIL expected", f"expected FAIL signal for: {req!r}"


def test_must_be_ignored_returns_no_signal():
    """'must be ignored' is not in the safe phrase set; None is the correct conservative result."""
    result = validate_requirement_alignment(_FAIL_CONTENT, "Empty phrases must be ignored.")
    assert result.aligned
    assert result.requirement_behavior == "unknown"


def test_ambiguous_both_pass_and_fail_phrases_returns_no_signal():
    """When both multi-word PASS and FAIL phrases appear, returns None (conservative)."""
    result = validate_requirement_alignment(
        _FAIL_CONTENT,
        "Empty phrases should pass, non-empty forbidden phrases should fail.",
    )
    assert result.aligned
    assert result.requirement_behavior == "unknown"


def test_negated_requirement_no_longer_blocks_valid_fail_test():
    """End-to-end: 'should not be ignored' (None signal) no longer raises RequirementContradictionError."""
    content = (
        "def test_empty_phrase():\n"
        "    checker = Check(forbidden=[''])\n"
        "    result = checker.check('Hello world')\n"
        "    assert not result.passed\n"
    )
    result = validate_test_validity(content, "Empty phrases should not be ignored.")
    assert result.status == "consistent"
    assert result.alignment is not None
    assert result.alignment.aligned
    assert result.alignment.requirement_behavior == "unknown"


@pytest.mark.parametrize('requirement,assertions,outcomes,invalid', [
    ('Empty strings should pass.', ['result.passed', 'not result.passed'], ['PASS', 'FAIL'], True),
    ('Empty strings should fail.', ['not result.passed', 'result.passed'], ['FAIL', 'PASS'], True),
    ('Empty strings should pass.', ['result.passed', 'result.allowed is True'], ['PASS', 'PASS'], False),
    ('Empty strings should fail.', ['not result.passed', 'result.allowed is False'], ['FAIL', 'FAIL'], False),
    ('Handle empty strings.', ['result.passed', 'not result.passed'], ['PASS', 'FAIL'], True),
    ('Handle empty strings.', ['result.passed'], ['PASS'], False),
    ('Empty strings should pass.', ['result.passed', 'result.message == "ok"', 'len(result.reasons) == 0'], ['PASS'], False),
    ('Empty strings should fail.', ['not result.passed', 'result.message == "bad"'], ['FAIL'], False),
])
def test_all_recognized_assertions(requirement, assertions, outcomes, invalid):
    content = 'def test_empty():\n    result = check("")\n'
    content += ''.join(f'    assert {expression}\n' for expression in assertions)
    alignment = validate_requirement_alignment(content, requirement)
    assert alignment.recognized_outcomes == outcomes
    assert alignment.unique_outcomes == list(dict.fromkeys(outcomes))
    assert alignment.internally_consistent is (not invalid)
    assert alignment.aligned is (not invalid)
    assert alignment.test_behavior == ('MIXED' if invalid else outcomes[0])
    assert RequirementAlignmentResult.model_validate_json(alignment.model_dump_json()) == alignment
    if invalid:
        assert 'contradictory expected outcomes: PASS and FAIL' in alignment.contradiction_reason
        with pytest.raises(InvalidGeneratedTestError, match='contradictory expected outcomes'):
            validate_test_validity(content, requirement)
    else:
        assert validate_test_validity(content, requirement).status == 'consistent'


@pytest.mark.parametrize('content', [
    'def test_a():\n    assert result.passed\ndef test_b():\n    assert not result.passed\n',
    'def test_a():\n    assert good.passed\n    assert not bad.passed\n',
    'def test_a():\n    result = check(1)\n    assert result.passed\n    result = check(2)\n    assert not result.passed\n',
])
def test_independent_results_are_not_internally_contradictory(content):
    result = validate_test_validity(content, 'Handle both cases.')
    assert result.alignment.internally_consistent
    assert result.alignment.test_behavior == 'MIXED'
    # Unsupported mapping stays unknown; no global requirement fallback.
    alignment = validate_requirement_alignment(content, 'Inputs should pass.')
    assert alignment.aligned
    assert alignment.alignment == 'UNKNOWN'
    assert validate_test_validity(content, 'Inputs should pass.').status == 'consistent'


def _scenario(name, value, outcome):
    assertion = 'result.passed' if outcome == 'PASS' else 'not result.passed'
    return f'def test_{name}():\n    result = check({value!r})\n    assert {assertion}\n'


@pytest.mark.parametrize('reverse', [False, True])
def test_empty_allowed_preserves_unrelated_forbidden_regression(reverse):
    parts = [_scenario('empty', '', 'PASS'), _scenario('forbidden', 'forbidden', 'FAIL')]
    content = ''.join(reversed(parts) if reverse else parts)
    alignment = validate_test_validity(content, 'Empty strings should be allowed.').alignment
    assert alignment.aligned and alignment.internally_consistent
    assert alignment.test_behavior == 'MIXED'
    by_input = {s.literal_inputs[0]: s for s in alignment.scenarios}
    assert by_input[''].alignment == 'ALIGNED'
    assert by_input[''].requirement_mapping_established
    assert by_input['forbidden'].alignment == 'UNKNOWN'
    assert not by_input['forbidden'].requirement_mapping_established
    assert by_input[''].call == "check('')"
    assert by_input[''].source_line > 0 and by_input[''].assertion_lines
    assert RequirementAlignmentResult.model_validate_json(alignment.model_dump_json()) == alignment


@pytest.mark.parametrize('second_outcome', ['PASS', 'FAIL'])
def test_all_mapped_empty_and_blank_scenarios_are_checked(second_outcome):
    content = _scenario('empty', '', 'PASS') + _scenario('blank', ' ', second_outcome)
    requirement = 'Empty/blank values should be allowed.'
    alignment = validate_requirement_alignment(content, requirement)
    assert all(s.requirement_mapping_established for s in alignment.scenarios)
    assert alignment.aligned is (second_outcome == 'PASS')
    if second_outcome == 'FAIL':
        with pytest.raises(RequirementContradictionError, match='test_blank'):
            validate_test_validity(content, requirement)
    else:
        assert validate_test_validity(content, requirement).status == 'consistent'


@pytest.mark.parametrize('reverse', [False, True])
def test_relevant_empty_failure_is_rejected_among_other_scenarios(reverse):
    parts = [_scenario('empty', '', 'FAIL'), _scenario('forbidden', 'forbidden', 'FAIL')]
    with pytest.raises(RequirementContradictionError, match='test_empty'):
        validate_test_validity(''.join(reversed(parts) if reverse else parts), 'Empty strings should pass.')


@pytest.mark.parametrize('prefix', [
    'result = check(dynamic)',
    'result = check("forbidden")',
    'result = check("", dynamic)',
    'result = check("", mode=dynamic)',
    'result = check(*args)',
    'result = check(**kwargs)',
    'value = ""\n    value = dynamic\n    result = check(value)',
])
def test_no_name_or_global_fallback_for_unknown_mapping(prefix):
    content = f'def test_empty_string_should_pass():\n    {prefix}\n    assert not result.passed\n'
    alignment = validate_test_validity(content, 'Empty strings should pass.').alignment
    assert alignment.alignment == 'UNKNOWN'
    assert all(s.alignment == 'UNKNOWN' for s in alignment.scenarios)
    assert not alignment.contradiction_reason


@pytest.mark.parametrize('requirement', [
    'Values should pass.',
    'Empty strings should pass when mode is enabled.',
    'Non-empty strings should pass.',
    'It is false that empty strings should pass.',
    'Empty strings should not be rejected.',
    'Empty strings should pass. Other values should fail.',
])
def test_unsupported_or_ambiguous_requirement_mapping_is_unknown(requirement):
    result = validate_test_validity(_scenario('empty', '', 'FAIL'), requirement)
    assert result.alignment.alignment == 'UNKNOWN'


def test_unknown_mapping_still_detects_same_result_conflict():
    content = 'def test_x():\n    result = check(dynamic)\n    assert result.passed\n    assert not result.passed\n'
    alignment = validate_requirement_alignment(content, 'Empty strings should pass.')
    assert alignment.alignment == 'UNKNOWN'
    assert not alignment.scenarios[0].internally_consistent
    with pytest.raises(InvalidGeneratedTestError):
        validate_test_validity(content, 'Empty strings should pass.')


@pytest.mark.parametrize('binding', ['forbidden_result', 'result'])
def test_distinct_results_and_reassignment_in_one_function(binding):
    content = ('def test_both():\n    result = check("")\n    assert result.passed\n'
               f'    {binding} = check("forbidden")\n    assert not {binding}.passed\n')
    result = validate_test_validity(content, 'Empty strings should pass.')
    assert len(result.alignment.scenarios) == 2
    assert [s.alignment for s in result.alignment.scenarios] == ['ALIGNED', 'UNKNOWN']


def test_literal_name_and_annotated_assignment_are_evidence():
    content = 'def test_empty():\n    value = ""\n    result: Result = check(value)\n    assert not result.passed\n'
    with pytest.raises(RequirementContradictionError):
        validate_test_validity(content, 'Empty strings should pass.')


def test_multiple_bindings_keep_their_own_inputs_when_assertions_are_interleaved():
    content = ('def test_both():\n    empty = check("")\n    forbidden = check("forbidden")\n'
               '    assert empty.passed\n    assert not forbidden.passed\n    assert empty.allowed\n')
    result = validate_test_validity(content, 'Empty strings should pass.')
    assert [s.recognized_outcomes for s in result.alignment.scenarios] == [['PASS', 'PASS'], ['FAIL']]


def test_setup_is_not_borrowed_across_functions_or_later_assignments():
    content = ('def test_allowed():\n    checker = Check(forbidden=["bad"])\n'
               '    result = checker.check("good")\n    assert result.passed\n'
               'def test_denied():\n    checker = Check(forbidden=["bad"])\n'
               '    result = checker.check("bad")\n    assert not result.passed\n')
    assert validate_test_validity(content, 'Empty strings should pass.').status == 'consistent'
    # Rebinding the receiver later must not retroactively change the earlier result's setup.
    content = ('checker = Check(forbidden=["other"])\nresult = checker.check("bad")\n'
               'checker = Check(forbidden=["bad"])\nassert result.passed\n')
    assert validate_test_validity(content, 'Handle forbidden values.').status == 'consistent'


def test_empty_config_mapping_requires_explicit_setup():
    requirement = 'Empty forbidden phrases should be ignored.'
    unrelated = 'def test_empty():\n    result = checker.check("Hello")\n    assert not result.passed\n'
    assert validate_test_validity(unrelated, requirement).alignment.alignment == 'UNKNOWN'
    mapped = unrelated.replace('    result =', '    checker = Check(forbidden=[""])\n    result =')
    with pytest.raises(RequirementContradictionError):
        validate_test_validity(mapped, requirement)


def test_branch_paths_are_not_flattened_and_mapping_is_unknown():
    content = ('def test_conditional():\n    result = check(value)\n    if condition:\n'
               '        assert result.passed\n    else:\n        assert not result.passed\n')
    alignment = validate_test_validity(content, 'Empty strings should pass.').alignment
    assert alignment.internally_consistent and alignment.alignment == 'UNKNOWN'


@pytest.mark.parametrize('intervening', [
    'from fixtures import value',
    'assert (value := "forbidden")',
    'ignored = consume(value := "forbidden")',
])
def test_unknown_rebinding_invalidates_literal_evidence(intervening):
    content = ('def test_empty():\n    value = ""\n'
               f'    {intervening}\n    result = check(value)\n    assert not result.passed\n')
    alignment = validate_test_validity(content, 'Empty strings should pass.').alignment
    assert alignment.alignment == 'UNKNOWN'


def test_opaque_call_invalidates_mutable_setup_evidence():
    content = ('def test_empty():\n    checker = Check(forbidden=[""])\n'
               '    ignored = configure(checker)\n    result = checker.check("hello")\n'
               '    assert not result.passed\n')
    alignment = validate_test_validity(content, 'Empty forbidden phrases should be ignored.').alignment
    assert alignment.alignment == 'UNKNOWN'


def test_annotation_without_assignment_does_not_hide_internal_conflict():
    content = ('def test_empty():\n    result = check("")\n    assert result.passed\n'
               '    result: Result\n    assert not result.passed\n')
    with pytest.raises(InvalidGeneratedTestError):
        validate_test_validity(content, 'Empty strings should pass.')


@pytest.mark.parametrize('intervening', ['assert len(result.reasons) == 0', 'print("debug")'])
def test_unrelated_statements_do_not_hide_same_result_contradiction(intervening):
    content = ('def test_empty():\n    result = check("")\n    assert result.passed\n'
               f'    {intervening}\n    assert not result.passed\n')
    with pytest.raises(InvalidGeneratedTestError):
        validate_test_validity(content, 'Empty strings should pass.')
