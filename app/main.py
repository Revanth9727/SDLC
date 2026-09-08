"""FastAPI application entry point.

Run with:
    uvicorn app.main:app --reload --port 8000
"""

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db.connection import SessionLocal
from app.db.models import Ticket
from app.tools.github_tool import GitHubTool
from app.tools.jira_tool import JiraTool

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
