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
(deterministic, no LLM), per ai_rules.md R-25 and architecture.md §5c.

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
architecture.md §5b and ai_rules.md R-26. No LLM in the resolver.

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

# PHASE 3 — First Real Agent + The Graph

Goal: a LangGraph graph with one real agent (Diagnosis) that reads your sandbox
repo and produces a structured root-cause — streamed live.

## 3.1 The LLM wrapper + SubtaskState

**PROMPT**
```
Create the LLM access layer and the core state model.
- app/agents/llm.py: a single LLMClient wrapping OpenAI. Methods: complete(system,
  user, model=None) and complete_json(system, user, schema: pydantic model,
  model=None) that returns a validated instance (retry once on validation error
  with the error fed back). Model defaults from settings; NO agent calls OpenAI
  directly (ai_rules R-11 (fail safe & visible)). Track token usage and return it.
- app/agents/state.py: the SubtaskState Pydantic model exactly as in
  architecture.md §5 (the blackboard) — all fields, including `repo` and the
  control fields (retry_count, budget_used, status, failure_reason).
End by telling me how to run a quick script that calls complete_json and prints a
validated object.
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
- clone_or_pull(full_name) -> local path to a fresh checkout of the given
  "owner/repo" default branch (shallow clone to a temp dir keyed by repo; reuse if
  present). The repo comes from the sub-task's `repo` field (assigned by the
  Planner), NOT a hardcoded one — for this early phase you may pass the sandbox repo
  explicitly while the Planner isn't built yet.
- list_files(full_name) and read_file(full_name, path) within that checkout.
These are deterministic tools. End by telling me how to print the repo's file list
and app.py contents.
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
Accumulate token usage into budget_used on every LLM call. Add the runaway rule:
if any loop hits MAX_AGENT_RETRIES, stop and escalate. Add a per-ticket budget
ceiling that pauses to human. Surface budget_used live in the UI. End by telling
me how to see the budget climb and trip.
```

**SEE** — a live token/budget counter on the ticket page; it trips to human if
exceeded.

**TEST** — set a tiny TICKET_TOKEN_BUDGET in `.env`, run a ticket, watch it pause
for human when the budget is exceeded.

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
issues: list[str]}. If not approved, route back to Executor with the issues
(counts toward MAX_AGENT_RETRIES; on cap -> escalate). Insert Critic between
Executor and the PR node. End by telling me how to watch the Critic accept/reject
a fix.
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
For now the flow still handles ONE sub-task (take the first), but the Planner runs
and records the full decomposition. Failure exit: CannotDecompose -> needs_human.
Put Planner at the very front of the graph (after repo resolution). Each sub-task's
`repo` flows into its SubtaskState and is used by Diagnosis/Executor/PR.
End by telling me how to see a ticket decomposed with a repo assigned per sub-task.
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

**Phase 8 done when:** multi-request tickets decompose into isolated sub-tasks,
each approved and executed independently, with honest partial-failure handling.

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

**Phase 9 done when:** the system reuses past resolutions through a controlled,
threshold-gated, summary-only channel — proven to recall on matches and stay
quiet on noise.

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
