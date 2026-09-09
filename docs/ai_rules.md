# ai_rules.md — Engineering Rules Any Code Must Follow (Guardrails)

> **Purpose.** `architecture.md` says *what* to build. This document says *how the
> code must behave*, as hard rules. Every Codex prompt in `codex_prompts.md` ends with
> "obey ai_rules.md." When you review Codex's output, check it against this list. A
> rule broken here is a bug even if the code "works," because it breaks an invariant
> the whole system depends on.
>
> Rules are grouped and numbered so you can reference them (e.g. "this violates R-3").

---

## A. State & handoffs

**R-1. The blackboard is the only channel between agents.**
Agents never call each other and never receive another agent's raw conversation. An
agent reads named fields from the state object and writes named fields back. If you
find one agent importing or invoking another agent, that's wrong.

**R-2. Every handoff is a validated contract (Pydantic).**
Every field an agent writes to state is defined by a Pydantic model and validated
before the next stage reads it. No `dict` passed around untyped. If an agent's output
fails validation, it is rejected by the guard (see R-9), never silently accepted.

**R-3. Pass fields, not transcripts.**
The default handoff is structured fields, not another agent's reasoning history. Only
pass a trajectory when an agent genuinely must judge another's reasoning (rare — e.g.
the Critic). When you do, it must be compacted, never raw.

**R-4. One state object per sub-task; isolation is enforced at the data layer.**
Sub-task A's code path must be physically unable to read sub-task B's state — enforced
by keying every read on `subtask_id`, not by convention or prompt instruction. The
Planner is the only component allowed to see across sub-tasks.

---

## B. Agents vs. deterministic code

**R-5. LLM calls only for reasoning; everything mechanical is plain code.**
These are NOT agents and must contain no LLM call: Jira I/O, GitHub ops, test running,
diff application, file reads/writes, memory similarity search. (One narrow exception:
the memory service may make a single LLM call to summarise a resolution for storage.)

**R-6. The model never touches the world directly.**
An agent decides; a tool acts. The LLM never writes a file, runs a command, pushes to
git, or calls an API itself. It emits structured intent (e.g. a SEARCH/REPLACE block,
a plan); deterministic tool code executes it. This keeps every real-world action
predictable and auditable.

**R-7. Every tool call is logged with input and output.**
Store what each agent saw and produced, per sub-task, in the events log. This is the
audit trail, the debugging surface, and later the demo/talk material.

---

## C. Failure, loops, and the human exit

**R-8. Every loop has a hard limit and a human exit.**
Any retry loop (Critic↔Executor, edit self-correction, schema retry) has a max attempt
count `N`. On exhaustion: stop, write `failure_reason`, set `status = needs_human`,
route to the UI. Never loop unbounded. This is non-negotiable — it's the rule that
protects your API budget and your sanity.

**R-9. The guard runs after every agent, before proceeding.**
The Orchestrator's deterministic guard performs, in order: (1) schema validation,
(2) retry/loop-limit check, (3) budget check, (4) honest-failure exit. No agent output
advances the flow until the guard passes it. See architecture.md §6.

**R-10. Agents must be allowed to say "I can't."**
Each reasoning agent's output schema includes an explicit "unable / low-confidence"
path. When an agent can't produce a valid result (Diagnosis finds no root cause,
Planner can't decompose), it must signal that — and the guard routes to the human.
Agents hallucinate hardest when cornered; give them an honest exit so they don't
fabricate.

**R-11. Fail safe and visible, never silent.**
On any unrecoverable condition the system writes state, surfaces the failure in the
UI/Jira with a reason, and stops. It must never (a) silently swallow an error,
(b) proceed on partial/garbage data, or (c) keep spending tokens on a stuck loop.

---

## D. Checkpointing & recovery

**R-12. Persist state after every node.**
Use LangGraph's checkpointer backed by Postgres. After each agent/guard step, the full
state is on disk. A pause at a gate, a crash, or a server restart must resume from the
last checkpoint — never restart the sub-task from scratch.

**R-13. The human gate is a real pause, not a busy-wait.**
At a gate the flow persists and yields. It consumes no tokens and no CPU while waiting.
Approval (or rejection) resumes the exact checkpoint. An abandoned gate leaves valid,
resumable state indefinitely (timeout policy is a later phase, not a silent drop).

