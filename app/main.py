"""FastAPI application entry point.

Run with:
    uvicorn app.main:app --reload --port 8000
"""

import logging
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.db.connection import SessionLocal
from app.db.models import Ticket
from app.tools.github_tool import GitHubTool
from app.tools.jira_tool import JiraTool
from app.tools.repo_resolver import resolve_repos

logger = logging.getLogger(__name__)

_BASE = Path(__file__).parent

app = FastAPI(title="Agentic SDLC")

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
def ticket_resolve_repos(key: str) -> JSONResponse:
    """Run the repo-resolver cascade for a Jira issue key and return candidates."""
    result = resolve_repos(key)
    return JSONResponse(content=result)


class _ConfirmReposRequest(BaseModel):
    repos: list[str]


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
