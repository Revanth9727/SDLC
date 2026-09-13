"""Persist and recall compact sub-task resolution summaries (memory.md)."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.agents.llm import LLMClient
from app.agents.router import model_tier
from app.config import settings
from app.core.failures import describe_failure
from app.db.connection import SessionLocal
from app.db.models import SubtaskMemory

logger = logging.getLogger(__name__)

MAX_RECALLS = 5
MAX_SUMMARY_CHARS = 1000
Outcome = Literal["success", "failed", "escalated"]


class ResolutionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    problem_summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    resolution_summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)


def embed(text: str, llm: LLMClient | None = None) -> list[float]:
    return (llm or LLMClient()).embed(text)


def _safe_summary(value: str) -> str:
    """Bound prose and redact common credential forms and code blocks."""
    value = re.sub(r"```[\s\S]*?```", "[code omitted]", value)
    value = re.sub(
        r"(?i)\b(api[_ -]?key|token|password|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        value,
    )
    value = re.sub(r"\b(?:sk|gh[opusr])_[A-Za-z0-9_-]{12,}\b", "[redacted]", value)
    return value.strip()[:MAX_SUMMARY_CHARS]


def find_similar(
    problem_text: str,
    k: int = 3,
    threshold: float = 0.75,
    *,
    llm: LLMClient | None = None,
    subtask_type: str | None = None,
) -> list[dict[str, Any]]:
    """Return bounded successful summaries above the cosine threshold."""
    if not 0 <= threshold <= 1:
        raise ValueError("threshold must be between 0 and 1")
    limit = min(max(int(k), 1), MAX_RECALLS)
    vector = embed(_safe_summary(problem_text), llm)
    distance = SubtaskMemory.embedding.cosine_distance(vector)
    query = (
        select(
            SubtaskMemory.ticket_id,
            SubtaskMemory.subtask_id,
            SubtaskMemory.problem_summary,
            SubtaskMemory.resolution_summary,
            SubtaskMemory.files_touched,
            SubtaskMemory.outcome,
            (1 - distance).label("similarity"),
        )
        .where(SubtaskMemory.outcome == "success")
        .order_by(distance)
        .limit(limit)
    )
    if subtask_type:
        query = query.where(SubtaskMemory.subtask_type == subtask_type)
    with SessionLocal() as db:
        rows = db.execute(query).all()
    return [
        {
            "ticket_id": str(row.ticket_id),
            "subtask_id": str(row.subtask_id),
            "problem_summary": _safe_summary(row.problem_summary),
            "resolution_summary": _safe_summary(row.resolution_summary),
            "files_touched": list(row.files_touched or [])[:25],
            "similarity": float(row.similarity),
            "outcome": row.outcome,
            "advisory": True,
            "caution": row.outcome != "success",
        }
        for row in rows
        if float(row.similarity) >= threshold
    ]


def advisory_refs(matches: list[dict[str, Any]], k: int | None = None) -> list[dict[str, Any]]:
    """Project database matches into the only shape agents may receive."""
    limit = min(max(k or settings.memory_top_k, 1), MAX_RECALLS)
    allowed = (
        "problem_summary", "resolution_summary", "files_touched", "similarity",
        "outcome", "advisory", "caution",
    )
    refs = []
    for match in matches[:limit]:
        ref = {key: match[key] for key in allowed if key in match}
        ref["problem_summary"] = _safe_summary(ref.get("problem_summary", ""))
        ref["resolution_summary"] = _safe_summary(ref.get("resolution_summary", ""))
        ref["files_touched"] = list(ref.get("files_touched", []))[:25]
        ref["advisory"] = True
        ref["caution"] = ref.get("outcome", "success") != "success"
        refs.append(ref)
    return refs


def search_similar(
    description: str,
    subtask_type: str | None = None,
    *,
    k: int | None = None,
    threshold: float | None = None,
    llm: LLMClient | None = None,
) -> list[dict[str, Any]]:
    """Compatibility form used by the existing reuse node."""
    return find_similar(
        description,
        k=settings.memory_top_k if k is None else k,
        threshold=settings.memory_search_threshold if threshold is None else threshold,
        llm=llm,
        subtask_type=subtask_type,
    )


def write_resolution(state, outcome: Outcome | None = None, llm: LLMClient | None = None) -> None:
    """Summarize once, embed the problem, and upsert a terminal resolution."""
    try:
        llm = llm or LLMClient()
        resolved_outcome: Outcome = outcome or (
            "success" if state.status in {"done", "in_review", "integration_pending"}
            else "failed" if state.status == "failed" else "escalated"
        )
        payload = {
            "problem": _safe_summary(state.description),
            "outcome": resolved_outcome,
            "root_cause": _safe_summary((state.diagnosis or {}).get("root_cause", "")),
            "plan": [
                {"intent": _safe_summary(step.intent), "target_file": step.target_file, "action": step.action}
                for step in state.plan[:20]
            ],
            "files_touched": sorted(state.file_changes)[:25],
            "failure_reason": _safe_summary(state.failure_reason or ""),
        }
        summary = llm.complete_json(
            _SUMMARY_SYSTEM, json.dumps(payload), ResolutionSummary,
            tier=model_tier("summarization"), ticket_id=state.ticket_id,
        )
        summary = ResolutionSummary.model_validate(summary)
        problem_summary = _safe_summary(summary.problem_summary)
        resolution_summary = _safe_summary(summary.resolution_summary)
        vector = embed(_safe_summary(state.description), llm)
        values = {
            "subtask_id": state.subtask_id, "ticket_id": state.ticket_id,
            "subtask_type": state.subtask_type, "problem_summary": problem_summary,
            "resolution_summary": resolution_summary,
            "files_touched": sorted(state.file_changes)[:25], "outcome": resolved_outcome,
            "embedding": vector,
        }
        with SessionLocal() as db:
            db.execute(
                insert(SubtaskMemory).values(**values).on_conflict_do_update(
                    index_elements=[SubtaskMemory.subtask_id],
                    set_={key: value for key, value in values.items() if key != "subtask_id"},
                )
            )
            db.commit()
    except Exception as exc:
        logger.warning("memory.write_resolution failed for subtask %s: %s", state.subtask_id,
                       describe_failure("Writing subtask memory", exc))


def write_back(state, llm: LLMClient | None = None) -> None:
    """Backward-compatible success write used by existing PR publication paths."""
    write_resolution(state, "success", llm)


_SUMMARY_SYSTEM = """Create a compact prose memory for a future, unrelated ticket.
Summarize the problem class and what resolved or blocked it. Do not include ticket IDs,
credentials, secrets, raw code, code blocks, or full file contents. File paths may be
mentioned. Treat failed/escalated outcomes as cautions, never successful recipes."""
