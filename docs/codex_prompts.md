# codex_prompts.md — The Phased Build (Codex Prompts)

This is your build manual. **10 phases**, each split into **3+ sub-steps**. Every
sub-step has three parts:

- **PROMPT** — paste into Codex (after it has read `agent_context.md` +
  `ai_rules.md`).
- **SEE** — what you should be able to *see* working when the sub-step is done.
- **TEST** — the exact command/action that proves it, and the expected result.

**Rules for using this doc**
1. Do sub-steps **in order**. Do not start the next until SEE + TEST pass.
2. Before each Codex session, tell Codex: *"Read agent_context.md and ai_rules.md.
   Stay strictly in scope for the sub-step I give you. End by telling me what to
   run to see it work."*
3. If Codex adds out-of-scope code, reject it and re-prompt with the scope line.
4. "Seeing it work" is mandatory — if you can't see/test it, it's not done.

Phase map:
- **P1** Foundations you can see: DB models, FastAPI, submit a ticket, real Jira/GitHub reachable in-app.
- **P2** Live streaming: watch agent activity in real time (SSE).
- **P3** First real agent + the graph: Diagnosis runs on a real repo.
- **P4** Step-Planner + human approval gate (pause/resume).
- **P5** Executor with surgical edits (the cascade) + real PR.
- **P6** The guard system + failure exits + retry caps.
- **P7** Critic + Planner (full 6-agent flow, single sub-task).
- **P8** Multi-sub-task decomposition + isolation.
- **P9** Memory layer (pgvector).
- **P10** Parallelism, hardening, production-ready.

---

# PHASE 1 — Foundations You Can See

Goal: a running web app backed by Postgres where you submit a ticket and see it
persist, with Jira and GitHub reachable from inside the app. No agents yet.

## 1.1 Database models + connection

**PROMPT**
```
Create the database layer for the project.
- In app/db/, create connection.py that builds a SQLAlchemy engine + session
  from settings.database_url (see app/config.py). Use SQLAlchemy 2.0 style.
- Create models.py with two tables:
  * tickets: id (uuid pk), source ("manual"|"jira"), external_key (nullable),
    title, description, status ("new"|"processing"|"done"|"needs_human"),
    created_at.
  * subtasks: id (uuid pk), ticket_id (fk), type, description, status, depends_on
    (json list), state (jsonb — holds the full SubtaskState object), created_at.
- Create init_db.py with a function create_all() that creates the tables, and a
  __main__ block so `python -m app.db.init_db` runs it.
Follow ai_rules.md. End by telling me the exact command to create the tables and
how to confirm they exist in Postgres.
```

**SEE** — the `tickets` and `subtasks` tables existing in your Postgres.

**TEST**
```bash
python -m app.db.init_db
docker exec -it agentic_sdlc_db psql -U agentic -d agentic_sdlc -c "\dt"
# Expected: tickets and subtasks listed
```

## 1.2 FastAPI app + submit a ticket (persists to DB)

**PROMPT**
```
Create the FastAPI app in app/main.py.
- A GET "/" serving a minimal HTML page (Jinja2 template in app/web/templates/)
  with a form: title + description + a submit button, and a list showing all
  tickets from the DB (id, title, status).
- A POST "/tickets" that inserts a ticket (source="manual", status="new") and
  redirects back to "/".
- Wire Jinja2 templates and a static dir.
- Add a run note: `uvicorn app.main:app --reload --port 8000`.
Keep it minimal and clean. End by telling me how to run it and what I'll see.
```

**SEE** — a web page at localhost:8000 where you type a ticket, submit, and it
appears in the list.

**TEST**
```bash
uvicorn app.main:app --reload --port 8000
# open http://localhost:8000 , submit a ticket, see it appear in the list
docker exec -it agentic_sdlc_db psql -U agentic -d agentic_sdlc -c "SELECT title,status FROM tickets;"
# Expected: your submitted ticket row
```

## 1.3 Jira + GitHub tools (reachable in-app, tested)

**PROMPT**
```
Create two deterministic tools (no LLM) following ai_rules.md.
- app/tools/jira_tool.py: JiraTool with
  * list_open_issues() -> list of {key,summary,description,status}
  * get_issue(key) -> {key,summary,description,status}
  * comment(key, body) -> None
  Uses httpx + basic auth (settings.jira_email, settings.jira_api_token) against
  settings.jira_base_url REST v3.
  (Status transitions are added in 1.4 — leave a TODO hook, don't build yet.)
- app/tools/github_tool.py: GitHubTool using PyGithub with
  * get_repo(full_name) -> repo object for a given "owner/repo" (the repo is
    resolved per-ticket in 1.5, not hardcoded). Keep an allowlist hook for safety,
    but do NOT hardcode a single repo. For local testing you may default to
    settings.github_repo (the sandbox) when no repo is passed.
  * create_branch(full_name, base, new_branch)
  * open_pr(full_name, branch, title, body) -> pr_url
- Add two temporary debug routes in main.py: GET "/debug/jira" returns the open
  Jira issues as JSON; GET "/debug/github" returns the repo name + default branch.
End by telling me the two URLs to open and what I should see.
```

**SEE** — `/debug/jira` shows your real Jira ticket(s); `/debug/github` shows your
sandbox repo name.

**TEST**
```bash
# with the server running:
curl -s localhost:8000/debug/jira | python -m json.tool     # your AGT-1 ticket
curl -s localhost:8000/debug/github | python -m json.tool   # your sandbox repo
```

## 1.4 Dynamic Jira status transitions (config-mapped, graceful)

**PROMPT**
```
Extend app/tools/jira_tool.py with DYNAMIC, config-mapped status transitions
(deterministic, no LLM), per ai_rules.md R-25 and architecture.md §5d.

Add to settings (pydantic-settings, from .env) a status map:
  JIRA_STATUS_IN_PROGRESS, JIRA_STATUS_AWAITING_APPROVAL, JIRA_STATUS_IN_REVIEW,
  JIRA_STATUS_BLOCKED, JIRA_STATUS_DONE   (all optional, string names)
plus an optional JIRA_STATUS_FALLBACKS map (internal_stage -> alternate name).

Add methods:
  * get_transitions(key) -> list of {id, name} from Jira's
    GET /rest/api/3/issue/{key}/transitions  (the ALLOWED transitions from the
    issue's CURRENT status — never assume the whole workflow).
  * set_status(key, internal_stage) where internal_stage is one of:
    "in_progress" | "awaiting_approval" | "in_review" | "blocked" | "done".
    Logic:
      1. resolve the desired status NAME from the config map for that stage
      2. fetch get_transitions(key)
      3. match desired name case-insensitively against available transition names;
         if no match, try the configured fallback name; if still none, LOG a
         warning and RETURN without error (never raise, never block work — R-11).
      4. on match, POST the transition id.
    Return {applied: bool, from: <old>, to: <new_or_none>, reason: <if skipped>}.

Add a temporary debug route: POST "/debug/jira/status" body {key, stage} that
calls set_status and returns the result dict, so I can watch transitions happen.
Emit a structured log line for every attempt (applied or skipped).
End by telling me how to move a real ticket's status from the app and see it on
the Jira board.
```

**SEE** — you POST a stage, refresh your Jira board, and the ticket's status
actually changed. A non-existent status logs a clear "skipped, no matching
transition" instead of crashing.

**TEST**
```bash
# with the server running and a real ticket key (e.g. AGT-1):
curl -s -X POST localhost:8000/debug/jira/status \
  -H 'content-type: application/json' \
  -d '{"key":"AGT-1","stage":"in_progress"}' | python -m json.tool
# Expected: {"applied": true, "from": "To Do", "to": "In Progress", ...}
# Then check the Jira board — the ticket moved.

# Now test graceful failure with a stage whose status you did NOT configure:
curl -s -X POST localhost:8000/debug/jira/status \
  -H 'content-type: application/json' \
  -d '{"key":"AGT-1","stage":"blocked"}' | python -m json.tool
# Expected (if "Blocked" not in your workflow): {"applied": false,
#   "reason": "no matching transition for 'Blocked'"} — and NO crash.
```

