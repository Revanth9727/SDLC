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


class NoMatch(ValueError):
    pass


class Ambiguous(ValueError):
    pass


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
                raise NoMatch("Empty SEARCH is only valid for creating one empty/missing file")
            resolved.append((EditMatch(block=index, start=0, end=0, tier="create"), replacement))
            continue
        starts, offset = [], 0
        while (at := file_text.find(search, offset)) >= 0:
            starts.append(at)
            offset = at + 1
        if len(starts) > 1:
            raise Ambiguous("SEARCH matches multiple locations; provide more context")
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
            for start in range(max(0, len(lines)-count+1)):
                found = "".join(lines[start:start+count])
                # Preserve whether SEARCH includes a trailing newline.
                end = offsets[start+count]
                if not search.endswith("\n"):
                    found = found.rstrip("\r\n")
                    end = offsets[start] + len(found)
                normalized = _normalize(found)
                score = SequenceMatcher(None, _normalize(search), normalized, autojunk=False).ratio()
                if score > closest[0]:
                    closest = score, found
                if score >= 0.8:
                    candidates.append((score, offsets[start], end, found))
            exact_normalized = [item for item in candidates if item[0] == 1.0]
            matches = exact_normalized or candidates
            if not matches:
                raise NoMatch(f"SEARCH not found; closest lines (score {closest[0]:.2f}):\n{closest[1][:2000]}")
            if len(matches) != 1:
                raise Ambiguous("SEARCH has multiple normalized/fuzzy matches; provide more context")
            score, start, end, found = matches[0]
            match = EditMatch(block=index, start=start, end=end,
                              tier="whitespace" if exact_normalized else "fuzzy", score=score)
            replacement = _replacement(search, replacement, found)
        resolved.append((match, replacement))
    ordered = sorted(resolved, key=lambda item: item[0].start)
    for (left, _), (right, _) in zip(ordered, ordered[1:]):
        if left.end > right.start:
            raise Ambiguous("Edit blocks overlap on the original file; combine them")
    new_text = file_text
    for match, replacement in reversed(ordered):
        new_text = new_text[:match.start] + replacement + new_text[match.end:]
    return new_text, EditReport(matches=[match for match, _ in resolved])
