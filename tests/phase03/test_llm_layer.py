from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.agents import llm
from app.agents.llm import LLMClient
from app.agents.router import model_tier
from app.agents.state import SubtaskState


class _ChoiceMessage:
    def __init__(self, content: str) -> None:
        self.message = SimpleNamespace(content=content)


class _FakeCompletions:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.responses.pop(0)
        return SimpleNamespace(
            choices=[_ChoiceMessage(content)],
            usage=SimpleNamespace(total_tokens=100),
        )


class _FakeChat:
    def __init__(self, responses: list[str]) -> None:
        self.completions = _FakeCompletions(responses)


class _FakeOpenAI:
    def __init__(self, responses: list[str]) -> None:
        self.chat = _FakeChat(responses)


class _Ping(BaseModel):
    word: str


def test_complete_uses_tier_model_and_tracks_usage(monkeypatch) -> None:
    monkeypatch.setattr(
        llm,
        "settings",
        SimpleNamespace(
            openai_api_key="test", max_agent_retries=2, llm_est_cost_per_1k_tokens=0.01,
            model_strong="strong-model",
            model_cheap="cheap-model",
        ),
    )
    LLMClient.reset_usage()
    fake = _FakeOpenAI(["pong"])
    client = LLMClient(ticket_id="ticket-1", client=fake)

    assert client.complete("system", "user", tier="cheap") == "pong"

    call = fake.chat.completions.calls[0]
    assert call["model"] == "cheap-model"
    assert LLMClient.get_usage("ticket-1") == {
        "calls": 1,
        "tokens": 100,
        "est_cost_usd": 0.001,
    }


def test_complete_json_retries_once_on_validation_error(monkeypatch) -> None:
    monkeypatch.setattr(
        llm,
        "settings",
        SimpleNamespace(
            openai_api_key="test", max_agent_retries=2, llm_est_cost_per_1k_tokens=0.01,
            model_strong="gpt-4o",
            model_cheap="gpt-4o-mini",
        ),
    )
    LLMClient.reset_usage()
    fake = _FakeOpenAI(["not-json", '{"word":"pong"}'])
    client = LLMClient(client=fake)

    with client.use_ticket("ticket-2"):
        result = client.complete_json("Reply in JSON.", "ping", _Ping, tier="strong")

    assert result == _Ping(word="pong")
    assert len(fake.chat.completions.calls) == 2
    assert LLMClient.get_usage("ticket-2")["calls"] == 2


def test_complete_json_strips_markdown_code_fence(monkeypatch) -> None:
    # Some models wrap JSON in ```json ... ``` despite instructions not to; that
    # used to raise a raw ValidationError ("Invalid JSON: expected value at line
    # 1 column 1") instead of parsing. Also confirms JSON mode is requested.
    monkeypatch.setattr(
        llm,
        "settings",
        SimpleNamespace(openai_api_key="test", max_agent_retries=2, llm_est_cost_per_1k_tokens=0.01, model_strong="gpt-4o", model_cheap="gpt-4o-mini"),
    )
    LLMClient.reset_usage()
    fenced = '```json\n{\n  "word": "pong"\n}\n```'
    fake = _FakeOpenAI([fenced])
    client = LLMClient(client=fake)

    with client.use_ticket("ticket-3"):
        result = client.complete_json("Reply in JSON.", "ping", _Ping, tier="strong")

    assert result == _Ping(word="pong")
    assert len(fake.chat.completions.calls) == 1  # parsed on the first attempt, no retry needed
    assert fake.chat.completions.calls[0]["response_format"] == {"type": "json_object"}


def test_router_is_deterministic() -> None:
    assert model_tier("diagnosis") == "strong"
    assert model_tier("planner", ambiguous=True) == "strong"
    assert model_tier("step_planner") == "cheap"
    assert model_tier("executor_apply") == "cheap"
    assert model_tier("parsing") == "cheap"
    assert model_tier("routing") == "cheap"
    assert model_tier("critic") == "strong"
    assert model_tier("unknown_task") == "cheap"


def test_subtask_state_has_blackboard_fields() -> None:
    state = SubtaskState(
        ticket_id="ticket-1",
        subtask_id="subtask-1",
        subtask_type="bug",
        description="Fix divide by zero",
        repo="owner/repo",
    )

    assert state.depends_on == []
    assert state.diagnosis is None
    assert state.plan == []
    assert state.current_step == 0
    assert state.steps_done == []
    assert state.approval_status == "pending"
    assert state.approval_payload is None
    assert state.retry_count == 0
    assert state.budget_used.calls == 0
    assert state.status == "running"
    assert state.failure_reason is None
    assert state.pr_url is None
    assert state.memory_refs == []