**Note on wiring (used from Phase 3 onward):** the Orchestrator calls `set_status`
at each stage boundary — `in_progress` when work starts, `awaiting_approval` at the
human gate, `in_review` when the PR opens, `blocked` on guard escalation, `done` on
completion. You build those call-sites as each stage is added; here you only build
and prove the tool.

## 1.5 Repo resolution from the ticket (cascade + confirm)

**PROMPT**
```
Build the repo-resolver as a deterministic tool + a confirm gate, per
architecture.md §5c and ai_rules.md R-26. No LLM in the resolver.

app/tools/repo_resolver.py: resolve_repos(issue_key) -> ResolveResult with a
CASCADE (stop at first that yields candidates):
  1. web/remote links: GET the issue's remote links
     (/rest/api/3/issue/{key}/remotelink) and any URLs in issue links; extract
     github.com/owner/repo -> owner/repo (regex, dedupe).
  2. description: scan the issue description text for github.com/owner/repo URLs.
  3. reporter repos (BEST-EFFORT): if a GitHub username is derivable, list a few of
     their repos as candidates; if messy/unavailable, SKIP to step 4 (do not block).
  4. none found: return status="needs_paste" so the UI can ask for a URL.
Return {source: links|description|reporter|needs_paste, candidates: [owner/repo...]}.

Confirm gate: candidates are ALWAYS confirmed before proceeding (even a single
match). Add UI + endpoints:
  * POST "/ticket/{key}/resolve-repos" -> runs resolver, returns candidates (or
    needs_paste), streams an event.
  * POST "/ticket/{key}/confirm-repos" body {repos:[...]} -> stores the confirmed
    repo list on the ticket record; this is the human-gate confirmation.
Validate each owner/repo is reachable via GitHubTool.get_repo before accepting.
Store confirmed repos on the ticket row (repos TEXT[]).
End by telling me how to: link a repo on a Jira ticket, run resolve, see the
candidate, confirm it, and see it saved.
```

**SEE** — you add a GitHub web link to a Jira ticket, click resolve in the UI, see
the repo detected, confirm it, and see it saved on the ticket. Then test the
no-link path and get asked to paste.

**TEST**
```bash
# 1. On a Jira ticket, add a Web link to https://github.com/<you>/agentic-sdlc-sandbox
# 2. Resolve:
curl -s -X POST localhost:8000/ticket/AGT-1/resolve-repos | python -m json.tool
#    Expected: {"source":"links","candidates":["<you>/agentic-sdlc-sandbox"]}
# 3. Confirm:
curl -s -X POST localhost:8000/ticket/AGT-1/confirm-repos \
  -H 'content-type: application/json' \
  -d '{"repos":["<you>/agentic-sdlc-sandbox"]}' | python -m json.tool
#    Expected: {"saved": true, "repos": ["<you>/agentic-sdlc-sandbox"]}
# 4. No-link ticket -> resolve returns {"source":"needs_paste","candidates":[]}
```

**Note (used in Phase 7+):** the Planner reads a ticket's confirmed `repos` and
assigns each sub-task its repo during decomposition. There is NO separate
repo-matching agent (R-26). You build that assignment when the Planner lands.

## 1.6 Jira poller + atomic claiming (scheduled intake)

**PROMPT**
```
Build the scheduled Jira poller as a deterministic background job (no LLM), per
architecture.md §5a and ai_rules.md R-27.

- app/core/poller.py: a scheduler (use asyncio task or APScheduler) running every
  settings.jira_poll_interval_minutes (default 30).
- Each cycle:
  1. Guard: if the previous cycle is still running, skip this tick (no overlap).
  2. Query Jira for issues in status "To Do" (the ready signal) in the project.
  3. For each, CLAIM atomically BEFORE any work:
       a. skip if already in the local claims table (tickets.claimed_at set)
       b. insert/mark the ticket row claimed with claimed_at = now
       c. flip Jira status to in_progress via JiraTool.set_status (R-25)
  4. Process claimed tickets SEQUENTIALLY for now (parallelism is Phase 10) — for
     this phase, "process" just means: create the ticket record and emit an event
     "claimed" (real agent flow arrives in later phases).
- Add a manual trigger endpoint POST "/poll/run-once" so I can force a cycle
  without waiting 30 min, and a UI button "Run poll now".
- Emit a structured event per claimed ticket so it shows in the UI.
Never re-claim an in-flight ticket; never touch non-"To Do" tickets.
End by telling me how to create a To Do ticket, trigger a poll, and watch it get
claimed (UI + Jira board flipping to In Progress).
```

**SEE** — create a Jira ticket (status To Do), click "Run poll now" (or wait for
the interval), and watch it appear as *claimed* in the UI while the ticket flips to
**In Progress** on your Jira board. Trigger a second poll — it is NOT re-claimed.

**TEST**
- Create 2 tickets in **To Do** and 1 in **In Review**.
- Click "Run poll now."
- Expected: both To Do tickets get claimed (appear in UI, move to In Progress on the
  board); the In Review ticket is **untouched**.
- Click "Run poll now" again → already-claimed tickets are NOT picked up again.

**Note (Phase 10):** claimed tickets are processed one at a time here. True parallel
processing of independent tickets is enabled in Phase 10.

**Phase 1 done when:** you can submit a manual ticket and see it in the DB; Jira and
GitHub respond with real data in-app; you can move a Jira status from the app
(missing statuses skipped gracefully); you can resolve + confirm a repo linked on a
ticket (paste fallback when none); and the poller claims **To Do** tickets on a
trigger — flipping them to In Progress, skipping non-To-Do and already-claimed ones.

---

# PHASE 2 — Live Streaming (Watch It Happen)

Goal: an SSE channel so the browser shows real-time events. This is the
"see it happen" backbone every later phase plugs into.

## 2.1 Event bus + SSE endpoint

**PROMPT**
```
Add a live event system.
- app/events.py: a simple in-process async pub/sub. publish(ticket_id, event)
  where event = {agent, stage, message, ts}; and subscribe(ticket_id) async
  generator yielding events for that ticket.
- In main.py: GET "/stream/{ticket_id}" using sse-starlette EventSourceResponse
  that streams events for that ticket as they're published.
Follow ai_rules.md (events are the audit + stream source). End by telling me how
to test the stream with a manual publish.
```

**SEE** — an SSE endpoint that emits events.

**TEST**
```bash
# add a temporary GET "/debug/emit/{ticket_id}" that publishes 3 fake events,
# then in one terminal:
curl -N localhost:8000/stream/test123
# in another:
curl localhost:8000/debug/emit/test123
# Expected: the 3 events appear live in the first terminal
```

## 2.2 Live UI panel

**PROMPT**
```
On the ticket page, add a live activity panel.
- When viewing a ticket (GET "/tickets/{id}"), render the ticket details and a
  panel that opens an EventSource to /stream/{id} and appends each event as a row
  (agent • stage • message • time).
- Style it simply so the newest event is visible. No framework — plain JS.
End by telling me how to see events stream into the page.
```

**SEE** — a ticket page where events appear live as colored rows.

**TEST** — open a ticket page, hit the debug emit route, watch rows appear
without refresh.

## 2.3 Wire events into the DB state (persistence)

**PROMPT**
```
Make events durable: every published event is also appended to the ticket's/
subtask's state.events in the DB, so a page reload shows history, then continues
streaming live. Add a helper log_event(ticket_id, subtask_id, agent, stage, msg)
that both publishes (2.1) and persists. End by telling me how to confirm events
survive a reload.
```

