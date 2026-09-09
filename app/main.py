"""FastAPI application entry point.

Run with:
    uvicorn app.main:app --reload --port 8000
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app import events as ev
from app.agents.state import SubtaskState
from app.core.ownership import owner_of_key
from app.core.poller import poll_cycle, start_loop
from app.core.steps import post_step
from app.core.subtasks import cleanup_active_subtask_pile, prepare_subtask
from app.core.failures import describe_failure
from app.orchestrator.graph import run_diagnosis_graph
from app.db.connection import SessionLocal
from app.db.models import Subtask, Ticket, TicketEvent
from app.tools.github_tool import GitHubTool
from app.tools.jira_tool import JiraTool
from app.tools.repo_tokens import RepoTokenStore
from app.tools.repo_tool import RepoAccessRequired, RepoTool
from app.tools.repo_resolver import resolve_repos
from app.web.approval import router as approval_router, persist_state

logger = logging.getLogger(__name__)

_BASE = Path(__file__).parent

# SSE broadcast — one asyncio.Queue per connected client.
_subscribers: list[asyncio.Queue] = []


async def _broadcast(event: dict) -> None:
    for q in list(_subscribers):
        await q.put(event)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleaned = await asyncio.to_thread(cleanup_active_subtask_pile)
    if cleaned:
        logger.info("startup: superseded %d duplicate active subtask(s)", cleaned)
    from app.db.models import WebhookDelivery, PRLink, PendingApproval
    from app.db.connection import engine
    for table in (WebhookDelivery.__table__, PRLink.__table__, PendingApproval.__table__):
        await asyncio.to_thread(table.create, engine, checkfirst=True)
    from app.core.webhook_processing import recover_deliveries
    recovery = asyncio.create_task(recover_deliveries())
    task = await start_loop(_broadcast)
    yield
    recovery.cancel()
    task.cancel()
    await asyncio.gather(task, recovery, return_exceptions=True)


app = FastAPI(title="Agentic SDLC", lifespan=lifespan)
app.include_router(approval_router)
from app.web.webhooks import router as webhook_router
app.include_router(webhook_router)

app.mount(
    "/static",
    StaticFiles(directory=_BASE / "web" / "static"),
    name="static",
)

templates = Jinja2Templates(directory=_BASE / "web" / "templates")


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    with SessionLocal() as db:
        tickets = db.query(Ticket).order_by(Ticket.created_at.desc()).all()
    return templates.TemplateResponse(
        "index.html", {"request": request, "tickets": tickets}
    )


@app.get("/tickets/{ticket_id}", response_class=HTMLResponse)
def ticket_detail(request: Request, ticket_id: str) -> HTMLResponse:
    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return HTMLResponse(content="<h1>Ticket not found</h1>", status_code=404)
    return templates.TemplateResponse("ticket.html", {"request": request, "ticket": ticket})


@app.post("/tickets")
def create_ticket(
    title: str = Form(...),
    description: str = Form(...),
) -> RedirectResponse:
    with SessionLocal() as db:
        ticket = Ticket(source="manual", title=title, description=description)
        db.add(ticket)
        db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.get("/debug/jira", response_class=JSONResponse)
def debug_jira() -> JSONResponse:
    """Temporary: return open Jira issues for the configured project."""
    issues = JiraTool().list_open_issues()
    return JSONResponse(content=issues)


@app.get("/debug/jira/statuses", response_class=JSONResponse)
def debug_jira_statuses() -> JSONResponse:
    """Temporary: show Jira project statuses exactly as resolved from Jira."""
    try:
        statuses = JiraTool().fetch_project_statuses(
            force_refresh=True,
            timeout_seconds=5,
        )
    except Exception as exc:
        logger.exception("debug_jira_statuses failed")
        return JSONResponse(
            status_code=502,
            content={"error": "jira_status_fetch_failed", "detail": str(exc)},
        )
    return JSONResponse(content={"statuses": statuses})


@app.get("/debug/jira/category/{key}", response_class=JSONResponse)
def debug_jira_category(key: str) -> JSONResponse:
    """Temporary: show a Jira ticket's current status and resolved category."""
    try:
        jira = JiraTool()
        issue = jira.get_issue(key)
        category = issue.get("status_category") or jira.status_category(issue["status"])
    except Exception as exc:
        logger.exception("debug_jira_category failed for %r", key)
        return JSONResponse(
            status_code=502,
            content={"error": "jira_category_fetch_failed", "key": key, "detail": str(exc)},
        )
    return JSONResponse(
        content={
            "key": key,
            "status": issue["status"],
            "category": category,
        }
    )


