"""Classify a valid failing test before it is allowed to drive code repair."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.router import model_tier
from app.agents.state import SubtaskState
from app.tools.test_runner import TestResult


class TestFailureScope(BaseModel):
    """Typed R-32c decision for a test already proven internally valid."""

    __test__ = False
    model_config = ConfigDict(extra="forbid")
    classification: Literal["ticket_change", "unrelated_defect", "uncertain"]
    failing_behavior: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=10)
    suspected_root_cause: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def unrelated_requires_specific_evidence(self):
        if self.classification == "unrelated_defect" and (
            not self.evidence or not self.suspected_root_cause.strip()
        ):
            raise ValueError("An unrelated defect requires evidence and a suspected root cause")
        return self


def classify_test_failure(llm, state: SubtaskState, step, test_content: str,
                          result: TestResult) -> TestFailureScope:
    """Use bounded reasoning over approved scope and verified code context.

    This runs only after deterministic test-validity checks passed and pytest
    produced a real FAIL. It does not edit code or decide whether a test is valid.
    """
    payload = {
        "ticket_requirement": state.description,
        "diagnosis": state.diagnosis,
        "approved_plan": [item.model_dump() for item in state.plan],
        "completed_steps": state.steps_done,
        "changed_files": sorted(state.file_changes),
        "changed_symbols": state.code_impacts,
        "verified_code_context": {
            key: state.code_context.get(key)
            for key in ("relevant_files", "relevant_functions", "execution_path", "verified_evidence")
            if key in state.code_context
        },
        "failing_test_step": step.model_dump(),
        "failing_test": test_content,
        "pytest_failure": result.output[-8000:],
    }
    assessment = llm.complete_json(
        _SYSTEM, json.dumps(payload), TestFailureScope,
        tier=model_tier("critic"), ticket_id=state.ticket_id,
    )
    return TestFailureScope.model_validate(assessment)


_SYSTEM = """You classify the cause of a test that is already proven VALID and has failed.
Compare the failing assertion and behavior with the ticket requirement, diagnosis,
approved plan, changed files/symbols, and verified code context.

Return ticket_change only when the failure exercises behavior the approved plan was
supposed to change, or evidence ties the regression to that change. Return
unrelated_defect when the valid test exposed a distinct pre-existing behavior outside
the approved requirement/files/symbol contract; name the exact behavior and suspected
root cause with concrete evidence. Caller popularity alone is not evidence. Return
uncertain when the supplied evidence cannot safely distinguish them. Never broaden the
ticket, propose edits, or call a valid test invalid merely because it failed."""