**SEE** — reload the ticket page; past events are still there, new ones still
stream.

**TEST** — emit events, reload page, confirm history present; emit more, confirm
live append.

**Phase 2 done when:** you can watch time-ordered events stream live into a
ticket page and they persist across reloads.

---

# PHASE 2.5 — The Supervisor: Sync & Reconciliation (Jira is the Truth)

Goal: turn the app into a supervisor that watches tickets continuously — the DB is a
cache, Jira is the truth. Every cycle it reconciles tickets in scope against real Jira
status so a human changing status (or reopening a closed ticket) always re-triggers the
app; detects stuck tickets and comments (history-aware, @mentioning the owner); and
escalates through a cascade that CANNOT fail even if statuses are unmapped. Fixes the
"already claimed → ignored forever" friction directly.

> **Build async from here (R-35).** The supervisor is I/O-heavy (many Jira/GitHub/DB
> calls). Make these calls `async` (`httpx.AsyncClient`, async DB) so the poller can
> handle independent tickets concurrently without blocking. When Codex builds 2.5.x,
> tell it to use async I/O throughout — this is the concurrency foundation, in Python,
> no second language needed.

## 2.5.1 Reconciliation loop (Jira status wins)

**PROMPT**
```
Add a reconciliation step that runs at the START of every poll cycle, before
claiming, per architecture.md §5b and ai_rules.md R-28. Deterministic, no LLM.

app/core/reconcile.py: reconcile() ->
  For EVERY ticket in scope (active local run OR Jira ticket updated since last
  sweep — this is what makes REOPENED old tickets re-enter automatically), fetch its
  CURRENT Jira status and resolve drift:
    - local claimed BUT Jira status == "To Do"  -> clear claim
      (claimed_at=NULL, status='new') so the next claim step re-picks it.
    - local shows an active run BUT Jira status in {Done, <blocked>} -> mark the
      run stopped/superseded (human overrode).
    - Jira "In Progress" with NO local claim -> record as human-owned (do not claim).
    - a ticket that was resolved/closed and is now back to an active status
      (REOPENED) -> treat as active again; never error because it was closed before.
    - Jira status has NO mapping in config (UNKNOWN) -> run the ESCALATION CASCADE
      (below) AND set the ticket status to needs_human. Never silently ignore.
    - matches -> no-op.
  ESCALATION CASCADE (must never fail on missing config): always post a Jira comment;
  @mention the owner (assignee else reporter); TRY set a human/blocked status but skip
  silently if unmapped (R-25); (email deferred). Even with no status mapping, the
  comment + @mention reach a human.
  ISOLATION: read/write ONLY this ticket's own row + events. Never query across
  tickets. Emit a structured event per reconciled change so it streams to the UI.
  Track last_sweep_at so the next cycle only re-scans tickets updated since then.
Wire reconcile() to run first in the poller cycle (before the claim scan).
End by telling me how to: claim a ticket, drag it back to To Do in Jira, run poll,
and watch it get re-picked automatically (no manual SQL).
```

**SEE** — claim SCRUM-x, drag it back to **To Do** on the Jira board, click "Run
poll now" → it is re-claimed automatically and moves back to In Progress. No DB
command needed.

**TEST**
- Claim a ticket (poll once; it goes In Progress).
- On the Jira board, drag it back to **To Do**.
- Click "Run poll now."
- Expected: reconcile clears the stale claim, the claim step re-picks it, it returns
  to In Progress. Confirm in the UI events and the board — no manual SQL.
- **Unknown-status test:** drag a ticket to a status you did NOT map in `.env` (add a
  new column/status in Jira like "On Hold"). Run poll → the escalation cascade fires:
  a comment appears @mentioning the owner, and the ticket is flagged needs_human — and
  it does NOT crash even though the status is unmapped.
- **Reopen test:** take a Done ticket, reopen it (drag to To Do / In Progress). Run
  poll → it re-enters scope and is handled as active again, no error.

## 2.5.2 Who's-in-control detection

**PROMPT**
```
Add ownership detection used by reconciliation and stuck-detection, per
architecture.md §5b. Deterministic.
- app/core/ownership.py: owner_of(ticket) -> "ai" | "human" | "none".
  Rule: if there is an ACTIVE AI run for the ticket in the DB -> "ai";
  else if Jira status is In Progress with no active run -> "human";
  else "none".
- Add a debug route GET "/debug/ownership/{key}" returning the owner, so I can see
  it classify a ticket.
End by telling me how to check ownership for an AI-claimed vs a human-moved ticket.
```

**SEE** — for a ticket the app claimed, ownership = "ai"; for one you moved to In
Progress by hand in Jira, ownership = "human".

**TEST**
```bash
# app-claimed ticket:
curl -s localhost:8000/debug/ownership/SCRUM-1 | python3 -m json.tool   # -> "ai"
# a ticket YOU dragged to In Progress in Jira, never claimed by app:
curl -s localhost:8000/debug/ownership/SCRUM-2 | python3 -m json.tool   # -> "human"
```

## 2.5.3 Stuck detection + smart comment

**PROMPT**
```
Add stuck detection to the poll cycle, per architecture.md §5b and ai_rules.md
R-28. Config: STUCK_THRESHOLD_MINUTES (default 120) in settings.
For each ticket in ANY non-terminal status (category != "done" — includes In Progress
AND parked statuses like On-Hold) longer than the threshold:
  1. READ THAT TICKET'S OWN history/events first (isolation: only this ticket's
     data, never another's) to determine what stage it reached and what happened.
  2. Branch on owner_of():
     - "ai"    -> post a Jira comment STATING what completed and where it stuck
                  (from the history, e.g. "diagnosis done; stuck applying fix at
                  step 3"), NOT a generic message. DO NOT change the Jira status.
                  @mention the owner.
     - "human" -> post a Jira comment asking what's blocking, referencing what's
                  already been done. @mention the owner.
     - healthy in-flight AI run under threshold -> do nothing (never nag).
Guard against duplicate comments: only comment once per stuck episode (track a
last_stuck_comment_at on the ticket; don't re-comment until it clears/changes).
Emit an event per comment. End by telling me how to force a stuck ticket (lower the
threshold) and watch the correct, history-aware comment appear on the Jira issue.
```

**SEE** — set `STUCK_THRESHOLD_MINUTES=1`, leave a ticket In Progress a minute, run
poll → a comment appears on the **real Jira issue**, worded for AI-stuck vs
human-stuck, and it doesn't spam repeat comments.

**TEST**
- In `.env`, set `STUCK_THRESHOLD_MINUTES=1`, restart the app.
- Have one AI-claimed ticket and one you moved to In Progress by hand; wait ~1 min.
- Click "Run poll now."
- Expected: the AI-owned one gets a "agent appears blocked" comment; the human-owned
  one gets a "what's blocking?" comment. Run poll again → NO duplicate comments.

**Phase 2.5 done when:** dragging a ticket back to To Do auto-re-picks it (no SQL); a
reopened closed ticket re-enters scope without error; an unmapped status triggers the
escalation cascade (comment + @mention + needs_human) and never crashes; ownership is
correctly classified; and stuck tickets get ONE history-aware comment, @mentioning the
owner, reading only that ticket's own data.

---

# PHASE 3 — First Real Agent + The Graph

Goal: a LangGraph graph with one real agent (Diagnosis) that reads your sandbox
repo and produces a structured root-cause — streamed live.

## 3.1 The LLM wrapper + SubtaskState

