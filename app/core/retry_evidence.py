"""Deterministic candidate identity and bounded Executor reasoning evidence."""

import hashlib
import json
import posixpath

from app.agents.execution import EditProposal
from app.agents.state import RetryAttempt
from app.core.failures import sanitize_failure_text

CONTENT_LIMIT = 8000
# Thirty steps per plan plus a bounded archive of replaced steps.
STEP_HISTORY_LIMIT = 100


def normalize_content(content: str) -> str:
    """Normalize transport line endings only; indentation/literals remain significant."""
    return content.replace('\r\n', '\n').replace('\r', '\n')


def proposal_content(action: str, target: str, proposal: EditProposal) -> str:
    return json.dumps({
        'action': action.strip().lower(),
        'target': posixpath.normpath(target),
        'blocks': [{'search': normalize_content(block.search),
                    'replace': normalize_content(block.replace)} for block in proposal.blocks],
        'full_content': (normalize_content(proposal.full_content)
                         if proposal.full_content is not None else None),
    }, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def fingerprint(content: str) -> str:
    """SHA-256 of full normalized UTF-8 content, always before preview/redaction."""
    return hashlib.sha256(normalize_content(content).encode('utf-8')).hexdigest()


def preview(content: str) -> tuple[str, bool]:
    content = sanitize_failure_text(content)
    if len(content) <= CONTENT_LIMIT:
        return content, False
    marker = '\n... [truncated] ...\n'
    head = (CONTENT_LIMIT - len(marker)) // 2
    return content[:head] + marker + content[-(CONTENT_LIMIT - len(marker) - head):], True


class DuplicateCandidate(ValueError):
    def __init__(self, previous: RetryAttempt):
        self.previous = previous
        super().__init__(
            f'Exact candidate already failed at attempt {previous.attempt_number} '
            f'({previous.failure_type}). Rejected without execution. '
            'Produce a materially different candidate grounded in the current source '
            'and approved requirement; do not repeat any failed fingerprint.'
        )


def reject_duplicate(history: list[RetryAttempt], candidate: str, *, generated_test=False) -> None:
    for previous in history:
        saved = previous.generated_test_fingerprint if generated_test else previous.candidate_fingerprint
        if saved == candidate:
            raise DuplicateCandidate(previous)
