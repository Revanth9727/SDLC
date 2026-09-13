"""Memory persistence, relevance filtering, and injection guardrails."""
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.agents.state import SubtaskState
from app.db.connection import SessionLocal, engine
from app.db.init_db import ensure_memory_schema
from app.db.models import Subtask, SubtaskMemory, Ticket
from app.memory import store


def _vector(index: int) -> list[float]:
    vector = [0.0] * 1536
    vector[index] = 1.0
    return vector


@pytest.fixture(autouse=True)
def memory_schema():
    SubtaskMemory.__table__.create(engine, checkfirst=True)
    ensure_memory_schema()


@pytest.fixture
def state():
    ticket_id, subtask_id = uuid.uuid4(), uuid.uuid4()
    with SessionLocal() as db:
        db.add(Ticket(id=ticket_id, source="jira", external_key=f"MEM-{ticket_id.hex[:8]}",
                      title="Guard division", description="Avoid dividing by zero", status="processing"))
        db.flush()
        db.add(Subtask(id=subtask_id, ticket_id=ticket_id, type="bug",
                       description="Avoid dividing by zero", status="done"))
        db.commit()
    value = SubtaskState(ticket_id=str(ticket_id), subtask_id=str(subtask_id),
                         subtask_type="bug", description="Avoid dividing by zero",
                         repo="owner/repo", status="done", file_changes={"app.py": "changed"})
    yield value
    with SessionLocal() as db:
        db.delete(db.get(Ticket, ticket_id))
        db.commit()


def test_write_and_read_roundtrip(state):
    llm = SimpleNamespace(
        complete_json=Mock(return_value=store.ResolutionSummary(
            problem_summary="Division without a zero guard",
            resolution_summary="Validate the denominator before division",
        )),
        embed=Mock(return_value=_vector(0)),
    )
    store.write_resolution(state, "success", llm)

    matches = store.find_similar(state.description, llm=llm)
    assert len(matches) == 1
    assert matches[0]["resolution_summary"] == "Validate the denominator before division"
    assert matches[0]["outcome"] == "success"
    assert llm.complete_json.call_count == 1


def test_threshold_filters_unrelated_memory(state):
    writer = SimpleNamespace(
        complete_json=Mock(return_value=store.ResolutionSummary(
            problem_summary="Division guard", resolution_summary="Check denominator")),
        embed=Mock(return_value=_vector(0)),
    )
    store.write_resolution(state, "success", writer)
    unrelated = SimpleNamespace(embed=Mock(return_value=_vector(1)))
    assert store.find_similar("unrelated styling request", threshold=0.75, llm=unrelated) == []


def test_injection_contains_summaries_not_raw_context():
    refs = store.advisory_refs([{
        "problem_summary": "A bounded problem summary",
        "resolution_summary": "A bounded resolution summary",
        "files_touched": ["app.py"],
        "similarity": 0.91,
        "outcome": "failed",
        "raw_context": "SECRET raw source and prompt",
        "file_changes": {"app.py": "raw code"},
    }])
    assert len(refs) == 1
    assert "raw_context" not in refs[0]
    assert "file_changes" not in refs[0]
    assert refs[0]["advisory"] is True
    assert refs[0]["caution"] is True