**PROMPT**
```
Create the LLM access layer and the core state model.
- app/agents/llm.py: a single LLMClient wrapping OpenAI. Methods: complete(system,
  user, tier="strong"|"cheap") and complete_json(system, user, schema: pydantic
  model, tier=...) returning a validated instance (retry once on validation error).
  MODEL TIERING (R-33): tier "strong" -> settings.MODEL_STRONG, "cheap" ->
  settings.MODEL_CHEAP; each agent passes its tier, NO agent hardcodes a model name.
  NO agent calls OpenAI directly (ai_rules R-11).
  COST/CALL TRACKING (R-34): every call records tokens + estimated cost against the
  current ticket_id; expose get_usage(ticket_id) -> {calls, tokens, est_cost_usd}.
- app/agents/router.py: a DETERMINISTIC model router (plain rules, NO LLM) mapping
  task type -> tier, per R-33. E.g. {diagnosis: strong, planner(ambiguous): strong,
  step_planner: cheap, executor_apply: cheap, parsing: cheap, routing: cheap,
  critic: strong}. Agents ask the router for their tier instead of hardcoding.
  Design principle (plan-then-execute): the STRONG model produces specs detailed
  enough that CHEAP models execute without searching/reasoning.
- app/agents/state.py: the SubtaskState Pydantic model exactly as in
  architecture.md §5 (the blackboard) — all fields, including `repo` and the
  control fields (retry_count, budget_used, status, failure_reason).
End by telling me how to run a quick script that makes a cheap-tier and a strong-tier
call and prints the usage totals.
```

**SEE** — a validated structured response from OpenAI via your wrapper.

**TEST**
```bash
python - <<'EOF'
from app.agents.llm import LLMClient
from pydantic import BaseModel
class Ping(BaseModel):
    word: str
c = LLMClient()
print(c.complete_json("You reply in JSON.", "give {\"word\":\"pong\"}", Ping))
EOF
# Expected: word='pong'
```

## 3.2 GitHub read tool (clone/read repo files)

**PROMPT**
```
Extend app/tools/github_tool.py (or a new repo_tool.py) with read capability:
- clone_or_pull(full_name, subtask_id) -> local path to a FRESH SHALLOW checkout
  (git clone --depth 1) of the given "owner/repo" default branch, into an ISOLATED
  per-sub-task dir (e.g. /tmp/agentic-workspaces/{subtask_id}/). Pull fresh if reused
  within an active run. The repo comes from the sub-task's `repo` field (assigned by
  the Planner), NOT hardcoded — for this early phase you may pass the sandbox repo
  explicitly while the Planner isn't built yet. Per architecture.md §8a and R-39.
- cleanup_workspace(subtask_id): delete the sub-task's workspace dir. Called when the
  sub-task reaches a resting state (PR opened / escalated / abandoned) — NOT mid retry
  loop. Re-clone fresh on any reopen.
- list_files(full_name) and read_file(full_name, path) within that checkout.
These are deterministic tools. End by telling me how to print the repo's file list
and app.py contents, and how to confirm the workspace is deleted after cleanup.
```

**SEE** — your sandbox repo's files (including the buggy `app.py`) readable in-app.

**TEST**
```bash
python - <<'EOF'
from app.tools.repo_tool import RepoTool
r = RepoTool(); repo = "YOUR_USER/agentic-sdlc-sandbox"
r.clone_or_pull(repo)
print(r.list_files(repo))
print(r.read_file(repo, "app.py"))
EOF
# Expected: file list incl app.py, and the buggy divide() source
```

## 3.3 Diagnosis agent inside a minimal graph

**PROMPT**
```
Build the first agent and a minimal LangGraph.
- app/agents/diagnosis.py: DiagnosisAgent. Input: a SubtaskState (with
  description) + repo access. It reads relevant files (use list_files/read_file;
  simple keyword shortlist is fine) and returns a Pydantic Diagnosis
  {root_cause, files: list[str], reasoning} via llm.complete_json. It MUST have a
  failure exit: if it can't find a cause, return Diagnosis with root_cause="" and
  a NoRootCause flag -> status needs_human (ai_rules R-8 (loop limits + human exit)).
- app/orchestrator/graph.py: a LangGraph with a Postgres checkpointer and, for
  now, a single node running DiagnosisAgent, writing the result into SubtaskState
  and log_event-ing start/finish.
- Add POST "/tickets/{id}/diagnose" that creates one subtask from the ticket,
  runs the graph, and streams events.
End by telling me how to submit the divide-by-zero ticket and watch Diagnosis run
live and produce a root cause.
```

**SEE** — submit the divide-by-zero ticket, watch a "Diagnosis" event stream in,
then see a structured root cause naming `app.py` / `divide`.

**TEST** — via UI: create ticket "divide by zero crashes app", click diagnose,
watch the live event, then see the stored diagnosis. Confirm in DB:
```bash
docker exec -it agentic_sdlc_db psql -U agentic -d agentic_sdlc -c "SELECT state->'diagnosis' FROM subtasks;"
```

**Phase 3 done when:** a real agent reads your real repo and produces a validated
root cause, streamed live, with a failure exit if it can't.

---

# PHASE 4 — Step-Planner + Human Approval Gate

Goal: after diagnosis, plan concrete steps, **pause for your approval**, and
resume — the checkpointed human-in-the-loop gate.

## 4.1 Step-Planner agent

**PROMPT**
```
Add app/agents/step_planner.py: StepPlannerAgent. Input: SubtaskState with a
diagnosis. Output: a validated Plan = list of Step {step_id, intent,
target_file}. Failure exit: if it cannot form a plan, return CannotPlan ->
needs_human. Add it as the next node after Diagnosis in the graph, writing plan
into state and emitting events. End by telling me how to see a plan produced for
the divide-by-zero ticket.
```

**SEE** — after diagnosis, a step list appears (e.g. "add zero-check in divide").

**TEST** — run the flow; confirm `state->'plan'` populated in DB and events show
the Step-Planner.

## 4.2 The human gate (pause + persist)

**PROMPT**
```
Add a human approval gate after Step-Planner using LangGraph's interrupt().
- When reached, set approval_status="pending", set Jira status to
  `awaiting_approval` via JiraTool.set_status (R-25), post the plan+reasoning as a
  Jira comment (JiraTool.comment) AND emit a "needs approval" event, then interrupt
  so the graph pauses with state checkpointed.
- The UI ticket page shows the pending plan with Approve / Reject buttons.
Follow ai_rules (checkpointed, resumable). End by telling me how to see the flow
pause and wait.
```

**SEE** — the flow runs to the plan, then **stops**, showing the plan with
Approve/Reject buttons and posting a comment to your real Jira ticket.

**TEST** — run flow; confirm it halts at "pending", a comment appears on the Jira
issue, and the graph state is checkpointed (still there after a server restart).

## 4.3 Resume on approval

**PROMPT**
```
Add POST "/tickets/{id}/subtasks/{sid}/approve" and ".../reject" that set
approval_status and resume the LangGraph run from the checkpoint. On approve: set
Jira status back to `in_progress` (work resuming) and continue (for now, the next
node is a placeholder that just logs "would execute"). On reject: status ->
needs_human with the note, and set Jira status to `blocked` (R-25). End by telling
me how to approve and watch it resume.
```

**SEE** — click Approve, watch the flow resume live past the gate.

**TEST** — pause the flow, restart the server (proving persistence), then approve;
confirm it resumes from where it paused, not from the start.

**Phase 4 done when:** the system plans, pauses for real approval (surviving a
restart), and resumes on your click.

---

# PHASE 5 — Executor + Surgical Edits + Real PR

Goal: on approval, make surgical code edits via the cascade, run tests, open a
real PR on your sandbox repo.

## 5.1 The edit applier (deterministic cascade) — unit tested first