---

## E. Surgical edits (the Executor)

**R-14. SEARCH/REPLACE only — never full-file rewrite, never line-number diffs.**
The Executor edits by quoting exact existing code and its replacement. Full-file
rewrites and unified-diff/line-number edits are forbidden (they cause silent deletion
and line-math failures respectively).

**R-15. Exactly one match applies.**
An edit applies only if its SEARCH block resolves to exactly one location via the
cascade (exact → whitespace-normalised → fuzzy ≥ 0.8). Zero matches → self-correct and
retry. More than one match → fail closed, ask the model for more context. Never edit
on an ambiguous match.

**R-16. Deterministic apply + post-apply guard.**
Matching, applying, and validating are code, not the LLM. Before writing to disk: lint/
syntax check + sanity checks (line-count variance, emptied block, unexpected shrink).
Keep a pre-edit buffer so a failed guard rolls back cleanly. Multiple edits to one file
apply bottom-up (resolved against the unmodified file).

---

## F. Secrets, config, and safety

**R-17. Secrets only from environment, never in code or logs.**
All credentials come from `.env` via a settings object. Never hard-code a key, never
log a key, never send a real key to the model. Config is centralised (pydantic-settings)
— no scattered `os.environ` reads.

**R-18. The model is swappable behind one interface.**
All LLM calls go through a single model-client module reading `OPENAI_MODEL` from
config. No agent hard-codes a model name. Swapping models = changing one env var.

**R-19. Never act on the real repo's main branch.**
The system works on a fresh branch per sub-task and opens a PR. It never commits
directly to `main`/default, never force-pushes, never deletes branches it didn't
create. Human review via PR is mandatory.

**R-20. Freshness check before executing an approved plan.**
Because diagnosis happens before human approval (possibly much earlier), verify the
target files haven't changed since diagnosis before the Executor edits. If the repo
moved, re-diagnose or flag — don't edit a stale codebase.

**R-25. Jira status is dynamic and category-based; setting status is non-blocking.**
Never hardcode a full status-name mapping. Jira exposes every status with a
statusCategory (`new`=To Do, `indeterminate`=In Progress, `done`=Done); the app fetches
the project's statuses+categories from Jira and buckets by **category**, so ANY custom
status (On-Hold, Parking Lot, QA Review) is classified correctly with no `.env` editing.
"Ready to pick up" = category `new`; "active" = `indeterminate`; "finished" = `done`.
For status changes the app *performs* (claim→In Progress, PR→In Review), resolve the
target by category (optionally a configured name override when a category has several
statuses), and skip silently if none is available — a status update must NEVER raise,
block, or fail the actual work (R-11). Only a status whose category can't be resolved at
all counts as "unknown" and escalates. The Orchestrator triggers transitions; they are
deterministic tool calls, never agent decisions.

**R-26. The target repo comes from the ticket, resolved then confirmed — never hardcoded.**
There is no single global repo. Before planning, a deterministic repo-resolver runs a
cascade — ticket web links → description → (best-effort) reporter's repos → ask the
user to paste — and the resolved repo(s) are ALWAYS confirmed by the user at a gate
before work starts (a wrong repo edits the wrong codebase). Reading links/description
is plain code (no LLM). Assigning a repo to each sub-task is the Planner's job during
decomposition — NOT a separate agent. Each sub-task carries exactly one `repo` and is
isolated to it. The sandbox `GITHUB_REPO` env var is for local testing only, never the
production source of truth.

**R-27. Intake is poll-based with atomic, status-based claiming.**
Tickets are pulled by polling Jira on a configurable interval
(`JIRA_POLL_INTERVAL_MINUTES`, default 30) — a deterministic job, no LLM. Only tickets
in the ready status ("To Do") are eligible; tickets in any other status are left
untouched. On selection, a ticket is claimed atomically: recorded in the local DB
(with timestamp) AND flipped to In Progress, before any work. Every poll checks both
the local claim table and Jira status, so no ticket is ever claimed twice, even if
polls overlap. A poll must not re-enter while the previous cycle runs. A ticket
In Progress in Jira with no active local run is orphaned and flagged for a human, never
silently stuck (R-11). Multiple ready tickets are processed sequentially until true
parallelism is enabled (Phase 10).

