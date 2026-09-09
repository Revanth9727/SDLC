"""First reasoning agent: diagnose one isolated subtask."""

from __future__ import annotations

import logging
from typing import Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.agents.llm import LLMClient
from app.agents.router import model_tier
from app.agents.state import BudgetUsed, SubtaskState
from app.tools.repo_tool import RepoTool

logger = logging.getLogger(__name__)


class Diagnosis(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    root_cause: str
    files: list[str] = Field(default_factory=list)
    reasoning: str
    no_root_cause: bool = Field(False, alias="NoRootCause")


class DiagnosisAgent:
    """Reads repo files and asks the LLM for a structured root-cause diagnosis."""

    def __init__(
        self,
        llm: LLMClient | None = None,
        repo_tool: RepoTool | None = None,
        max_files: int = 8,
        max_chars_per_file: int = 6000,
    ) -> None:
        self.llm = llm or LLMClient()
        self.repo_tool = repo_tool or RepoTool()
        self.max_files = max_files
        self.max_chars_per_file = max_chars_per_file

    def run(self, state: SubtaskState) -> SubtaskState:
        """Run diagnosis and return the updated blackboard state."""
        files = self.repo_tool.list_files(state.repo, state.subtask_id)
        selected = self._shortlist_files(files, state.description)
        snippets = self._read_snippets(state.repo, state.subtask_id, selected)

        diagnosis = self.llm.complete_json(
            _SYSTEM_PROMPT,
            self._user_prompt(state, snippets),
            Diagnosis,
            tier=model_tier("diagnosis"),
            ticket_id=state.ticket_id,
        )

        state.diagnosis = diagnosis.model_dump(by_alias=True)
        state.budget_used = BudgetUsed.model_validate(self.llm.get_usage(state.ticket_id))
        if diagnosis.no_root_cause or not diagnosis.root_cause.strip():
            state.status = "needs_human"
            state.failure_reason = "NoRootCause"
        return state

    def _shortlist_files(self, files: Sequence[str], description: str) -> list[str]:
        lowered = description.lower()
        preferred_names = (
            "app.py",
            "main.py",
            "server.py",
            "routes.py",
            "views.py",
            "models.py",
            "utils.py",
        )
        allowed_suffixes = (".py", ".js", ".ts", ".tsx", ".jsx", ".md", ".txt")

        scored: list[tuple[int, str]] = []
        for path in files:
            lower = path.lower()
            if not lower.endswith(allowed_suffixes):
                continue
            score = 0
            if any(lower.endswith(name) for name in preferred_names):
                score += 20
            for token in _keywords(lowered):
                if token in lower:
                    score += 10
            if "test" in lower:
                score += 2
            scored.append((score, path))

        scored.sort(key=lambda item: (-item[0], item[1]))
        selected = [path for _score, path in scored[: self.max_files]]
        logger.info("diagnosis.shortlist repo_files=%d selected=%r", len(files), selected)
        return selected

    def _read_snippets(self, repo: str, subtask_id: str, files: Sequence[str]) -> list[dict[str, str]]:
        snippets: list[dict[str, str]] = []
        for path in files:
            try:
                text = self.repo_tool.read_file(repo, subtask_id, path)
            except Exception as exc:
                logger.warning("diagnosis.read_file failed repo=%r path=%r: %s", repo, path, exc)
                continue
            snippets.append({"path": path, "content": text[: self.max_chars_per_file]})
        return snippets

    def _user_prompt(self, state: SubtaskState, snippets: list[dict[str, str]]) -> str:
        rendered = "\n\n".join(
            f"FILE: {item['path']}\n```\n{item['content']}\n```"
            for item in snippets
        )
        return (
            f"Ticket id: {state.ticket_id}\n"
            f"Subtask id: {state.subtask_id}\n"
            f"Subtask type: {state.subtask_type}\n"
            f"Repo: {state.repo}\n"
            f"Description:\n{state.description}\n\n"
            "Relevant files:\n"
            f"{rendered or '(no readable files found)'}\n\n"
            "Find the likely root cause. If the provided files are insufficient, "
            "return root_cause as an empty string and NoRootCause as true."
        )


def _keywords(text: str) -> set[str]:
    stop = {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "when",
        "into",
        "from",
        "ticket",
        "error",
        "bug",
    }
    return {token for token in text.replace("-", " ").replace("_", " ").split() if len(token) > 3 and token not in stop}


_SYSTEM_PROMPT = """You are the Diagnosis agent for one isolated subtask.
Use only the ticket description and provided file snippets. Return JSON with:
root_cause, files, reasoning, NoRootCause.
Be specific about the failing code path. Do not propose edits yet.
"""