**PROMPT**
```
Build the surgical edit engine as a deterministic tool (no LLM), per
architecture.md §8 and ai_rules E (surgical edits, R-14–R-16).
- app/tools/edit_applier.py: apply_edits(file_text, blocks) where each block is
  {search, replace}. Implement the cascade: exact -> whitespace-normalized ->
  fuzzy (difflib SequenceMatcher >= 0.8). Enforce EXACTLY ONE match per block
  (0 -> raise NoMatch with closest-lines hint; >1 -> raise Ambiguous asking for
  more context). Multi-block: resolve positions on the unmodified text, apply
  bottom-up. Pre-strip a spurious leading blank line. Return new_text + a report.
- app/tools/edit_guard.py: post-apply check (syntax via compile() for .py,
  line-count variance, empty/truncation). 
- tests/test_edit_applier.py: unit tests for exact, whitespace-off, fuzzy,
  no-match, ambiguous, multi-block bottom-up.
End by telling me how to run the tests and see them pass.
```

**SEE** — a tested edit engine; green tests.

**TEST**
```bash
pytest tests/test_edit_applier.py -v
# Expected: all pass, covering each cascade tier + fail-closed cases
```

## 5.2 Executor agent (LLM emits blocks, applier applies)

**PROMPT**
```
Add app/agents/executor.py: ExecutorAgent. For each approved Step: read the
target file, ask the LLM (complete_json) for SEARCH/REPLACE blocks for that step,
apply via edit_applier, run edit_guard, then run the test runner tool
(app/tools/test_runner.py: runs `pytest` in the checkout, returns pass/fail +
output — fixed bounded command, never LLM-authored). On NoMatch/Ambiguous/guard
failure: feed the error back to the LLM and retry up to MAX_AGENT_RETRIES, then
escalate to human (ai_rules R-8, R-9). Emit events for every step + edit + test.
Add it as the node after the gate. End by telling me how to watch it edit app.py
and run tests live.
```

**SEE** — after approval, watch the Executor produce an edit to `app.py`, apply
it, and run tests — all streaming live.

**TEST** — approve the divide-by-zero fix; confirm the local checkout's `app.py`
now has a zero-check and tests run. Inspect `state->'steps_done'`.

## 5.3 Open the real PR

**PROMPT**
```
Add the PR node: after Executor succeeds, GitHubTool creates a branch on the
sub-task's `repo`, commits the changed files, pushes, opens a PR, writes pr_url
into state, comments the PR link on the Jira issue, and sets Jira status to
`in_review` via set_status (the PR is open awaiting human merge — NOT done yet;
`done` is only after merge, a later phase). Idempotent: don't open a duplicate PR
on retry (ai_rules R-19 (never touch main; PR only)). End by telling me how to see
the real PR.
```

**SEE** — a **real pull request** on your sandbox GitHub repo, linked back in the
Jira comment.

**TEST** — run the full flow; open the PR URL from the UI; confirm the diff adds
the zero-check; confirm the Jira issue has the PR link.

**Phase 5 done when:** ticket → diagnosis → plan → approve → surgical edit →
tests → **real PR**, end to end, streamed live.

---

# PHASE 5.5 — Event-Driven Fast Path (Webhooks)

Goal: react instantly to GitHub/Jira changes via webhooks, instead of waiting for the
next poll. Polling stays the reliable fallback; webhooks are the fast path. Built now
because PRs exist (Phase 5) so there are real PR events to react to. All Python/FastAPI —
no Go.

## 5.5.1 Webhook receivers + idempotency + linkage

**PROMPT**
```
Add FastAPI webhook endpoints, per architecture.md §5b and ai_rules.md R-37. Python
only, no separate service.
- POST /webhooks/github and POST /webhooks/jira.
- Verify the webhook signature/secret (settings.GITHUB_WEBHOOK_SECRET,
  JIRA_WEBHOOK_SECRET).
- IDEMPOTENCY: read the delivery id (X-GitHub-Delivery / Jira delivery id); if seen
  before (webhook_deliveries table) -> return 200 and DROP. Else record it.
- KEY EXTRACTION: from a GitHub PR event, parse the Jira key from PR title/branch/
  commits with regex (?i)[A-Z]{2,10}-\d+.
- LINKAGE: pr_links table (github_pr_id, jira_issue_key, pr_state, updated_at).
- Emit a streamed event per received webhook so it shows live in the UI.
End by telling me how to register the webhook in GitHub/Jira (smee.io or ngrok for
local) and watch an event arrive live.
```

**SEE** — open a PR (or move a ticket) and watch the webhook arrive live in the UI;
send the same delivery twice and watch the duplicate get dropped.

**TEST**
- Use smee.io or ngrok to expose localhost; register the webhook in GitHub.
- Open a PR whose branch/title contains a Jira key -> confirm the event arrives, the
  key is extracted, and a pr_links row is created.
- Replay the same delivery id -> confirm it's dropped (idempotent).

## 5.5.2 PR-state matrix (ticket/PR transition handling)

**PROMPT**
```
Implement the PR-state matrix on webhook events, per architecture.md §5b (the table)
and ai_rules.md R-37. Reuse the existing status-sync + comment tools.
- PR opened -> link; move ticket To Do->In Progress; comment "PR #N opened".
- PR merged -> mark link DONE; move ticket -> Done.
- Jira ticket reopened (Done->active): look up linked PRs and branch:
    * linked PR MERGED  -> DO NOT reopen the PR; comment on the ticket "reopened but
      PR #N already merged — a new PR/branch is likely needed".
    * linked PR CLOSED-unmerged -> leave it; post a context comment.
    * linked PR OPEN -> comment on the PR "ticket moved back to In Progress".
- Branch deleted / PR closed no-merge -> mark link ABANDONED; context comment on ticket.
All actions go through the SAME status-sync (R-25) and reconcile with the polling
supervisor. Emit events. End by telling me how to trigger each row and see the right
action.
```

**SEE** — merge a PR → its ticket moves to Done live. Reopen a ticket whose PR was
merged → a "new PR needed" comment appears, and the merged PR is NOT touched.

**TEST** — walk each matrix row: open/merge a PR, reopen a ticket with a merged vs.
open PR; confirm the exact action from the table happens and nothing un-merges a merged
PR.

**Phase 5.5 done when:** GitHub/Jira webhooks drive instant, idempotent, correctly-keyed
ticket/PR updates through the PR-state matrix — with polling still the fallback and a
merged PR never un-merged.

## 5.5.3 Approve/Reject from Jira comments (second gate channel)

**PROMPT**
```
Add Jira-comment approval as a SECOND input to the existing human gate (the first is
the UI button). Reuses the comment_created webhook from 5.5.1. Per ai_rules.md R-30
(never change without approval) and R-38.
- pending_approvals table: id, jira_issue_key, proposed_action, status
  ENUM('PENDING','APPROVED','REJECTED','EXPIRED'), jira_comment_id, requested_at,
  resolved_at. Partial index on (jira_issue_key) WHERE status='PENDING'.
- When the flow reaches a human gate, in addition to the UI prompt, post a Jira
  comment: "Reply APPROVE or REJECT to authorize: <the proposed action>", and insert
  a PENDING row.
- On comment_created webhook:
    * ignore comments that don't start (case-insensitive) with APPROVE or REJECT
      (noise prevention).
    * PERMISSION CHECK (required): verify the comment author is allowed (assignee or a
      configured allow-list / project role). If not allowed -> post "you're not
      authorised to approve this" and IGNORE. This is essential — an approval anyone
      can trigger is not an approval.
    * find the latest PENDING approval for that issue; APPROVE -> mark APPROVED, RESUME
      the paused LangGraph run (same resume path as the UI button); REJECT -> mark
      REJECTED, route needs_human, no code change.
    * post a confirmation comment naming who approved/rejected.
- Both channels (UI button + comment) drive the SAME gate/resume — never double-apply.
End by telling me how to reach a gate, approve it by replying APPROVE in Jira, and
watch the flow resume — and how an unauthorised commenter is refused.
```

**SEE** — a ticket pauses at the gate; you reply "APPROVE" on the Jira issue; the flow
resumes live (same as clicking the UI button). A reply from an unauthorised user is
refused with a comment and nothing happens.