@app.get("/debug/github", response_class=JSONResponse)
def debug_github() -> JSONResponse:
    """Temporary: return repo name and default branch from GitHub."""
    repo = GitHubTool().get_repo()
    return JSONResponse(content={"full_name": repo.full_name, "default_branch": repo.default_branch})


class _StatusRequest(BaseModel):
    key: str
    stage: str


@app.post("/debug/jira/status", response_class=JSONResponse)
def debug_jira_status(body: _StatusRequest) -> JSONResponse:
    """Temporary: move a Jira issue to the given internal stage and return the result."""
    result = JiraTool().set_status(body.key, body.stage)
    return JSONResponse(content=result)


@app.get("/debug/ownership/{key}", response_class=JSONResponse)
def debug_ownership(key: str) -> JSONResponse:
    """Temporary: classify who owns a Jira ticket: ai, human, or none."""
    return JSONResponse(content=owner_of_key(key))


@app.post("/ticket/{key}/resolve-repos", response_class=JSONResponse)
@app.post("/tickets/{key}/resolve-repos", response_class=JSONResponse)
async def ticket_resolve_repos(key: str) -> JSONResponse:
    """Run the repo-resolver cascade for a Jira issue key, return candidates, stream event."""
    ticket = _ticket_ref_by_identifier(key)
    issue_key = ticket.get("external_key") if ticket else key
    result = (
        {"source": "needs_paste", "candidates": []}
        if ticket and not issue_key
        else resolve_repos(issue_key)
    )
    if result.get("error"):
        ticket = ticket or _ensure_jira_ticket_ref(issue_key)
        _flag_needs_human(ticket["id"])
        await post_step(ticket, result["error"], stage="blocked", emit=_broadcast)
        await _broadcast({"type": "resolve_result", "key": issue_key or key, **result})
        return JSONResponse(status_code=422, content=result)
    await _broadcast({"type": "resolve_result", "key": issue_key or key, **result})
    return JSONResponse(content=result)


class _ConfirmReposRequest(BaseModel):
    repos: list[str]
    repo_tokens: dict[str, str] = {}


@app.get("/poll/events")
async def poll_events(request: Request) -> EventSourceResponse:
    """SSE stream — pushes a JSON event for every ticket the poller claims."""
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.append(q)

    async def stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20)
                    yield {"data": json.dumps(event)}
                except asyncio.TimeoutError:
                    yield {"comment": "keepalive"}
        finally:
            if q in _subscribers:
                _subscribers.remove(q)

    return EventSourceResponse(stream())


@app.post("/poll/run-once", response_class=JSONResponse)
async def poll_run_once() -> JSONResponse:
    """Manually trigger one poll cycle without waiting for the interval."""
    claimed = await poll_cycle(_broadcast)
    return JSONResponse(content={"claimed": claimed})


