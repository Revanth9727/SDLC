"""First reasoning agent: diagnose one isolated subtask."""

from __future__ import annotations


from pydantic import BaseModel, ConfigDict, Field

from app.agents.llm import LLMClient
from app.agents.router import model_tier
from app.agents.state import BudgetUsed, SubtaskState
from app.tools.repo_tool import RepoTool
from app.tools.code_search import CodeSearchTool, ToolEventSink

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
        event_sink: ToolEventSink | None = None,
        search_tool: CodeSearchTool | None = None,
    ) -> None:
        self.llm = llm or LLMClient()
        self.repo_tool = repo_tool or RepoTool()
        self.max_files = max_files
        self.max_chars_per_file = max_chars_per_file
        self.search = search_tool or CodeSearchTool(self.repo_tool, event_sink=event_sink)

    def run(self, state: SubtaskState) -> SubtaskState:
        """Run diagnosis and return the updated blackboard state."""
        self.repo_tool.clone_or_pull(state.repo, state.subtask_id)
        context = state.code_context
        if not context or not context.get("verified_evidence"):
            state.status = "needs_human"
            state.failure_reason = "Diagnosis blocked: Code-Intelligence supplied no verified source evidence"
            return state
        state.base_commit = context.get("commit_sha") or self.repo_tool.revision(state.repo, state.subtask_id)
        snippets = self._context_snippets(state)

        diagnosis = self.llm.complete_json(
            _SYSTEM_PROMPT,
            self._user_prompt(state, snippets),
            Diagnosis,
            tier=model_tier("diagnosis"),
            ticket_id=state.ticket_id,
        )

        state.diagnosis = diagnosis.model_dump(by_alias=True)
        state.diagnosed_file_hashes = self.repo_tool.file_fingerprints(
            state.repo, state.subtask_id, diagnosis.files)
        state.freshness_recorded = True
        state.budget_used = BudgetUsed.model_validate(self.llm.get_usage(state.ticket_id))
        if diagnosis.no_root_cause or not diagnosis.root_cause.strip():
            state.status = "needs_human"
            reason = diagnosis.reasoning.strip() or "the available ticket and repository evidence was insufficient"
            state.failure_reason = f"Diagnosis could not determine a root cause: {reason}"
        return state

    def _context_snippets(self, state: SubtaskState) -> list[dict[str, str]]:
        snippets = []
        seen = set()
        for chunk in state.code_context.get("relevant_chunks", [])[:self.max_files]:
            path = chunk.get("path")
            if not path or path in seen:
                continue
            source = self.search.get_file(
                state.repo, state.subtask_id, path,
                int(chunk.get("start_line", 1)), int(chunk.get("end_line", 200)),
            )
            snippets.append({"path": path, "content": source["content"][:self.max_chars_per_file]})
            seen.add(path)
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


_SYSTEM_PROMPT = """You are the Diagnosis agent for one isolated subtask.
Use only the ticket description and provided file snippets. Return JSON with:
root_cause, files, reasoning, NoRootCause.
Be specific about the failing code path. Do not propose edits yet.
"""