**TEST**
- Reach a human gate. Reply `APPROVE` on the Jira ticket → confirm the run resumes and
  a confirmation comment posts.
- On another, reply `REJECT` → confirm it routes to needs_human, no code change.
- Reply "looks good maybe?" (not APPROVE/REJECT) → confirm it's ignored (noise).
- Reply APPROVE as a non-authorised user → confirm it's refused.

**Phase 5.5 (with approvals) done when:** the human gate can be answered from either the
UI button or a permission-checked Jira comment, both driving the same resume, with noise
and unauthorised replies safely ignored.

## 5.5.4 Rich comment monitoring (classify + route intent)

**PROMPT**
```
Extend the Jira comment_created webhook to READ and classify ALL human comments, not
just APPROVE/REJECT. Per ai_rules.md R-44.
Classify each human comment's intent (one cheap LLM call, MODEL_CHEAP):
  - COMMAND (stop / redo differently / also do X): turn it into a PROPOSAL and route
    it through the normal human gate + approval — NEVER auto-act (R-30). A comment
    requests; it does not override.
  - QUESTION (why this repo? what's the status?): answer it with a comment back,
    reading only that ticket's own state.
  - CHATTER (discussion, thanks, looks good): ignore.
GUARDRAILS (all required):
  - Only ACT on comments from AUTHORISED users (permission check, reuse R-38's check).
  - NEVER treat the tool's OWN comments as instructions — filter out comments whose
    author is the tool/bot account (prevents self-triggering loops).
  - If intent is AMBIGUOUS, post a clarifying question instead of acting (R-10).
  - Idempotent: process each comment delivery once.
Emit an event per classified comment (intent + action taken). End by telling me how
to: comment a command and see it become a gated proposal, comment a question and get
an answer, and confirm the tool ignores its own comments and unauthorised users.
```

**SEE** — comment "also handle the divide function" → it appears as a new gated
proposal (not auto-done). Comment "why did you pick this repo?" → the tool replies.
Comment "thanks!" → ignored. The tool never reacts to its own comments.

**TEST**
- As an authorised user, comment a COMMAND → confirm it becomes a proposal at the gate,
  not an immediate change.
- Comment a QUESTION → confirm the tool answers.
- Comment CHATTER → confirm ignored.
- Comment as an unauthorised user → confirm not acted on.
- Confirm the tool's own step-narration comments don't trigger it.

**Phase 5.5 (full) done when:** the tool reads human comments, routes commands through
the gate (never auto-acting), answers questions, ignores chatter and its own comments,
and only acts on authorised users.

---

# PHASE 6 — Guards, Failure Exits, Retry Caps

Goal: make Principle A real everywhere — the system fails safely and visibly.

## 6.1 The Orchestrator guard step

**PROMPT**
```
Implement the guard that runs after every agent node (architecture.md §10 (deferred scope)):
validate output schema, check retry_count vs MAX_AGENT_RETRIES, check budget_used
vs TICKET_TOKEN_BUDGET, and detect clean failure-exit signals. On any trip: set
status=needs_human with failure_reason and route to a human node. Wire it between
every existing node. End by telling me how to see a forced failure route to human.
```

**SEE** — when something fails past the cap, the flow stops and the ticket shows
"needs human" with a reason, instead of looping.

**TEST** — temporarily force Diagnosis to return invalid output; confirm the
guard catches it, retries, then escalates with a visible reason.

## 6.2 Budget + runaway-loop protection

**PROMPT**
```
Accumulate LLM calls + estimated cost per ticket (from LLMClient.get_usage) into
the ticket record on every call. Add the runaway rule: if any loop hits
MAX_AGENT_RETRIES, stop and escalate (R-8). Enforce per-ticket budgets (R-34):
TICKET_CALL_BUDGET and TICKET_COST_BUDGET_USD from settings — on exceed, the guard
PAUSES and escalates to the human ("used X calls / $Y — continue?"), never spends
silently. Surface calls + cost live in the UI per ticket. End by telling me how to
see the budget climb and trip.
```

**SEE** — a live calls/cost counter on the ticket page; it trips to human if either
the call count or cost ceiling is exceeded.

**TEST** — set a tiny `TICKET_CALL_BUDGET` (e.g. 2) in `.env`, run a ticket, watch it
pause for human once the call budget is exceeded.

## 6.3 The "needs human" resolution UI

**PROMPT**
```
Build the human-escalation experience: a page listing all subtasks with
status=needs_human, each showing failure_reason and the state so far, with a
"retry" and a "reject" action. When a subtask escalates to needs_human, ALSO set
its Jira status to `blocked` via set_status (R-25) so the board reflects it.
Escalation is a first-class outcome (ai_rules R-8 (loop limits + human exit)).
End by telling me how to see and act on an escalated subtask.
```

> **Status lifecycle now complete across the flow** (all via set_status, R-25):
> claim → `in_progress` (1.6) · gate → `awaiting_approval` (4.2) · approve →
> `in_progress` (4.3) · PR open → `in_review` (5.3) · escalation → `blocked` (6.3) ·
> (merge → `done` is a later phase). Reject at the gate → `blocked` (4.3).

**SEE** — a queue of escalated items you can review and act on.

**TEST** — force an escalation; confirm it appears in the queue with context and
can be retried/closed.

**Phase 6 done when:** no loop can run away, every failure surfaces to a human
with context, and you can act on escalations.

---

# PHASE 7 — Critic + Planner (Full 6-Agent Flow, One Sub-task)

Goal: add the two remaining agents so the full pipeline exists for a
single-sub-task ticket.

## 7.1 Critic agent

**PROMPT**
```
Add app/agents/critic.py: CriticAgent. Input: the sub-task + the Executor's
change + test results. Output: validated CriticVerdict {approved: bool,
issues: list[str], verifiability: "ok"|"no_tests"|"uncovered_change"}.
Verifiability check (R-32): if the repo has NO tests -> verifiability="no_tests"
(the fix can't be meaningfully verified); if the change added code with no
coverage -> "uncovered_change". Surface these; do NOT silently pass an
unverifiable change. If not approved, route back to Executor with the issues
(counts toward MAX_AGENT_RETRIES; on cap -> escalate). Insert Critic between
Executor and the PR node. End by telling me how to watch the Critic accept/reject
a fix, and how it flags a repo with no tests.
```

**SEE** — after the Executor, a Critic reviews the change and either passes it to
PR or sends it back with specific issues — streamed live.

**TEST** — run a good fix (Critic passes → PR) and a deliberately bad step (Critic
rejects → loop → eventually escalates). Watch both live.

## 7.2 Planner agent (single sub-task for now)

**PROMPT**
```
Add app/agents/planner.py: PlannerAgent. Input: a ticket PLUS its confirmed
repo list (ticket.repos from 1.5). Output: validated list[SubtaskSpec]
{type, description, repo, depends_on}. The Planner ASSIGNS each sub-task exactly
one repo from the confirmed list (R-26) — this is the Planner's job, NOT a separate
agent. If a sub-task's repo is ambiguous (multiple repos, unclear which), the
Planner flags it -> needs_human at the gate rather than guessing (R-10). If the
ticket has exactly one confirmed repo, every sub-task gets that repo.

INTENT-CONFIRMATION GATE (R-30): before the decomposition is trusted, the Planner
posts its interpretation and PAUSES for confirmation — "I read this as: [list of
sub-tasks]. Correct?" — surfaced in the UI and as a Jira comment. Only on approval
does the flow proceed. This is a human gate (interrupt + resume), same mechanism as
the plan gate.

For now the flow still handles ONE sub-task (take the first), but the Planner runs
and records the full decomposition. Failure exit: CannotDecompose -> needs_human.
Put Planner at the very front of the graph (after repo resolution). Each sub-task's
`repo` flows into its SubtaskState and is used by Diagnosis/Executor/PR.
End by telling me how to see a ticket decomposed, confirm the interpretation, and
then watch it proceed.
```