**R-28. The supervisor: Jira is the source of truth; escalation cannot fail.**
The app is a supervisor watching tickets continuously; the DB is a cache, never an
excuse to stop checking Jira. Each cycle it reconciles tickets in scope (active runs +
Jira tickets updated since last sweep) against real Jira status: back-in-To-Do →
un-claim and re-pick; moved-to-Done/blocked → stop the AI run; human-driven In Progress
→ don't double-claim; a **reopened** long-closed ticket → treat as active again, never
error because it was closed; a status with NO config mapping (unknown) → escalate +
flag needs_human, never silently ignore. Ownership = "is there an active AI run?"
Reconciliation and commenting read ONLY the subject ticket's own record and history —
never another ticket's data. Before ANY comment, read the ticket's history and state
what's actually done. **Escalation must never fail on missing config:** always post a
comment, always @mention the owner (assignee else reporter), TRY to set a human/blocked
status but skip silently if unmapped (R-25), email deferred — even with every status
missing, comment + @mention reach a human. Stuck in ANY non-terminal status (not `done` category — In Progress OR parked like
On-Hold) beyond `STUCK_THRESHOLD_MINUTES`: AI-owned → history-aware "what completed /
where stuck" comment (no status change); human-owned/parked → "what's blocking / still
needed?" comment; healthy run → never nag; once per episode. So the supervisor never
silently forgets a non-done ticket — it nudges after the threshold (not instantly, which
would be noise). Push signal is To Do; reconciliation makes a drag-back or a reopen
reliably re-trigger the supervisor.

**R-29. Solution reuse is tiered — skip reasoning, never skip safety.**
Before running the reasoning agents, memory searches top-K resolved tickets. On a strong
match (similarity ≥ 0.9), skip Diagnosis and Step-Planner ONLY: take the past resolution
as a proposed fix, run a freshness/applicability check, then STILL go through the human
gate and Critic verification before the PR. Never blind-apply a past fix — a similar
ticket is not a proven-correct fix, and the codebase may have changed. Weak/no match →
full pipeline. The savings come from skipping heavy reasoning, not from skipping the gate
or verification. Reuse reads only resolved-ticket summaries (isolation still holds).

**R-30. Never change anything without a prior human gate; confirm intent first.**
No state-changing action (code edit, PR, status forcing) happens without a preceding
human approval, batched at the plan level (one meaningful approval per sub-task's plan —
not a prompt per micro-action, which would be unusable). Additionally, when a ticket's
intent is ambiguous, the Planner states its interpretation and asks the human to confirm
BEFORE decomposition is trusted ("I read this as X and Y — correct?").

**R-31. Validate the whole change as a team, not just each piece.**
Sub-task isolation prevents hallucination but hides cross-impact (a frontend change
breaking the backend). After all sub-tasks are individually complete and BEFORE any PR,
an integration stage assembles the combined change across affected repos, runs the full
test suite, and checks for cross-breakage. Pass → PR(s). Cross-breakage → escalate to the
human with what broke; never ship a change that passes per-sub-task but fails as a whole.

