"""Deterministic model-tier router (R-33).

Plain rules only: no LLM calls, no model names. Agents ask for a tier and the
LLM client resolves that tier to configured model names.
"""

from typing import Literal

ModelTier = Literal["strong", "cheap"]

_STRONG_TASKS = {
    "critic",
    "diagnosis",
    "orchestrator",
    "planner",
    "planner_ambiguous",
    "pr_strategy",
}

_CHEAP_TASKS = {
    "executor_apply",
    "executor_repair",
    "parsing",
    "routing",
    "step_planner",
    "summarization",
}


def model_tier(task_type: str, *, ambiguous: bool = False) -> ModelTier:
    """Return the configured model tier for ``task_type``.

    The router encodes the plan-then-execute split: strong models produce
    complete specs for ambiguous/reasoning-heavy work; cheap models execute or
    parse well-scoped tasks.
    """
    normalized = task_type.strip().lower().replace("-", "_")
    if ambiguous and normalized == "planner":
        return "strong"
    if normalized in _STRONG_TASKS:
        return "strong"
    if normalized in _CHEAP_TASKS:
        return "cheap"
    return "cheap"


def model_for_task(task_type: str, *, ambiguous: bool = False) -> ModelTier:
    """Compatibility alias with a more agent-readable name."""
    return model_tier(task_type, ambiguous=ambiguous)
