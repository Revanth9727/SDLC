"""Validated execution artifacts kept in each subtask checkpoint."""
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.tools.edit_applier import EditBlock, EditReport
from app.tools.test_runner import TestResult


class EditProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blocks: list[EditBlock] = Field(default_factory=list, max_length=20)
    unable_reason: str | None = None

    @model_validator(mode='after')
    def outcome(self):
        if bool(self.blocks) == bool(self.unable_reason):
            raise ValueError('Provide blocks or an honest unable_reason')
        return self


class StepResult(BaseModel):
    step_id: str
    intent: str
    target_file: str
    content: str
    before_sha256: str
    report: EditReport
    tests: TestResult
