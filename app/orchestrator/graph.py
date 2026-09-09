"""Minimal LangGraph orchestration for the first real agent."""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from app.agents.diagnosis import DiagnosisAgent
from app.agents.state import SubtaskState
from app.config import settings
from app.events import log_event

logger = logging.getLogger(__name__)

_POSTGRES_CHECKPOINTER_CONTEXT = None


def _checkpointer():
    global _POSTGRES_CHECKPOINTER_CONTEXT
    try:
        from langgraph.checkpoint.postgres import PostgresSaver

        candidate = PostgresSaver.from_conn_string(settings.database_url)
        if hasattr(candidate, "__enter__"):
            _POSTGRES_CHECKPOINTER_CONTEXT = candidate
            checkpointer = candidate.__enter__()
        else:
            checkpointer = candidate
        if hasattr(checkpointer, "setup"):
            checkpointer.setup()
        return checkpointer
    except Exception as exc:
        from langgraph.checkpoint.memory import MemorySaver

        logger.warning(
            "graph: Postgres checkpointer unavailable; using MemorySaver: %s",
            exc,
        )
        return MemorySaver()


def build_graph(agent: DiagnosisAgent | None = None):
    diagnosis_agent = agent or DiagnosisAgent()

    async def diagnose_node(raw_state: dict[str, Any]) -> dict[str, Any]:
        state = SubtaskState.model_validate(raw_state)
        await log_event(
            ticket_id=state.ticket_id,
            subtask_id=state.subtask_id,
            agent="diagnosis",
            stage="started",
            message=f"Diagnosis started for {state.repo}",
        )
        updated = diagnosis_agent.run(state)
        if updated.status == "needs_human":
            stage = "needs_human"
            message = "Diagnosis could not find a root cause; human review needed"
        else:
            stage = "done"
            root = (updated.diagnosis or {}).get("root_cause", "")
            message = f"Diagnosis complete — {root}"
        await log_event(
            ticket_id=updated.ticket_id,
            subtask_id=updated.subtask_id,
            agent="diagnosis",
            stage=stage,
            message=message,
        )
        return updated.model_dump()

    graph = StateGraph(dict)
    graph.add_node("diagnosis", diagnose_node)
    graph.set_entry_point("diagnosis")
    graph.add_edge("diagnosis", END)
    return graph.compile(checkpointer=_checkpointer())


async def run_diagnosis_graph(state: SubtaskState) -> SubtaskState:
    graph = build_graph()
    result = await graph.ainvoke(
        state.model_dump(),
        config={"configurable": {"thread_id": state.subtask_id}},
    )
    return SubtaskState.model_validate(result)
