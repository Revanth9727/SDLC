import uuid
from types import SimpleNamespace

import pytest

from app import main
from app.db.connection import SessionLocal
from app.db.models import Ticket


def test_list_renders_contextual_repo_actions():
    template = main.templates.get_template("index.html")
    missing = SimpleNamespace(
        id=uuid.uuid4(), external_key="UI-1", title="Missing repo",
        status="new", claimed_at=None, repos=None,
    )
    confirmed = SimpleNamespace(
        id=uuid.uuid4(), external_key="UI-2", title="Confirmed repo",
        status="processing", claimed_at=None, repos=["owner/repo"],
    )
    html = template.render(request=None, tickets=[missing, confirmed])
    missing_row = html.split("Missing repo", 1)[1].split("</tr>", 1)[0]
    confirmed_row = html.split("Confirmed repo", 1)[1].split("</tr>", 1)[0]
    assert "Resolve repos" in missing_row
    assert "Change repo" not in missing_row
    assert "owner/repo" in confirmed_row
    assert "Change repo" in confirmed_row
    assert "Resolve repos" not in confirmed_row


def test_ticket_detail_contains_complete_state_driven_interface():
    source = (main._BASE / "web" / "templates" / "ticket.html").read_text()
    for control in (
        "Current run", "diagnosis-panel", "plan-panel", "budget_used",
        "Run diagnosis &amp; plan", "approve-btn", "reject-btn", "retry-btn",
        "failure-reason", "Previous attempts", "subtask_id",
        "critic-panel", "critic_verdict", "critic-verifiability",
        "critic_retry_count", "MAX_AGENT_RETRIES", "renderCritic(display)",
        "overall-progress", "subtask-board", "renderSubtasks(workRows)",
        "subtaskGate(row)", "Sequential by dependency",
    ):
        assert control in source
    assert "active?.waiting" in source
    assert "failure-panel').hidden" in source


@pytest.mark.asyncio
async def test_manual_ticket_resolve_alias_opens_paste_gate():
    ticket_id = uuid.uuid4()
    with SessionLocal() as db:
        db.add(Ticket(
            id=ticket_id, source="manual", title="UI manual ticket",
            description="Resolve from browser",
        ))
        db.commit()
    try:
        response = await main.ticket_resolve_repos(str(ticket_id))
        assert response.status_code == 200
        assert b'"source":"needs_paste"' in response.body
    finally:
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            if ticket:
                db.delete(ticket)
                db.commit()


def test_ticket_id_repo_routes_keep_existing_api_routes():
    routes = {(route.path, method) for route in main.app.routes for method in getattr(route, "methods", set())}
    assert ("/ticket/{key}/resolve-repos", "POST") in routes
    assert ("/tickets/{key}/resolve-repos", "POST") in routes
    assert ("/ticket/{key}/confirm-repos", "POST") in routes
    assert ("/tickets/{key}/confirm-repos", "POST") in routes