@app.post("/tickets/{ticket_id}/diagnose", response_class=JSONResponse)
async def diagnose_ticket(ticket_id: str) -> JSONResponse:
    """Diagnose and plan one subtask, returning at the checkpointed approval gate."""
    missing_repo_step: dict | None = None
    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        if ticket is None:
            return JSONResponse(status_code=404, content={"error": "ticket_not_found"})
        if not ticket.repos or not ticket.repos[0].strip():
            error = "no confirmed repo for this ticket"
            ticket.status = "needs_human"
            missing_repo_step = _ticket_step_ref(ticket)
            db.commit()
        else:
            error = ""
            repo = ticket.repos[0].strip()
            jira_key = ticket.external_key
            title = ticket.title
            description = ticket.description
            ticket_uuid = ticket.id

    if missing_repo_step is not None:
        await post_step(
            missing_repo_step,
            f"Blocked: {error}; resolve and confirm a valid repo before diagnosis.",
            stage="blocked",
            emit=_broadcast,
        )
        return JSONResponse(status_code=422, content={"error": error})

    replacement = await asyncio.to_thread(
        prepare_subtask,
        ticket_uuid,
        description=description,
        jira=JiraTool(),
        reuse_fresh_pending=True,
    )
    for item in replacement.superseded:
        await ev.publish(ticket_id, {
            "type": "subtask_superseded",
            "ticket_id": ticket_id,
            "subtask_id": item["subtask_id"],
            "agent": "subtask_lifecycle",
            "stage": "superseded",
            "message": item["message"],
        })
    subtask_id = replacement.subtask_id
    with SessionLocal() as db:
        subtask = db.get(Subtask, subtask_id)
        subtask.status = "running"
        db.get(Ticket, ticket_uuid).status = "processing"
        db.commit()

    state = SubtaskState(
        ticket_id=ticket_id,
        jira_key=jira_key,
        subtask_id=subtask_id,
        subtask_type="bug",
        description=f"{title}\n\n{description}".strip(),
        repo=repo,
    )
    try:
        await asyncio.to_thread(RepoTool().clone_or_pull, repo, subtask_id)
    except RepoAccessRequired as exc:
        error = describe_failure(f"Cloning repository {repo}", exc)
        logger.warning("diagnose_ticket private repo needs token ticket_id=%r repo=%r", ticket_id, repo)
        state.status = "needs_human"
        state.failure_reason = error
        await asyncio.to_thread(persist_state, state)
        blocked_step: dict | None = None
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            blocked_step = _ticket_step_ref(ticket) if ticket is not None else None
        if blocked_step is not None:
            await post_step(blocked_step, error, stage="blocked", emit=_broadcast)
        return JSONResponse(
            status_code=422,
            content={
                "error": error,
                "code": "private_repo_token_required",
                "repo": exc.full_name,
            },
        )
    except Exception as exc:
        error = describe_failure(f"Cloning repository {repo}", exc)
        logger.exception("diagnose_ticket clone failed ticket_id=%r repo=%r", ticket_id, repo)
        state.status = "needs_human"
        state.failure_reason = error
        await asyncio.to_thread(persist_state, state)
        blocked_step: dict | None = None
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            blocked_step = _ticket_step_ref(ticket) if ticket is not None else None
        if blocked_step is not None:
            await post_step(
                blocked_step,
                f"Blocked: {error}",
                stage="blocked",
                emit=_broadcast,
            )
        return JSONResponse(status_code=422, content={"error": error, "repo": repo})

    try:
        final_state = await run_diagnosis_graph(state)
    except Exception as exc:
        error = describe_failure("Running the diagnosis and planning workflow", exc)
        logger.exception("diagnose_ticket workflow failed ticket_id=%r repo=%r", ticket_id, repo)
        state.status = "needs_human"
        state.failure_reason = error
        await asyncio.to_thread(persist_state, state)
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            blocked_step = _ticket_step_ref(ticket) if ticket is not None else None
        if blocked_step is not None:
            await post_step(blocked_step, f"Blocked: {error}", stage="blocked", emit=_broadcast)
        return JSONResponse(status_code=422, content={"error": error, "repo": repo})

    await asyncio.to_thread(persist_state, final_state)
    await ev.log_event(ticket_id=ticket_id, subtask_id=subtask_id, agent="orchestrator",
                       stage="paused" if final_state.approval_payload else "needs_human",
                       message="Plan saved; waiting for approval" if final_state.approval_payload else final_state.failure_reason)

    await _broadcast(
        {
            "type": "diagnosis_done",
            "ticket_id": ticket_id,
            "subtask_id": subtask_id,
            "status": final_state.status,
            "diagnosis": final_state.diagnosis,
            "plan": final_state.model_dump(mode="json")["plan"],
            "approval_status": final_state.approval_status,
        }
    )
    return JSONResponse(
        content={
            "ticket_id": ticket_id,
            "subtask_id": subtask_id,
            "status": final_state.status,
            "diagnosis": final_state.diagnosis,
            "plan": final_state.model_dump(mode="json")["plan"],
            "approval_status": final_state.approval_status,
            "repo": repo,
        }
    )


