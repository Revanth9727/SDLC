"""Validated execution artifacts kept in each subtask checkpoint."""
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.tools.edit_applier import EditBlock, EditReport
from app.tools.test_runner import TestResult


class EditProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blocks: list[EditBlock] = Field(default_factory=list, max_length=20)
    full_content: str | None = None
    unable_reason: str | None = None

    @model_validator(mode='after')
    def outcome(self):
        outcomes = int(bool(self.blocks)) + int(self.full_content is not None) + int(bool(self.unable_reason))
        if outcomes != 1:
            raise ValueError('Provide exactly one of blocks, full_content, or an honest unable_reason')
        return self


class StepResult(BaseModel):
    step_id: str
    intent: str
    target_file: str
    content: str
    before_sha256: str
    report: EditReport
    tests: TestResult
