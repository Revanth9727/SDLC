import uuid

from sqlalchemy import select

from app.core.subtasks import ACTIVE_SUBTASK_STATUSES, prepare_subtask, summarize_subtask
from app.db.connection import SessionLocal
from app.db.models import Subtask, Ticket, TicketEvent


class Jira:
    def __init__(self):
        self.comments = []

    def comment(self, key, message):
        self.comments.append((key, message))


def test_summary_reports_completed_stages_and_stop():
    subtask = Subtask(
        ticket_id=uuid.uuid4(),
        type="bug",
        description="test",
        status="running",
        state={
            "diagnosis": {"root_cause": "unguarded division", "files": ["app.py"]},
            "plan": [{"step_id": "1", "intent": "Guard zero", "target_file": "app.py"}],
            "steps_done": [{"step_id": "1", "intent": "Guard zero"}],
            "approval_status": "pending",
            "approval_payload": {"plan": []},
        },
    )
    subtask.id = uuid.uuid4()
    summary = summarize_subtask(subtask, [])
    assert "Diagnosis completed: unguarded division in app.py" in summary
    assert "Plan prepared with 1 step" in summary
    assert "Completed 1 step" in summary
    assert "waiting for plan approval" in summary


def test_repick_supersedes_active_attempt_and_keeps_only_one():
    ticket_id = uuid.uuid4()
    old_id = uuid.uuid4()
    jira = Jira()
    with SessionLocal() as db:
        db.add(Ticket(
            id=ticket_id,
            source="jira",
            external_key="R43-" + ticket_id.hex[:8],
            title="Repick",
            description="test",
        ))
        db.flush()
        db.add(Subtask(
            id=old_id,
            ticket_id=ticket_id,
            type="bug",
            description="old",
            status="running",
            state={
                "diagnosis": {"root_cause": "old cause", "files": ["app.py"], "reasoning": "old evidence"},
                "plan": [{"step_id": "1", "intent": "fix it", "target_file": "app.py", "action": "edit"}],
                "steps_done": [{"step_id": "1", "intent": "bad edit", "target_file": "app.py",
                                "content": "must not be inherited"}],
                "failure_reason": "Generated test missed the import",
                "attempt_history": ["validator: MissingSymbol was not imported"],
            },
        ))
        db.commit()
    try:
        result = prepare_subtask(ticket_id, description="fresh", jira=jira)
        with SessionLocal() as db:
            old = db.get(Subtask, old_id)
            active = list(db.scalars(select(Subtask).where(
                Subtask.ticket_id == ticket_id,
                Subtask.status.in_(ACTIVE_SUBTASK_STATUSES),
            )))
            event = db.scalar(select(TicketEvent).where(
                TicketEvent.ticket_id == ticket_id,
                TicketEvent.subtask_id == old_id,
                TicketEvent.stage == "superseded",
            ))
        assert old.status == "superseded"
        assert len(active) == 1
        assert str(active[0].id) == result.subtask_id
        assert event and "old cause" in event.message
        assert jira.comments[0][0].startswith("R43-")
        assert "old cause" in jira.comments[0][1]
        assert result.prior_attempt["source_subtask_id"] == str(old_id)
        assert result.prior_attempt["failure_reason"] == "Generated test missed the import"
        assert result.prior_attempt["diagnosis"]["root_cause"] == "old cause"
        assert result.prior_attempt["steps_completed"] == [
            {"step_id": "1", "intent": "bad edit", "target_file": "app.py"}
        ]
        assert "content" not in result.prior_attempt["steps_completed"][0]
    finally:
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            if ticket:
                db.delete(ticket)
                db.commit()


def test_diagnosis_reuses_fresh_claim_subtask():
    ticket_id = uuid.uuid4()
    with SessionLocal() as db:
        db.add(Ticket(id=ticket_id, source="manual", title="Fresh", description="test"))
        db.commit()
    try:
        claimed = prepare_subtask(ticket_id, description="test")
        diagnosed = prepare_subtask(
            ticket_id,
            description="test",
            reuse_fresh_pending=True,
        )
        assert diagnosed.subtask_id == claimed.subtask_id
        assert not diagnosed.superseded
        with SessionLocal() as db:
            active_count = len(list(db.scalars(select(Subtask).where(
                Subtask.ticket_id == ticket_id,
                Subtask.status.in_(ACTIVE_SUBTASK_STATUSES),
            ))))
        assert active_count == 1
        assert diagnosed.prior_attempt is None
    finally:
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            if ticket:
                db.delete(ticket)
                db.commit()


def test_repick_prior_context_is_ticket_isolated():
    first_id, other_id = uuid.uuid4(), uuid.uuid4()
    with SessionLocal() as db:
        db.add_all([
            Ticket(id=first_id, source="manual", title="First", description="first"),
            Ticket(id=other_id, source="manual", title="Other", description="other"),
            Subtask(ticket_id=first_id, type="bug", description="first", status="needs_human",
                    state={"failure_reason": "first ticket failure"}),
            Subtask(ticket_id=other_id, type="bug", description="other", status="needs_human",
                    state={"failure_reason": "other ticket secret context"}),
        ])
        db.commit()
    try:
        result = prepare_subtask(first_id, description="retry first")
        assert result.prior_attempt["failure_reason"] == "first ticket failure"
        assert "other ticket" not in str(result.prior_attempt)
    finally:
        with SessionLocal() as db:
            for ticket_id in (first_id, other_id):
                ticket = db.get(Ticket, ticket_id)
                if ticket:
                    db.delete(ticket)
            db.commit()


def test_subtasks_api_does_not_reopen_superseded_checkpoint(monkeypatch):
    import asyncio
    from app.web import approval

    async def fail_if_opened(*args, **kwargs):
        raise AssertionError("closed subtasks must not load an active checkpoint")

    monkeypatch.setattr(approval, "_subtasks", lambda ticket_id: [{
        "subtask_id": str(uuid.uuid4()),
        "status": "superseded",
        "state": {"status": "running"},
    }])
    monkeypatch.setattr(approval, "open_graph", fail_if_opened)
    rows = asyncio.run(approval.ticket_subtasks(uuid.uuid4()))
    assert rows[0]["status"] == "superseded"
    assert rows[0]["waiting"] is False