@app.get("/stream/{ticket_id}")
async def stream_ticket(ticket_id: str, request: Request) -> EventSourceResponse:
    """Per-ticket SSE stream — emits every agent/tool event for this ticket (R-7, §9)."""
    async def _stream():
        async with ev.subscribe(ticket_id) as q:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20)
                    yield {"data": json.dumps(event)}
                except asyncio.TimeoutError:
                    yield {"comment": "keepalive"}

    return EventSourceResponse(_stream())


class _DebugEventBody(BaseModel):
    agent: str = "test"
    stage: str = "test"
    message: str = "manual test event"


@app.get("/debug/emit/{ticket_id}", response_class=JSONResponse)
async def debug_emit_events(ticket_id: str) -> JSONResponse:
    """Emit 3 sequential fake agent events — persisted + live.  Open the ticket
    page first, then hit this URL to watch events appear and survive a reload."""
    pairs = [
        ("diagnosis",  "started",    "reading repository files"),
        ("diagnosis",  "root_cause", "found off-by-one in divide()"),
        ("diagnosis",  "done",       "diagnosis complete — root cause recorded"),
    ]
    emitted = []
    for agent, stage, msg in pairs:
        e = await ev.log_event(ticket_id=ticket_id, agent=agent, stage=stage, message=msg)
        emitted.append(e)
    return JSONResponse(content={"emitted": emitted})


@app.post("/debug/events/{ticket_id}", response_class=JSONResponse)
async def debug_publish_event(ticket_id: str, body: _DebugEventBody) -> JSONResponse:
    """Persist + publish a test event — verifies both durability and live stream."""
    event = await ev.log_event(
        ticket_id=ticket_id,
        agent=body.agent,
        stage=body.stage,
        message=body.message,
    )
    return JSONResponse(content={"published": event, "subscribers": ev.subscriber_count(ticket_id)})


@app.get("/tickets/{ticket_id}/events", response_class=JSONResponse)
def ticket_event_history(ticket_id: str) -> JSONResponse:
    """Return all persisted events for a ticket ordered by time (for page-load replay)."""
    with SessionLocal() as db:
        rows = (
            db.query(TicketEvent)
            .filter(TicketEvent.ticket_id == ticket_id)
            .order_by(TicketEvent.ts.asc())
            .all()
        )
    return JSONResponse(content=[
        {
            "agent":      r.agent,
            "stage":      r.stage,
            "message":    r.message,
            "ts":         r.ts.isoformat(),
            "ticket_id":  str(r.ticket_id),
            "subtask_id": str(r.subtask_id) if r.subtask_id else None,
        }
        for r in rows
    ])


