"""Two-tier LLM response cache behavior (R-36)."""
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from sqlalchemy import delete, select

from app.agents import cache
from app.agents.llm import LLMClient
from app.config import settings
from app.db.connection import SessionLocal
from app.db.init_db import ensure_llm_cache_schema
from app.db.models import ExactCache, SemanticCache


class FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="cached response"))],
            usage=SimpleNamespace(total_tokens=12),
        )


class FakeEmbeddings:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        vector = [0.0] * 1536
        vector[0] = 1.0
        return SimpleNamespace(data=[SimpleNamespace(embedding=vector)])


class FakeOpenAI:
    def __init__(self):
        self.chat = SimpleNamespace(completions=FakeCompletions())
        self.embeddings = FakeEmbeddings()


class Answer(BaseModel):
    answer: str


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    ensure_llm_cache_schema()
    with SessionLocal() as db:
        db.execute(delete(SemanticCache))
        db.execute(delete(ExactCache))
        db.commit()
    cache.reset_stats()
    monkeypatch.setattr(settings, "llm_cache_enabled", True)
    monkeypatch.setattr(settings, "llm_cache_ticket_prompts", False)
    monkeypatch.setattr(settings, "llm_cache_semantic_threshold", 0.92)


def test_identical_second_request_is_exact_hit():
    provider = FakeOpenAI()
    client = LLMClient(client=provider)

    assert client.complete("generic system", "same request", tier="cheap") == "cached response"
    assert client.complete("generic system", "same request", tier="cheap") == "cached response"

    assert len(provider.chat.completions.calls) == 1
    assert cache.cache_stats() == {
        "exact_hits": 1, "semantic_hits": 0, "misses": 1,
        "bypassed": 0, "requests": 2, "hit_rate": 0.5,
    }
    with SessionLocal() as db:
        assert db.scalar(select(ExactCache.prompt_hash))
        assert db.scalar(select(SemanticCache.id))


def test_near_duplicate_uses_semantic_cache():
    provider = FakeOpenAI()
    client = LLMClient(client=provider)

    client.complete("generic system", "summarize an error", tier="cheap")
    result = client.complete("generic system", "summarise this error", tier="cheap")

    assert result == "cached response"
    assert len(provider.chat.completions.calls) == 1
    assert cache.cache_stats()["semantic_hits"] == 1


def test_ticket_scoped_prompts_bypass_shared_cache_by_default():
    provider = FakeOpenAI()
    client = LLMClient(ticket_id="private-ticket", client=provider)

    client.complete("system", "private details", tier="cheap")
    client.complete("system", "private details", tier="strong")

    assert len(provider.chat.completions.calls) == 2
    assert cache.cache_stats()["bypassed"] == 2
    with SessionLocal() as db:
        assert db.scalar(select(ExactCache.prompt_hash)) is None


def test_invalid_json_cache_entry_is_evicted_before_retry():
    provider = FakeOpenAI()
    responses = iter(["not json", '{"answer":"ok"}'])
    provider.chat.completions.create = lambda **kwargs: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=next(responses)))],
        usage=SimpleNamespace(total_tokens=12),
    )
    client = LLMClient(client=provider)

    assert client.complete_json("generic", "structured", Answer, tier="cheap") == Answer(answer="ok")
    with SessionLocal() as db:
        assert db.scalar(select(ExactCache.response).where(ExactCache.response == "not json")) is None


def test_cache_stats_route_reports_current_counters():
    from app.main import llm_cache_stats

    cache.note("exact_hits")
    payload = llm_cache_stats().body.decode()
    assert '"exact_hits":1' in payload
    assert '"hit_rate":1.0' in payload
