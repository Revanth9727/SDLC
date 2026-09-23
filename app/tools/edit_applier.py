"""Deterministic, fail-closed SEARCH/REPLACE matching on the original buffer."""
from difflib import SequenceMatcher
from typing import Literal
import textwrap

from pydantic import BaseModel, ConfigDict, Field


class EditBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search: str
    replace: str


class EditMatch(BaseModel):
    block: int
    start: int
    end: int
    tier: Literal["exact", "whitespace", "fuzzy", "create"]
    score: float = 1.0


class EditReport(BaseModel):
    matches: list[EditMatch]


class SourceLocation(BaseModel):
    """One-based, inclusive source lines; context is diagnostic only."""
    start_line: int
    end_line: int
    context_start_line: int
    context: str
    context_truncated: bool = False
    similarity: float = 1.0


class MatchFailureEvidence(BaseModel):
    block: int | None = None
    search: str | None = None
    match_count: int = 0
    locations: list[SourceLocation] = Field(default_factory=list)
    locations_truncated: bool = False
    closest_source: SourceLocation | None = None


class MatchFailure(ValueError):
    def __init__(self, message: str, *, evidence: MatchFailureEvidence | None = None):
        super().__init__(message)
        self.evidence = evidence or MatchFailureEvidence()


class NoMatch(MatchFailure):
    pass


class Ambiguous(MatchFailure):
    pass


def _location(text: str, start: int, end: int, score: float = 1.0) -> SourceLocation:
    first = text.count("\n", 0, start) + 1
    last = text.count("\n", 0, max(start, end - 1)) + 1
    context_start = max(1, first - 2)
    context = "".join(text.splitlines(keepends=True)[context_start - 1:last + 2])
    return SourceLocation(start_line=first, end_line=last,
                          context_start_line=context_start, context=context[:2000],
                          context_truncated=len(context) > 2000, similarity=score)


def _ambiguity(text: str, index: int, block: EditBlock, matches, message: str) -> Ambiguous:
    return Ambiguous(message, evidence=MatchFailureEvidence(
        block=index, search=block.search, match_count=len(matches),
        locations=[_location(text, start, end, score) for score, start, end in matches[:20]],
        locations_truncated=len(matches) > 20,
    ))


def _normalize(text: str) -> str:
    return "\n".join(line.rstrip() for line in textwrap.dedent(text.expandtabs(4)).strip("\r\n").splitlines())


def _replacement(search: str, replacement: str, found: str) -> str:
    """Restore the matched block's indentation for normalized/fuzzy matches."""
    if not replacement:
        return ""
    source_indent = len(search.splitlines()[0]) - len(search.splitlines()[0].lstrip())
    target_indent = len(found.splitlines()[0]) - len(found.splitlines()[0].lstrip())
    delta = target_indent - source_indent
    lines = replacement.splitlines(keepends=True)
    return "".join((" " * delta + line if delta >= 0 else line[min(-delta, len(line)-len(line.lstrip())):])
                   if line.strip() else line for line in lines)


def apply_edits(file_text: str, blocks: list[EditBlock | dict]) -> tuple[str, EditReport]:
    if not blocks:
        raise NoMatch("No edit blocks were supplied")
    resolved = []
    for index, raw in enumerate(blocks):
        block = EditBlock.model_validate(raw)
        search, replacement = block.search, block.replace
        if len(search.splitlines()) > 2 and not search.splitlines()[0].strip():
            search = "".join(search.splitlines(keepends=True)[1:])
            if replacement.startswith("\n"):
                replacement = replacement[1:]
        if not search:
            if file_text or len(blocks) != 1 or not replacement:
                raise NoMatch("Empty SEARCH is only valid for creating one empty/missing file",
                              evidence=MatchFailureEvidence(block=index, search=block.search))
            resolved.append((EditMatch(block=index, start=0, end=0, tier="create"), replacement))
            continue
        starts, offset = [], 0
        while (at := file_text.find(search, offset)) >= 0:
            starts.append(at)
            offset = at + 1
        if len(starts) > 1:
            raise _ambiguity(file_text, index, block,
                             [(1.0, at, at + len(search)) for at in starts],
                             "SEARCH matches multiple locations; provide more context")
        if starts:
            match = EditMatch(block=index, start=starts[0], end=starts[0]+len(search), tier="exact")
        else:
            lines = file_text.splitlines(keepends=True)
            count = len(search.splitlines())
            offsets = [0]
            for line in lines:
                offsets.append(offsets[-1] + len(line))
            candidates = []
            closest = (0.0, "")
            closest_source = None
            for start in range(max(0, len(lines)-count+1)):
                found = "".join(lines[start:start+count])
                # Preserve whether SEARCH includes a trailing newline.
                end = offsets[start+count]
                if not search.endswith("\n"):
                    found = found.rstrip("\r\n")
                    end = offsets[start] + len(found)
                normalized = _normalize(found)
                score = SequenceMatcher(None, _normalize(search), normalized, autojunk=False).ratio()
                if closest_source is None or score > closest[0]:
                    closest = score, found
                    closest_source = _location(file_text, offsets[start], end, score)
                if score >= 0.8:
                    candidates.append((score, offsets[start], end, found))
            exact_normalized = [item for item in candidates if item[0] == 1.0]
            matches = exact_normalized or candidates
            if not matches:
                raise NoMatch(f"SEARCH not found; closest lines (score {closest[0]:.2f}):\n{closest[1][:2000]}",
                              evidence=MatchFailureEvidence(block=index, search=block.search,
                                                            closest_source=closest_source))
            if len(matches) != 1:
                raise _ambiguity(file_text, index, block,
                                 [(score, start, end) for score, start, end, _ in matches],
                                 "SEARCH has multiple normalized/fuzzy matches; provide more context")
            score, start, end, found = matches[0]
            match = EditMatch(block=index, start=start, end=end,
                              tier="whitespace" if exact_normalized else "fuzzy", score=score)
            replacement = _replacement(search, replacement, found)
        resolved.append((match, replacement))
    ordered = sorted(resolved, key=lambda item: item[0].start)
    for (left, _), (right, _) in zip(ordered, ordered[1:]):
        if left.end > right.start:
            raise _ambiguity(file_text, right.block, EditBlock.model_validate(blocks[right.block]),
                             [(m.score, m.start, m.end) for m in (left, right)],
                             "Edit blocks overlap on the original file; combine them")
    new_text = file_text
    for match, replacement in reversed(ordered):
        new_text = new_text[:match.start] + replacement + new_text[match.end:]
    return new_text, EditReport(matches=[match for match, _ in resolved])