@app.post("/ticket/{key}/confirm-repos", response_class=JSONResponse)
@app.post("/tickets/{key}/confirm-repos", response_class=JSONResponse)
async def ticket_confirm_repos(key: str, body: _ConfirmReposRequest) -> JSONResponse:
    """Validate each repo is reachable on GitHub, then store on the ticket row."""
    existing_ticket = _ticket_ref_by_identifier(key)
    issue_key = existing_ticket.get("external_key") if existing_ticket else key
    invalid: list[tuple[str, str]] = []
    private: list[str] = []
    token_store: RepoTokenStore | None = None

    supplied_tokens = {
        repo: token
        for repo, token in body.repo_tokens.items()
        if repo in body.repos and token.strip()
    }
    if supplied_tokens:
        try:
            token_store = RepoTokenStore()
            for repo, token in supplied_tokens.items():
                token_store.save(repo, token.strip())
        except Exception as exc:
            logger.warning("confirm_repos: failed to save private repo token: %s", exc)
            error = describe_failure("Encrypting and storing the private repository token", exc)
            return JSONResponse(
                status_code=422,
                content={
                    "error": error,
                    "code": "encryption_key_required",
                },
            )

    repo_tool = RepoTool(token_store=token_store)
    for repo in body.repos:
        subtask_id = f"confirm-{key}-{repo.replace('/', '__')}"
        try:
            repo_tool.clone_or_pull(repo, subtask_id)
            repo_tool.cleanup_workspace(subtask_id)
        except RepoAccessRequired as exc:
            logger.warning("confirm_repos: %r needs private token: %s", repo, exc)
            private.append(repo)
        except Exception as exc:
            logger.warning("confirm_repos: %r not reachable: %s", repo, exc)
            invalid.append((repo, describe_failure(f"Validating repository {repo}", exc)))

    if private:
        ticket = existing_ticket or _ensure_jira_ticket_ref(issue_key)
        _flag_needs_human(ticket["id"])
        message = "this repo is private — enter a token to access it"
        await post_step(ticket, message, stage="blocked", emit=_broadcast)
        return JSONResponse(
            status_code=422,
            content={
                "error": message,
                "code": "private_repo_token_required",
                "private_repos": private,
            },
        )

    if invalid:
        ticket = existing_ticket or _ensure_jira_ticket_ref(issue_key)
        _flag_needs_human(ticket["id"])
        message = "; ".join(reason for _repo, reason in invalid)
        await post_step(ticket, message, stage="blocked", emit=_broadcast)
        return JSONResponse(
            status_code=422,
            content={
                "error": message,
                "invalid_repos": [repo for repo, _reason in invalid],
            },
        )

    with SessionLocal() as db:
        ticket = db.get(Ticket, existing_ticket["id"]) if existing_ticket else None
        if ticket is None:
            ticket = db.query(Ticket).filter(Ticket.external_key == issue_key).first()
        if ticket is None:
            issue = JiraTool().get_issue(issue_key)
            ticket = Ticket(
                source="jira",
                external_key=issue_key,
                title=issue["summary"],
                description=issue["description"],
                repos=body.repos,
            )
            db.add(ticket)
        else:
            ticket.repos = body.repos
        db.flush()
        ticket_for_step = _ticket_step_ref(ticket)
        saved_repos = list(ticket.repos or [])
        db.commit()

    logger.info("confirm_repos key=%r saved=%r", key, saved_repos)
    await post_step(
        ticket_for_step,
        f"Using repo `{', '.join(saved_repos)}`",
        stage="repo_resolved",
        emit=_broadcast,
    )
    return JSONResponse(content={"saved": True, "repos": saved_repos})


def _ensure_jira_ticket_ref(key: str) -> dict:
    with SessionLocal() as db:
        ticket = db.query(Ticket).filter(Ticket.external_key == key).first()
        if ticket is None:
            issue = JiraTool().get_issue(key)
            ticket = Ticket(
                source="jira",
                external_key=key,
                title=issue["summary"],
                description=issue["description"],
            )
            db.add(ticket)
            db.flush()
        ticket_ref = _ticket_step_ref(ticket)
        db.commit()
        return ticket_ref


def _flag_needs_human(ticket_id) -> None:
    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        if ticket is not None:
            ticket.status = "needs_human"
            db.commit()


def _ticket_ref_by_identifier(identifier: str) -> dict | None:
    """Resolve one local UUID or Jira key without fetching unrelated tickets."""
    with SessionLocal() as db:
        try:
            local_id = UUID(identifier)
        except ValueError:
            local_id = None
        ticket = db.get(Ticket, local_id) if local_id else None
        if ticket is None:
            ticket = db.query(Ticket).filter(Ticket.external_key == identifier).first()
        return _ticket_step_ref(ticket) if ticket is not None else None


def _ticket_step_ref(ticket: Ticket) -> dict:
    return {"id": ticket.id, "external_key": ticket.external_key}