**SEE** — a ticket now first shows a decomposition into sub-task(s), each with its
assigned repo, before the per-sub-task flow runs.

**TEST** — submit a single-issue ticket with one confirmed repo; confirm one
sub-task is produced with that repo assigned, and the full
Planner→Diagnosis→StepPlanner→gate→Executor→Critic→PR flow runs against it.

## 7.3 End-to-end full-flow verification

**PROMPT**
```
Add an integration test tests/test_full_flow.py that runs a single-sub-task
ticket through the whole graph with a stubbed LLM client returning canned valid
outputs (so it's deterministic and free), asserting: subtask created, diagnosis
set, plan set, gate reached, (auto-approve in test), edit applied, critic passed,
pr node called (mock GitHub). End by telling me how to run it green.
```

**SEE** — one command runs the entire pipeline deterministically and passes.

**TEST**
```bash
pytest tests/test_full_flow.py -v   # green: full pipeline, mocked externals
```

**Phase 7 done when:** all six agents exist and a single-sub-task ticket flows
through the complete pipeline, live and in an automated test.

---

# PHASE 7.5 — Solution Reuse (Don't Re-Solve What's Solved)

Goal: before running the expensive reasoning agents, check if a past resolved ticket
already solved this. On a strong match, skip Diagnosis + Step-Planner, propose the known
fix, and still verify it through the gate + Critic. Built now (not with the full memory
phase) because the full pipeline works, so resolved tickets exist to match against.

## 7.5.1 Minimal memory store + write-back on resolve

**PROMPT**
```
Add the minimal memory needed for reuse, per memory.md §4 and §8a. pgvector is
already available (setup.md).
- app/db/models.py: subtask_memory table (subtask_id, ticket_id, subtask_type,
  problem_summary, resolution_summary, files_touched, embedding VECTOR(1536),
  created_at) + a pgvector index.
- app/tools/memory.py:
  * embed(text) -> vector (OpenAI text-embedding-3-small; model pinned in config).
  * write_back(subtask, result): ONE LLM call to summarise problem+resolution, embed
    it, INSERT. Called after a PR is opened. Write-back failure must NOT fail the
    subtask (log + continue), per memory.md M-4.
End by telling me how to resolve a ticket and see a row appear in subtask_memory.
```

**SEE** — after a ticket reaches PR, a summarised row with an embedding appears in
`subtask_memory`.

**TEST**
```bash
docker exec -it agentic_sdlc_db psql -U agentic -d agentic_sdlc \
  -c "SELECT subtask_id, problem_summary, resolution_summary FROM subtask_memory;"
# Expected: a row for the ticket you just resolved.
```

## 7.5.2 The reuse gate (tiered, front of flow)

**PROMPT**
```
Add the solution-reuse gate at the FRONT of the per-subtask flow (after repo
resolution, before Diagnosis), per memory.md §8a and ai_rules.md R-29.
- app/tools/memory.py: search_similar(subtask) -> top-K resolved matches with
  similarity scores (pgvector cosine). Reads ONLY resolved-ticket summaries
  (isolation holds).
- In the graph: a gate node that runs search_similar. If the top match has
  similarity >= 0.9 (hardcoded STRONG bar):
    * mark the subtask reuse_source = that past ticket
    * take its resolution as a PROPOSED fix
    * run a freshness/applicability check (do the referenced files still exist /
      does the change still apply cleanly?)
    * route STRAIGHT to the human gate (SKIP Diagnosis + Step-Planner), with the
      gate clearly showing "reused from <KEY>, similarity X"
    * then continue normally: Executor applies, Critic verifies, PR.
  Else (weak/no match): fall through to the full pipeline (Diagnosis -> ...).
NEVER blind-apply: the gate + Critic still run. Emit an event showing whether reuse
fired and from which ticket.
End by telling me how to resolve a ticket, create a near-identical one, and watch it
skip Diagnosis/Step-Planner and reuse the known fix.
```

**SEE** — resolve a ticket (memory fills). Create a near-identical ticket → in the
live UI you watch it **skip Diagnosis and Step-Planner**, surface the known fix at the
gate labelled "reused from SCRUM-X (0.9x)", then still verify before the PR.

**TEST**
- Run a ticket fully to PR (populates memory).
- Create a new ticket describing essentially the same problem.
- Run it → confirm (in the streamed events) that Diagnosis and Step-Planner did NOT
  run, the gate shows the reused fix + source key, and Critic + PR still happened.
- Create a clearly DIFFERENT ticket → confirm it falls through to the full pipeline
  (Diagnosis runs).

**Phase 7.5 done when:** a near-duplicate ticket reuses a past solution (skipping the
two reasoning agents) while still passing through the human gate and Critic, and a
novel ticket still runs the full pipeline.

---

# PHASE 8 — Multi-Sub-task Decomposition + Isolation

Goal: handle tickets with multiple requests, each sub-task isolated, approved
per sub-task, respecting dependencies (sequential for now).

## 8.1 Multiple sub-tasks, sequential, isolated

**PROMPT**
```
Upgrade the Orchestrator to handle N sub-tasks from the Planner. Each sub-task
gets its OWN isolated SubtaskState and its own run through Diagnosis→StepPlanner→
gate→Executor→Critic. Enforce isolation: a sub-task's agents can only see their
own state (ai_rules C (failure & human exit)). Respect depends_on ordering (sequential). End by telling
me how to submit a 3-request ticket and watch each sub-task run isolated, in order.
```

**SEE** — a ticket like "fix divide-by-zero AND add a subtract() function"
produces two sub-tasks that run one after another, each isolated.

**TEST** — submit a 2–3 request ticket; confirm N sub-tasks, each with separate
state and its own approval gate; confirm order follows dependencies.

## 8.2 Per-sub-task approval UX

**PROMPT**
```
Update the UI so each sub-task shows its own plan + Approve/Reject, and the ticket
shows overall progress (e.g. 1/3 approved, 2/3 executing). Make the gate
per-sub-task as designed. End by telling me how to approve sub-tasks individually.
```

**SEE** — a ticket dashboard with each sub-task's gate and status.

**TEST** — approve sub-tasks one at a time; confirm each proceeds independently.

## 8.3 PR strategy + partial failure

**PROMPT**
```
Add the Orchestrator's PR strategy: one PR per sub-task by default. Handle partial
failure: if sub-task 2 escalates while 1 and 3 succeed, produce PRs for 1 and 3
and surface 2 as needs_human — the ticket reflects mixed status. End by telling me
how to see a partial-success ticket.
```

**SEE** — a ticket where some sub-tasks produced PRs and one is escalated, clearly
shown.

**TEST** — force one sub-task to fail; confirm the others still PR and the ticket
shows mixed status honestly.

## 8.4 Cross-sub-task integration stage (agents as a team)

**PROMPT**
```
Add an integration stage owned by the Orchestrator, per architecture.md §7b and
ai_rules.md R-31. It runs AFTER all sub-tasks in a ticket are individually complete
and BEFORE any PR is opened.
- app/core/integration.py: integrate(ticket):
  * assemble the combined change across ALL affected repos/sub-tasks (apply the
    per-sub-task diffs together onto fresh checkouts).
  * run the FULL test suite for each affected repo on the combined change.
  * detect cross-breakage: a change from sub-task A breaking sub-task B (or any
    previously-passing test now failing).
- If all pass -> proceed to the PR stage (8.3).
- If cross-breakage -> DO NOT open PRs; escalate the ticket to needs_human with a
  clear report of what broke and which sub-tasks are implicated. @mention owner.
- Emit events for the integration run so it streams live.
This is the ONE place isolated sub-tasks are viewed together. End by telling me how
to force a cross-breakage (two sub-tasks that conflict) and watch integration catch
it before any PR.
```

