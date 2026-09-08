"""FastAPI application entry point.

Run with:
    uvicorn app.main:app --reload --port 8000
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app import events as ev
from app.core.poller import poll_cycle, start_loop
from app.db.connection import SessionLocal
from app.db.models import Ticket
from app.tools.github_tool import GitHubTool
from app.tools.jira_tool import JiraTool
from app.tools.repo_resolver import resolve_repos

logger = logging.getLogger(__name__)

_BASE = Path(__file__).parent

# SSE broadcast — one asyncio.Queue per connected client.
_subscribers: list[asyncio.Queue] = []


async def _broadcast(event: dict) -> None:
    for q in list(_subscribers):
        await q.put(event)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = await start_loop(_broadcast)
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Agentic SDLC", lifespan=lifespan)

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


@app.post("/ticket/{key}/resolve-repos", response_class=JSONResponse)
async def ticket_resolve_repos(key: str) -> JSONResponse:
    """Run the repo-resolver cascade for a Jira issue key, return candidates, stream event."""
    result = resolve_repos(key)
    await _broadcast({"type": "resolve_result", "key": key, **result})
    return JSONResponse(content=result)


class _ConfirmReposRequest(BaseModel):
    repos: list[str]


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


@app.post("/debug/events/{ticket_id}", response_class=JSONResponse)
async def debug_publish_event(ticket_id: str, body: _DebugEventBody) -> JSONResponse:
    """Publish a test event to a ticket stream — useful for manual SSE verification."""
    event = ev.make_event(
        agent=body.agent,
        stage=body.stage,
        message=body.message,
        ticket_id=ticket_id,
    )
    await ev.publish(ticket_id, event)
    return JSONResponse(content={"published": event, "subscribers": ev.subscriber_count(ticket_id)})


@app.post("/ticket/{key}/confirm-repos", response_class=JSONResponse)
def ticket_confirm_repos(key: str, body: _ConfirmReposRequest) -> JSONResponse:
    """Validate each repo is reachable on GitHub, then store on the ticket row."""
    github = GitHubTool()
    invalid: list[str] = []
    for repo in body.repos:
        try:
            github.get_repo(repo)
        except Exception as exc:
            logger.warning("confirm_repos: %r not reachable: %s", repo, exc)
            invalid.append(repo)

    if invalid:
        return JSONResponse(
            status_code=422,
            content={"error": f"repos not reachable on GitHub: {invalid}"},
        )

    with SessionLocal() as db:
        ticket = db.query(Ticket).filter(Ticket.external_key == key).first()
        if ticket is None:
            issue = JiraTool().get_issue(key)
            ticket = Ticket(
                source="jira",
                external_key=key,
                title=issue["summary"],
                description=issue["description"],
                repos=body.repos,
            )
            db.add(ticket)
        else:
            ticket.repos = body.repos
        db.commit()
        saved_repos = ticket.repos

    logger.info("confirm_repos key=%r saved=%r", key, saved_repos)
    return JSONResponse(content={"saved": True, "repos": saved_repos})
