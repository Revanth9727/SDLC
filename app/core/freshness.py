"""Deterministic target-file freshness guard before execution (R-20)."""
from app.agents.state import SubtaskState
from app.tools.repo_tool import RepoTool


def check_freshness(state: SubtaskState, repo_tool: RepoTool | None = None) -> SubtaskState:
    if state.current_step or not state.freshness_recorded:
        return state
    targets = list(dict.fromkeys(step.target_file for step in state.plan))
    unrecorded = [path for path in targets if path not in state.diagnosed_file_hashes]
    if unrecorded:
        state.status = 'needs_human'
        state.failure_reason = (
            "Freshness cannot be proven for plan target(s): " + ", ".join(unrecorded) +
            ". Re-run diagnosis and approve a fresh plan."
        )
        return state
    current_commit, drifted = (repo_tool or RepoTool()).verify_freshness(
        state.repo, state.subtask_id,
        {path: state.diagnosed_file_hashes[path] for path in targets},
    )
    if drifted:
        state.status = 'needs_human'
        state.failure_reason = (
            "Repository target files changed since diagnosis: " + ", ".join(drifted) +
            ". Re-run diagnosis and approve a fresh plan."
        )
    else:
        state.base_commit = current_commit
    return state