**SEE** — a multi-sub-task ticket where the sub-tasks individually pass, but the
combined change breaks a test → integration catches it, blocks the PRs, and escalates
with a report of what broke.

**TEST**
- Make a ticket with two sub-tasks that individually pass but conflict when combined
  (e.g. one renames a function, the other still calls the old name).
- Run it → each sub-task completes, but the integration stage runs the full suite,
  detects the break, opens NO PRs, and escalates to needs_human with the details.
- Make a ticket whose sub-tasks are truly independent → integration passes, PRs open.

**Phase 8 done when:** multi-request tickets decompose into isolated sub-tasks,
each approved and executed independently, with honest partial-failure handling AND a
cross-sub-task integration stage that validates the whole change as one before any PR.

---

# PHASE 9 — Memory Layer (pgvector)

Goal: the controlled-lookup memory from `memory.md`. Build after core works.

## 9.1 Memory table + write path

**PROMPT**
```
Implement memory per memory.md.
- Migration: create subtask_memory table with VECTOR(1536) + hnsw index.
- app/memory/store.py: write_resolution(state) -> summarizes the resolution (one
  small LLM call), embeds problem_text (text-embedding-3-small), inserts the row.
- Call write_resolution when a sub-task reaches done (and on escalated/failed,
  labeled). End by telling me how to complete a sub-task and see its memory row.
```

**SEE** — after a sub-task finishes, a row in `subtask_memory` with a summary +
embedding.

**TEST**
```bash
docker exec -it agentic_sdlc_db psql -U agentic -d agentic_sdlc -c "SELECT type,problem_text,resolution,outcome FROM subtask_memory;"
# Expected: a row for the finished sub-task
```

## 9.2 Memory read path (similarity-gated injection)

**PROMPT**
```
Add app/memory/store.py: find_similar(problem_text, k=3, threshold=0.75) using
pgvector cosine distance, filtered to outcome='success'. In the Orchestrator,
before Diagnosis, call it and inject the top-k resolutions as advisory
prior_resolutions into the sub-task state (bounded, summaries only — memory.md
§7). Emit an event showing what was recalled. End by telling me how to see memory
recalled on a similar new ticket.
```

**SEE** — submit a ticket similar to a solved one; a "recalled from memory" event
shows the matched prior resolution.

**TEST** — solve a divide-by-zero ticket; submit another similar one; confirm the
prior resolution is recalled and shown. Submit an unrelated ticket; confirm
**nothing** is recalled (threshold works).

## 9.3 Memory guardrails + tuning

**PROMPT**
```
Enforce memory guardrails (memory.md §7): cap k and summary length, apply the
similarity threshold, mark non-success recalls as cautions, ensure no raw code/
secrets in summaries. Add tests/test_memory.py: write+read roundtrip, threshold
filtering (unrelated -> nothing), and that only summaries (not raw context) are
injected. End by telling me how to run them green.
```

**SEE** — memory that helps on real matches and stays silent on noise.

**TEST**
```bash
pytest tests/test_memory.py -v   # roundtrip + threshold + isolation all green
```

## 9.4 LLM response cache (exact + semantic, in Python)

**PROMPT**
```
Add a two-tier LLM response cache INSIDE the Python LLM client, per architecture.md
§7d and ai_rules.md R-36. Same Postgres/pgvector — NO separate service, NO Go.
- Tables: exact_cache (prompt_hash VARCHAR(64) PK, response, model_used, created_at;
  UNLOGGED for speed) and semantic_cache (prompt, embedding VECTOR(1536), response,
  model_used, created_at; HNSW cosine index).
- In LLMClient.complete/complete_json, BEFORE dispatching:
  1. exact: SHA-256 the (system+user+tier) -> lookup exact_cache; hit -> return it.
  2. semantic: embed the prompt -> pgvector search; if top cosine >= 0.92 -> return
     that response.
  3. miss -> call the model, then write BOTH caches.
- Never cache across tickets in a way that leaks context: cache keys are the prompt
  content only; responses are generic completions, not ticket-private data. Skip
  caching for prompts that embed ticket-private detail if configured to.
- Expose cache stats (hit rate) in the UI.
End by telling me how to make the same request twice and watch the second be a cache
hit (and see the hit-rate climb).
```

**SEE** — issue a repeat/near-repeat request; the second returns instantly as a cache
hit; a hit-rate counter climbs in the UI.

**TEST** — run a ticket, then a near-identical one; confirm (events/logs) the second
reuses cached LLM responses and cost/calls drop. Confirm distinct prompts still miss
and dispatch.

**Phase 9 done when:** the system reuses past resolutions through a controlled,
threshold-gated, summary-only channel — proven to recall on matches and stay quiet on
noise — AND repeat LLM calls are served from the exact/semantic cache.

---

# PHASE 10 — Parallelism, Hardening, Production-Ready

Goal: run independent sub-tasks in parallel, close the remaining edge cases, and
make it deployable.

## 10.1 Parallel independent sub-tasks

**PROMPT**
```
Upgrade the Orchestrator to run sub-tasks with no dependency between them in
parallel (LangGraph parallel branches), while still gating each for approval.
Ensure isolation and per-sub-task events remain correct under concurrency. End by
telling me how to watch two sub-tasks progress at once.
```

**SEE** — a multi-request ticket where independent sub-tasks advance
simultaneously in the live view.

**TEST** — submit a ticket with two independent sub-tasks; confirm concurrent
progress and that their events/states don't interleave incorrectly.

## 10.2 Remaining edge cases

**PROMPT**
```
Implement the remaining edge cases (architecture.md §6 (the guard)):
- Repo-changed-since-diagnosis freshness check before Executor (re-verify target
  files; if drifted, re-diagnose or escalate).
- Human-gate timeout policy (configurable reminder/expiry).
- Global per-ticket time budget alongside token budget.
Add tests for each. End by telling me how to trigger and observe each.
```

**SEE** — each edge case handled visibly (e.g. a stale-repo ticket re-diagnoses).

**TEST** — simulate a repo change between diagnosis and execute; confirm the
freshness check catches it. Confirm gate timeout behaves per config.

## 10.3 Production hardening + deploy

**PROMPT**
```
Make it deployable:
- Dockerfile for the app; docker-compose adds the app alongside db.
- Real Jira intake: a poller (or webhook endpoint) that ingests new Jira issues
  as tickets automatically.
- Structured logging, health endpoint, graceful handling of OpenAI/GitHub/Jira
  errors with backoff.
- A README with run instructions and an architecture summary.
- Basic auth on the UI (single operator) as a starting point for multi-user.
End by telling me how to bring the whole system up with one command and run a real
Jira ticket end-to-end automatically.
```

**SEE** — `docker compose up` brings up the whole system; a new Jira ticket flows
in on its own and runs the pipeline to a PR (with your approval gate).

**TEST**
```bash
docker compose up -d --build
# create a new Jira issue in your project;
# watch it get ingested, decomposed, and run to a PR after you approve — live.
```

**Phase 10 done when:** independent sub-tasks run in parallel, edge cases are
handled, and the whole system comes up with one command and ingests real Jira
tickets automatically — production-ready.

---

## Appendix — Per-prompt discipline (tape this to your monitor)

For **every** sub-step:
1. Tell Codex to read `agent_context.md` + `ai_rules.md` first.
2. Give it exactly one sub-step's PROMPT. Nothing more.
3. When it's done, run the **TEST**. Confirm the **SEE**.
4. Only then move on. If it drifted scope, reject and re-prompt.
5. Commit after each green sub-step: `git commit -m "phase X.Y: <capability>"`.

This is how a big system gets built without collapsing: small, visible, tested
increments — never a new layer on an unproven one.