**R-32. Verify every fix — always write a test, run locally AND let CI confirm on the PR.**
The tool ALWAYS writes its own test covering the specific change it made — even if the repo
already has tests — so the fix has a dedicated regression test. A written test must be
COMPLETE and runnable: correct imports referencing the real code from its real module/path
(R-46), meaningful assertions that exercise BOTH the failing case and the fixed behavior,
and conform to the repo's test framework/location/conventions. Verification = the new test
passes AND existing tests still pass. Run tests **locally** first (fast gate; catch obvious
breaks before the PR), and when the repo has a **CI/CD pipeline**, let it run on the PR as
the authoritative, environment-matched check (**both**: local for speed, CI as the real
gate; if they disagree, CI wins). pytest exit codes: 0 = pass, 1 = real failure
(retry/critic), 5 = none collected (shouldn't happen once the tool writes its own test).
Only if a change is genuinely untestable does it flag "unverifiable" at the gate (R-32),
never silently assumed safe. If the change adds uncovered code, the Critic surfaces it.

**R-33. Tier models by task difficulty — big model plans, cheap models execute.**
Each agent's model is configured independently (extends R-18). The mechanism is
**plan-then-execute**: the expensive model does the *reasoning* once — it searches,
understands the code, and produces a spec detailed enough that a cheap model can execute
it without needing to think or search. So the big model is reserved for reasoning that
requires understanding (Diagnosis, ambiguous planning); cheap models do well-specified
execution (applying a spec'd edit, simple parsing, routing, Step-Planner formatting).
A **deterministic model router** (plain rules, NO LLM — task type → tier) decides which
tier each task uses; not everything goes to the big model. No agent hardcodes a model —
each reads its tier from config (`MODEL_CHEAP`, `MODEL_STRONG`, per-agent map). The
quality bar shifts to **spec completeness**: the big model must hand over genuinely
everything (exact locations, exact changes, expected outcome) so the cheap executor
doesn't guess; the Critic + verification catch specs that were too thin. This is the
single biggest cost lever after solution reuse.

**R-34. Every ticket has an enforced cost/LLM-call budget.**
Track LLM calls and estimated cost per ticket (and per sub-task), visible in the UI.
A configurable ceiling (`TICKET_CALL_BUDGET`, `TICKET_COST_BUDGET_USD`) is enforced by
the guard: on exceed, pause and escalate to the human ("this ticket has used X calls /
$Y — continue?") rather than silently spending. This catches slow sprawl that never
technically loops (complements R-8's loop caps). Minimise calls structurally too: prefer
solution reuse (R-29), merge cheap sequential steps into one call where it doesn't hurt
modularity, cap context to relevant slices, and cache stable prompt prefixes.

**R-35. I/O is async; the supervisor handles tickets concurrently.**
All external I/O (Jira, GitHub, OpenAI, DB) uses async (`async`/`await`, `httpx.AsyncClient`,
async DB access). FastAPI is already async — extend it through the stack so the poller/
supervisor can process independent tickets concurrently without blocking on one slow
call. This delivers most of the concurrency benefit without a second language. (True
parallel *agent* execution is still Phase 10; async is the I/O foundation under it.)

**R-36. LLM response caching (built when agents exist, not before).**
An exact + semantic response cache cuts repeat LLM cost: an `exact_cache` (SHA-256 of the
prompt → response, an unlogged Postgres table) checked first, then a `semantic_cache`
(pgvector, cosine ≥ ~0.92) for near-duplicates, before dispatching to a model. It lives
IN the Python LLM client (same Postgres/pgvector already in use) — NO separate service,
NO second language; a Go edge gateway is unnecessary at this scale (a hash+lookup is
microseconds in Python). Because it caches LLM calls, it is built with the memory phase
(Phase 9), once agents actually make those calls — building it earlier caches nothing.

**R-37. Event-driven fast path via webhooks (Python), polling stays the fallback.**
GitHub (PR opened/merged/closed) and Jira (status change) webhooks are received by
FastAPI endpoints — no separate service, no Go (a webhook is an HTTP POST Python handles
natively). Enforce idempotency by delivery ID (drop duplicates). Extract the Jira key
from PR title/branch/commits and maintain a PR↔ticket linkage table. Webhooks and the
polling supervisor reconcile into the SAME state/status logic: webhooks make it fast,
polling guarantees nothing is missed. Ticket/PR transitions follow the PR-state matrix
(architecture.md §5b): a reopened ticket whose linked PR is MERGED never un-merges the PR
(immutable history) — it comments that a new PR is needed; open/closed-unmerged PRs get
context comments. Built after PRs exist. A Go edge receiver is a future option only if
webhook volume becomes a measured bottleneck.

**R-38. The human gate can be answered by comment — with a permission check.**
Approval works from two channels driving the SAME gate/resume: the UI button and a Jira
comment reply ("APPROVE"/"REJECT"). A `pending_approvals` record tracks each gate's
lifecycle so approving one action never triggers another. Comment handling: ignore
anything not starting with APPROVE/REJECT (noise prevention), and REQUIRE a permission
check — only the assignee or a configured allow-list/role may approve; an unauthorised
reply is refused, never actioned (an approval anyone can trigger is not an approval, and
this is R-30's "the right human asks" made concrete). Both channels resume via the same
path; never double-apply.

**R-39. Workspaces are ephemeral scratch — nothing durable on the laptop.**
Code is edited in a local clone that is pure throwaway: the change lives in the GitHub
branch/PR, the summary lives on the Jira ticket + DB. Each sub-task gets its own isolated
clone dir + branch (no shared working dirs). Clone SHALLOW (`--depth 1`) and pull/clone
FRESH right before editing (R-20). DELETE the clone when the sub-task reaches a resting
state (PR opened / escalated / abandoned); keep it during an active retry loop, then
delete; re-clone fresh on any reopen. Because clones never accumulate, disk stays
near-zero at any ticket volume. A disk/workspace guard pauses + alerts if space runs low
(R-11). Shared per-repo cache + git worktrees is a deferred scale option, only if
measured.

**R-40. Narrate every meaningful step on the Jira ticket.**
The ticket is the audit trail. At each meaningful step boundary the tool posts a comment
stating what it did or found — repo resolved ("using `owner/repo`"), diagnosis done
("found `<root cause>` in `<file>`"), plan ready (the approve prompt), edit+tests done
("applied fix, tests passed/failed"), PR opened ("PR #N: `<link>`"), and ANY failure or
block ("blocked: `<what and why>`"). Comment at step boundaries, ONE per completed step
with the result in it — not per micro-action (no "reading file"/"calling LLM" noise). The
test: would a human teammate post this as a progress update? So anyone reading the ticket
sees the full journey. Reads only that ticket's own data (isolation).

**R-41. Validate the repo before use — refuse and surface if invalid.**
Before accepting/using a resolved repo, verify it's reachable via GitHub
(GitHubTool.get_repo). If it doesn't exist or the token can't access it: REFUSE it (don't
silently accept), post a Jira comment naming the problem ("repo `owner/repo` not found or
not accessible"), flag needs_human, and block until a valid repo is provided. Same for a
missing/empty repo or a clone failure at diagnosis — surface it (UI + Jira comment +
needs_human), never silently proceed or no-op. This is R-11 ("never fail silently")
applied to repos.

**R-42. Public-first repo access; private repos use an encrypted UI-entered token.**
Works for ANY repo the ticket names — never a hardcoded or single repo (the repo is
always dynamic, R-26). Default to anonymous clone/read — any public repo needs no token
and works instantly (primary path). If an anonymous clone fails because the repo is
private/inaccessible: surface it (R-41) — the UI offers a masked, password-type token
field FOR THAT REPO, and a Jira comment notes it. Tokens are stored per-repo
(`repo_tokens` keyed by owner/repo), so many different private repos each carry their own
token. A UI-entered token is a credential: store it ENCRYPTED at rest (symmetric key from
env, decrypt only in memory during the clone), reuse it for that repo, and NEVER log it,
return it in any response, or show it after entry (R-17). Clone flow (per repo): try
public → on auth failure use that repo's saved token → else prompt via UI/comment.
Cross-owner private access this way needs no GitHub App; a GitHub App remains the future
option for true multi-tenant install-based access.

**R-43. One active subtask per ticket; supersede cleanly on re-pick.**
A ticket has at most ONE active (running/waiting) subtask at a time — never a pile of
zombie "running" subtasks. When a ticket is re-picked (e.g. In Progress → To Do → claimed
again), the reconciler/poller must: (1) summarise the previous subtask (what it did, where
it stopped), (2) post that summary as a comment on the Jira ticket so the human sees the
history, (3) close/supersede the old subtask, then (4) start a fresh one. The summary is
also the record reused if the ticket returns. Never leave an old subtask "running" when a
new run begins — orphaned runs confuse the UI and waste state. (Extends reconciliation
R-28 to the subtask lifecycle.)

**R-44. Monitor human Jira comments; commands route through the gate, never auto-act.**
The tool reads human comments on a ticket and classifies intent: COMMAND (stop, redo,
add work), QUESTION (why this repo? status?), or CHATTER (discussion, thanks). Commands
NEVER auto-act — they are turned into a proposal that goes through the normal human gate
+ approval (R-30); a comment requests, it does not override. Questions get an answer
(comment back). Chatter is ignored. Guardrails: (1) only ACT on comments from authorized
users (permission-checked, R-38); (2) NEVER read the tool's own comments as instructions
(filter by author — prevents self-triggering loops); (3) when intent is ambiguous, ASK
for clarification rather than guessing (R-10); (4) each comment processed once
(idempotent by delivery id). Built in Phase 5.5 on the comment webhook.

**R-45. The UI is the complete interface — every action is a button, not a command.**
Anything a user needs to do is doable from the web UI with a click: run poll, resolve
repos, confirm repos, change repo, run diagnosis, approve/reject at the gate, view a
ticket's detail (diagnosis, plan, subtasks, status, budget), retry/resolve escalations.
Debug curl endpoints may exist for testing, but they are NEVER the intended way to
operate the tool — the UI covers everything. UI consistency rules: show "Resolve repos"
ONLY when a ticket has no confirmed repo; once confirmed, hide it, show the repo, and
offer a "Change repo" option to re-resolve. Buttons reflect current state (don't show
actions that don't apply).

**R-46. Ground every agent in the repo's ACTUAL files — never assume a file exists.**
Diagnosis, Step-Planner, and Executor must work from the real repository contents, not
assumed conventions. Before referencing or editing a file, verify it exists (list/read
the real repo). The plan and execution operate on files that ARE there; the Executor may
CREATE a file when genuinely needed (creating it before referencing it), EDIT existing
files, or DELETE files the fix requires — but it must never assume a filename (e.g.
`test_app.py`) exists or run a tool against a file it never confirmed. Tests: run against
what the repo actually has; on "no tests collected" (pytest exit 5), treat as
"no tests present," not a failure — create a test if the plan calls for it, else flag
unverifiable (R-32). Distinguish exit 0 (pass) / 1 (real failure) / 5 (none collected).
When writing that test, ground it in the REAL current content of the file(s) it
covers (not just the diagnosis's prose summary) and, if one exists, an existing test
file's real framework/conventions — a name recalled only from prose, not from the
actual source, is how a written test ends up calling something that was never
imported (NameError). A failure whose output names an import/name error gets a
targeted repair hint, not a generic "pytest failed."

Local pytest is the fast gate; when the repo has CI configured on the PR, CI is the
**authoritative** check — if they disagree, CI wins. This never blocks the flow (CI
is checked on demand, not polled/awaited); both results are surfaced in the UI.

**R-47. Ticket comments are a two-way chat, not one-way narration.**
The Jira ticket is a conversation: the tool narrates steps (R-40) AND reads + responds
to human replies (R-44), back and forth, like a chat thread. A human can ask, instruct,
or correct mid-flow; the tool reads it, responds (answer, or a gated proposal for
commands), and continues — so the ticket reads as a genuine dialogue between the human
and the tool, each aware of the other's messages (tool never treats its own messages as
input; permission-checked; ambiguity → ask).

---

## G. Code quality (keep it reviewable)

**R-21. Small, typed, single-responsibility modules.**
Each agent, tool, and guard in its own module with typed inputs/outputs. No god-files.
A reviewer (you) should be able to open one file and understand one thing.

**R-22. Every phase ships with runnable tests.**
No phase is "done" until it has automated tests that pass AND a visible/manual
verification described. Tests live under `tests/phaseNN/`. See codex_prompts.md.

**R-23. Structured logging, one line per event.**
Log every stage transition, guard decision, tool call, and escalation as a structured
record (ideally JSON). This feeds the SSE stream and the audit trail.

**R-24. Fail imports loudly, degrade features gracefully.**
Missing dependency or misconfig → crash at startup with a clear message (fail fast).
A single agent/tool failing at runtime → contained by the guard, escalated to human —
not a whole-system crash.

---

## Quick self-review checklist (run against any Codex output)

- [ ] No agent imports/calls another agent (R-1)
- [ ] All handoff data is Pydantic-validated (R-2, R-9)
- [ ] No LLM call inside a mechanical step (R-5, R-6)
- [ ] Every loop has a max-N and a human exit (R-8, R-11)
- [ ] Agents can signal "I can't" (R-10)
- [ ] State persisted after each node; resumes after restart (R-12, R-13)
- [ ] Edits are SEARCH/REPLACE, one-match-only, guarded, reversible (R-14–R-16)
- [ ] No secret in code or logs; model swappable via env (R-17, R-18)
- [ ] Never touches main; PR only; freshness-checked (R-19, R-20)
- [ ] Phase has passing tests + a visible verification (R-22)
