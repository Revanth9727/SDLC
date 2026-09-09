"""Validate the candidate buffer before a deterministic tool writes it."""

class GuardFailure(ValueError):
    pass


def check_edit(path: str, before: str, after: str) -> None:
    if not after.strip():
        raise GuardFailure("Edit would empty the file")
    if before == after:
        raise GuardFailure("Edit makes no change")
    old_lines, new_lines = len(before.splitlines()), len(after.splitlines())
    if old_lines >= 8 and new_lines < old_lines * 0.5:
        raise GuardFailure("Edit would truncate more than half the file")
    if new_lines > max(old_lines * 3, old_lines + 100):
        raise GuardFailure("Unexpected line-count growth")
    if '\x00' in after:
        raise GuardFailure("NUL bytes are not valid text edits")
    if path.endswith('.py'):
        try:
            compile(after, path, 'exec')
        except (SyntaxError, ValueError) as exc:
            raise GuardFailure(f"Python syntax check failed: {exc}") from exc
