import pytest
from app.tools.edit_applier import apply_edits, Ambiguous, NoMatch
from app.tools.edit_guard import check_edit, GuardFailure


def test_exact():
    result, report = apply_edits('a = 1\nb = 2\n', [{'search': 'a = 1', 'replace': 'a = 3'}])
    assert result == 'a = 3\nb = 2\n'
    assert report.matches[0].tier == 'exact'


def test_whitespace():
    result, report = apply_edits('def f():\n    x = 1\n    return x\n',
        [{'search': 'x = 1\nreturn x', 'replace': 'x = 2\nreturn x'}])
    assert result == 'def f():\n    x = 2\n    return x\n'
    assert report.matches[0].tier == 'whitespace'


def test_fuzzy():
    result, report = apply_edits('result = divide(left, right)\n',
        [{'search': 'result = divide(left,right)', 'replace': 'result = safe_divide(left, right)'}])
    assert 'safe_divide' in result
    assert report.matches[0].tier == 'fuzzy'


def test_no_match():
    with pytest.raises(NoMatch, match='closest lines'):
        apply_edits('abc\n', [{'search': 'totally unrelated value', 'replace': 'x'}])


@pytest.mark.parametrize('text,search', [('x = 1\nx = 1\n', 'x = 1'),
    ('    x = 1\n    return x\n    x = 1\n    return x\n', 'x = 1\nreturn x'),
    ('value = foo(a,b)\nvalue = foo(a, b)\n', 'value = foo(a ,b)')])
def test_ambiguous(text, search):
    with pytest.raises(Ambiguous):
        apply_edits(text, [{'search': search, 'replace': 'x'}])


def test_bottom_up_and_original_positions():
    result, _ = apply_edits('a = 1\nb = 2\nc = 3\n', [
        {'search': 'a = 1', 'replace': 'a = 1\na2 = 2'}, {'search': 'c = 3', 'replace': 'c = 4'}])
    assert result == 'a = 1\na2 = 2\nb = 2\nc = 4\n'


def test_overlap_rejected():
    with pytest.raises(Ambiguous, match='overlap'):
        apply_edits('abcdef', [{'search': 'abc', 'replace': 'a'}, {'search': 'bcd', 'replace': 'b'}])


def test_leading_blank_stripped():
    result, _ = apply_edits('a\nb\nc\n', [{'search': '\na\nb\nc', 'replace': '\na\nb\nd'}])
    assert result == 'a\nb\nd\n'


def test_create_only_on_empty():
    assert apply_edits('', [{'search': '', 'replace': 'x = 1\n'}])[0] == 'x = 1\n'
    with pytest.raises(NoMatch):
        apply_edits('existing', [{'search': '', 'replace': 'x'}])


@pytest.mark.parametrize('before,after', [('x=1', ''), ('x=1', 'def x('), ('x=1\n'*20, 'x=2'), ('x=1', 'x=1')])
def test_guard_rejects_empty_syntax_truncation_and_noop(before, after):
    with pytest.raises(GuardFailure):
        check_edit('app.py', before, after)


@pytest.mark.parametrize('text,search', [
    ('left\nx = 1\nright\nx = 1\n', 'x = 1'),
    ('    x = 1\n    return x\n    x = 1\n    return x\n', 'x = 1\nreturn x'),
    ('value = foo(a,b)\nvalue = foo(a, b)\n', 'value = foo(a ,b)'),
    ('aaa', 'aa'),
])
def test_ambiguity_has_deterministic_locations(text, search):
    with pytest.raises(Ambiguous) as caught:
        apply_edits(text, [{'search': search, 'replace': 'changed'}])
    evidence = caught.value.evidence
    assert evidence.search == search
    assert evidence.match_count == 2
    assert len(evidence.locations) == 2
    for location in evidence.locations:
        assert location.start_line <= location.end_line
        assert location.context in text
        assert location.similarity >= .8


def test_ambiguity_locations_are_bounded_without_hiding_count():
    with pytest.raises(Ambiguous) as caught:
        apply_edits('x\n' * 30, [{'search': 'x', 'replace': 'y'}])
    assert caught.value.evidence.match_count == 30
    assert len(caught.value.evidence.locations) == 20
    assert caught.value.evidence.locations_truncated


def test_no_match_preserves_original_preprocessed_search_and_block_index():
    search = '\nunknown\nmissing\nabsent'
    with pytest.raises(NoMatch) as caught:
        apply_edits('a\nb\nc\n', [
            {'search': 'a', 'replace': 'z'}, {'search': search, 'replace': 'new'},
        ])
    assert caught.value.evidence.block == 1
    assert caught.value.evidence.search == search
    assert caught.value.evidence.closest_source.context == 'a\nb\nc\n'


def test_model_cannot_supply_a_location_selector():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        apply_edits('x\nx\n', [{'search': 'x', 'replace': 'y', 'match_number': 2}])
