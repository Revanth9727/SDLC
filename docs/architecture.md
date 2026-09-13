# architecture.md — The System Design (Permanent Reference)

> **Purpose.** This is the canonical description of what the system is and why each
> part exists. Read it once fully; return to it whenever a build decision is unclear.
> `codex_prompts.md` builds this design in phases; `ai_rules.md` enforces its
> invariants. If code ever contradicts this document, the document wins (or the
> document is updated deliberately — never silently).

---

## 1. One-paragraph summary

A ticket describing *any* software task (bug, feature, CI failure, design change)
enters from Jira. A **Planner** splits it into isolated sub-tasks with a dependency
graph. An **Orchestrator** schedules them (parallel where independent). For each
sub-task, running in its own isolated context: a **Code-Intelligence** agent finds and
verifies the relevant code (Phase 11; on small repos this is a simple read), a
**Diagnosis** agent reasons about the root cause from that verified slice, a
**Step-Planner** breaks the fix into ordered steps, a
**human approves the plan**, an **Executor** applies surgical edits step by step, and
a **Critic** validates the result before a pull request is opened. Everything moves
through a shared, checkpointed **state object** (the "blackboard"), so no agent ever
loses context and the flow can pause or crash and resume exactly where it left off. Two
persistent layers support this: a **memory layer** (how similar past work was resolved)
and **repository intelligence** (what the code is), both without breaking isolation.

---

## 2. The core principles (the "why" behind everything)

1. **Two levels of decomposition.** A ticket is split into sub-tasks; each sub-task
   is split into steps. Work keeps breaking down until each unit is small enough to
   do reliably. This is what lets the system handle "anything."

2. **Sub-task isolation prevents hallucination.** Each sub-task runs in its own
   isolated context. The bugfix sub-task cannot see the feature-add sub-task's data.
   Two requests in one ticket are as separated as two different tickets. Only the
   Planner ever sees across sub-tasks.

3. **The blackboard, not conversation, carries context.** Agents never message each
   other and never inherit each other's raw conversation. Each agent reads structured
   fields it needs from a shared state object and writes structured output back.
   Handoffs pass clean data, not conversational noise. This is what prevents "agent
   amnesia."

4. **Deterministic where possible, agentic only where needed.** LLM agents *reason*.
   Everything else — Jira I/O, GitHub ops, running tests, applying a diff, memory
   lookup — is plain, predictable code. Never turn a mechanical step into an agent.

5. **Every loop and every agent has a failure exit that routes to the human, and
   every handoff has a validated contract.** The system's job is not to never fail —
   it's to fail safely and visibly, handing control back rather than thrashing or
   hallucinating.

6. **Checkpoint everything.** State is persisted after every step, so a pause at an
   approval gate (minutes or hours) or a crash never loses progress. The flow is
   unbreakable because its memory is on disk, not in RAM.

---

## 3. The agents (7) and the non-agent components

### Agents (they reason with an LLM)

| # | Agent | Job (one line) | Decides |
|---|-------|----------------|---------|
| 1 | **Planner** | Split the ticket into isolated sub-tasks + dependency graph | *What* |
| 2 | **Step-Planner** | Split one sub-task into ordered coding steps | *How, in detail* |
| 3 | **Orchestrator** | Schedule sub-tasks, run gates, manage flow & PR strategy | *When* |
| 4 | **Diagnosis** | Find root cause for one sub-task (from the slice Code-Intelligence supplies) | — |
| 5 | **Executor** | Apply the approved steps as surgical edits, run tests | — |
| 6 | **Critic** | Validate the result against the ticket before PR | — |
| 7 | **Code-Intelligence** | *Investigator* (Phase 11): find the relevant code + how it connects; feed Diagnosis | *Where to look* |

> The Orchestrator is a coordinator; it does minimal reasoning (mostly scheduling and
> gate decisions). Some builds make it pure code — that's fine. It's listed as an
> agent because it may use an LLM for PR-strategy and ambiguous scheduling calls.
>
> The **Code-Intelligence agent** (7th, Phase 11) is an *investigator*: given a ticket it
> searches (exact + semantic), reranks to a shortlist, follows callers/callees, verifies
> against real source, and returns the relevant files + execution path + confidence. It
> **reasons** (the investigation path is unknown up front), but every capability it calls
> is a deterministic tool, and it is bounded, guarded, and budget-capped (R-50/R-51). See
> §7e.

### Non-agent components (deterministic code — NOT agents)

- **Jira intake** — receive/poll tickets, post comments.
- **GitHub ops** — clone, branch, commit, push, open PR.
- **Test runner** — run the repo's tests, capture pass/fail + output.
- **Edit applier** — the surgical-edit cascade (match, apply, guard). See §8.
- **Memory service** — embed + pgvector similarity search + write-back. One small
  LLM call to summarise a resolution for storage; otherwise pure retrieval.
- **Repository-intelligence service + tools** (Phase 11) — AST parse, symbol/reference
  extraction, SQL read/write detection, embeddings, git-diff incremental update, graph
  edges, and the query tools (`search_exact`, `search_semantic`, `find_symbol`,
  `get_callers/callees/references`, `get_reads_writes`, `get_file`, `get_diff`). All
  deterministic; the Code-Intelligence agent orchestrates them. See §7e.
- **The guard** — post-agent validation + counters (see §6).

Turning any of these into an agent adds cost, latency, and new hallucination
surface for zero benefit.

---

## 4. The end-to-end flow

```
Jira ticket
   │
   ▼
Jira ticket (status "To Do")
   │
   ▼
[Jira POLLER]  (every N min; claims only "To Do" tickets, flips to In Progress)
   │
   ▼
[RESOLVE REPOS]  (deterministic cascade + confirm gate — §5c)
   │
   ▼
[LIGHT REPO OVERVIEW]  (cheap deterministic: file inventory + folder/module structure,
            reuses cold-index inventory; NO embeddings/graph/investigation — R-54.
            Shown as a visible "Understanding the repository…" phase.)
   │
   ▼
PLANNER ──► splits into isolated sub-tasks + dependency graph
            (uses the overview to split sensibly; assigns each sub-task its repo)
   │
   ▼
ORCHESTRATOR ──► picks next ready sub-task (parallel where independent)
   │
   │   ┌─────────── per sub-task, in ISOLATED state ───────────┐
   │   │                                                        │
   │   │  MEMORY REUSE GATE ─► search resolved tickets;         │
   │   │      │  strong match (≥0.9)? → skip Diagnosis+Step-     │
   │   │      │  Planner, propose known fix → human gate         │
   │   │      │  (else fall through) ──────────────┐            │
   │   │      ▼                                     │            │
   │   │  CODE-INTEL ─► find+verify relevant slice   │            │
   │   │      │         (Phase 11; tools, no blind read)│          │
   │   │      ▼                                     │            │
   │   │  DIAGNOSIS ─► root_cause from the slice     │            │
   │   │      │                                     │            │
   │   │      ▼                                     │            │
   │   │  STEP-PLANNER ─► writes ordered steps      │            │
   │   │      │                                     │            │
   │   │      ▼◄────────────────────────────────────┘            │
   │   │  HUMAN GATE ─► post plan+reasoning to Jira, PAUSE      │
   │   │      │  (checkpointed; resumes on approval)            │
   │   │      ▼                                                 │
   │   │  EXECUTOR ─► surgical edits step-by-step (tool),       │
   │   │      │       run tests (tool)                          │
   │   │      ▼                                                 │
   │   │  CRITIC ─► validate vs. ticket                         │
   │   │      │        │                                        │
   │   │      │        └─ reject ─► back to Executor (≤N) ─┐    │
   │   │      ▼                                            │    │
   │   │  approved → sub-task marked ready-for-integration │    │
   │   │  (NO PR here; publishing happens after integration)│   │
   │   └───────────────────────────────────────────────────────┘
   │                    │  (N rejects → escalate to human)
   │                    ▼
   │            [INTEGRATION STAGE]  (§7b — after ALL sub-tasks are
   │             critic-approved: assemble the combined change on one
   │             checkout, run the FULL test suite, catch cross-breakage.
   │             Break → escalate, NO PR.)
   │                    │
   │                    ▼
   │            [GitHub ops] ─► branch, commit, open PR   (once, post-integration)
   │                    │
   │                    ▼
   │            [Memory write-back] ─► store each resolution
   ▼                    ▼
next sub-task …      done → final report on the ticket
```

Every arrow between agents is a **read-from / write-to the blackboard**, mediated by
the Orchestrator. Every action on the world (repo, tests, PR, Jira) is a **tool
call**, i.e. deterministic code. Every stage is **checkpointed**.

> **Where the PR is opened (important):** a sub-task does NOT open its own PR. On
> Critic-approval a sub-task is marked ready-for-integration; the integration stage
> assembles all sub-tasks, runs the full suite, and only then publishes. **Publishing is a
> single shared step (R-53):** both the normal ticket flow and the human comment-command
> flow go through ONE publish path — not the old split where a "legacy"/comment path used
> separate `publish_pr`/`notify_pr` nodes. That shared step enforces **one open PR per
> ticket**, checked BEFORE any LLM calls: if the ticket already has an open PR (from
> `pr_links` + the Phase 5.5 PR-state matrix), it stops, shows the human a summary of the
> existing PR, and asks whether to keep or replace it — never silently skipping or
> replacing. After a PR merges, the tool re-reads the updated code (freshness, R-20) and
> works incrementally.

---

## 5. The state object (the blackboard)

There is one state object **per sub-task** (isolation boundary). It is the single
source of truth; agents read and write only through it. Conceptual shape:

```
SubTaskState:
  # identity / isolation
  ticket_id            # parent ticket
  subtask_id           # THIS sub-task (isolation key)
  subtask_type         # bug | feature | ci | design
  repo                 # owner/repo this sub-task works on (assigned by Planner)
  depends_on           # sub-task ids that must finish first

  # produced by Code-Intelligence (Phase 11) — the verified slice Diagnosis reasons from
  code_context         # {relevant_files, relevant_symbols, relevant_chunks,
                       #  execution_paths, evidence, confidence, repo_snapshot_id}
                       # Pydantic-validated. This is the ONLY channel Code-Intelligence
                       # uses to hand results to Diagnosis (R-1: blackboard-only).

  # produced by agents (structured, validated)
  diagnosis            # {root_cause, files, reasoning}  — reasons from code_context
  plan                 # [ordered steps]
  current_step         # index into plan
  steps_done           # [{step, edit, apply_result, test_result}]

  # repository snapshot this work was built on (freshness)
  repo_snapshot_id     # the RepositorySnapshot (repo + ref + commit_sha) Code-Intel used
  diagnosed_commit_sha # commit the diagnosis/plan were built against

  # human interaction
  approval_status      # pending | approved | rejected
  approval_payload     # what was shown at the gate

  # control fields (the guard reads/writes these)
  retry_count          # per current loop
  budget_used          # tokens / time so far
  status               # running | needs_human | failed | done
  failure_reason       # set when escalating to human

  # outputs
  pr_url               # final
  memory_refs          # similar past sub-tasks the memory layer injected
```

Two rules about this object:
- **Structured, validated handoffs.** Every field an agent writes is validated
  (Pydantic) before the next agent reads it. Malformed output → the guard rejects and
  retries. See `ai_rules.md`.
- **Default to fields, not transcripts.** Agents receive the specific fields they
  need — not another agent's reasoning history. The rare exception (the Critic wanting
  to see *why* the Executor chose something) is where light context compaction applies
  later; it is not the default.

The Planner also has a small **TicketState** (the only thing that sees all sub-tasks):
`{ticket_id, raw_ticket, resolved_repos[], subtasks[], dependency_graph, overall_status}`.
`resolved_repos` is the confirmed list from §5c, which the Planner assigns across
sub-tasks.

---

## 5a. Ticket intake: polling + status-based claiming

Tickets enter by **polling Jira on a schedule** (interval configurable via
`JIRA_POLL_INTERVAL_MINUTES`, default 30). The poller is a deterministic job (no LLM).

**Status is the work-queue.** Only tickets in the ready status (**"To Do"**, the
project's default/created state) are eligible. Tickets in any other status
(In Progress, In Review, Awaiting Approval, Blocked, Done) are **left alone** — they're
already being worked, waiting on a human, or finished. This is deliberate: an
In-Progress/In-Review ticket may have an active run or a pending human decision;
re-grabbing it would double-process it.

**Atomic claiming (prevents double pickup).** The instant a ticket is selected:
1. Record it as claimed in the local DB (`tickets` row + claimed_at timestamp), and
2. Flip its Jira status to `in_progress` (R-25) — *before any work begins*.
The status flip **is** the claim: the next poll skips it (not "To Do" anymore), and the
local claim record guards the brief gap between selecting and flipping. Every poll
checks BOTH the local claim table and Jira status, so a ticket is never claimed twice —
even if polls overlap.

**Overlap & recovery.**
- A poll never re-claims an already-claimed ticket (local table is authoritative for
  in-flight work). A simple "poll already running?" guard avoids piling up cycles.
- A ticket that is In Progress in Jira but has **no active run** in the local DB is
  *orphaned* (e.g. a crash mid-work); it is flagged for a human, never silently left
  stuck (R-11). (Recovery job is a later phase.)

**Concurrency.** When a poll finds multiple ready tickets, they are **processed
sequentially for now**; independent tickets are safe to run in parallel (isolated
state), so **true parallelism is enabled in Phase 10**. This is the first place
parallelism applies: whole independent tickets. (The second is independent sub-tasks
within a ticket — also Phase 10.)

## 5b. The Supervisor: Jira is the source of truth

The app is a **supervisor** that watches tickets continuously, not just an intake that
grabs To Do items. The DB is a **cache**, not the truth. Jira status is the human's
control surface, so **every cycle the supervisor reconciles** the tickets it should be
watching against their real Jira status — the DB never lets the app stop re-checking
Jira. This fixes the core flaw where a ticket already in the DB is ignored even after a
human changes it.

**Sweep scope (robust to reopens, not a full-history rescan).** Each cycle the
supervisor considers: tickets with an active local run, plus any Jira ticket **updated
since the last sweep**. Key property: a ticket resolved long ago that gets **reopened**
changes its Jira updated-time/status, so it re-enters scope automatically and is handled
as active again — the app never fails or goes blind on a reopen, without the cost of
rescanning every historical ticket every cycle.

**Reconciliation (for each ticket in scope, any status):**
re-read the ticket's real Jira status and resolve the drift:
- Claimed locally but back in **To Do** (human reset it) → clear the local claim,
  re-pick it.
- App is working it but human moved it to **Done/blocked** → the human overrode →
  stop the AI run.
- Human moved **To Do → In Progress** manually → detect it's human-driven, don't
  double-claim.
- A previously-resolved ticket now **reopened** → treat as active again; never error
  because it was closed before.
- **Unknown status** (a Jira status with NO mapping in config) → the app can't
  interpret it → escalate (see the nudge cascade below) AND flag the ticket
  `needs_human`. Never silently ignore a status it doesn't understand.
- Status matches expectation → nothing to do.

**Escalation that cannot fail (the nudge cascade).** Escalation must NOT depend on any
Jira status existing. When a ticket needs human attention (stuck, unknown status,
unrecoverable), the supervisor: (1) ALWAYS posts a Jira comment (works regardless of
workflow config); (2) **@mentions the owner** (assignee, else reporter) so a human is
notified; (3) TRIES to set a human/blocked status but skips silently if it's not mapped
(R-25); (4) email notification is deferred. Even if every status mapping is missing, the
comment + @mention still reach a human — a ticket never falls silently through a crack.

**Per-ticket isolation is absolute here.** Reconciliation and commenting read ONLY the
subject ticket's own DB record and event history — never another ticket's data. No
cross-ticket query, ever. (This extends the sub-task isolation guarantee to the
reconciliation layer.)

**Informed comments (read history first).** Before writing ANY comment on a ticket,
load that ticket's own event history, determine what stage it actually reached and what
happened, and word the comment to reflect reality (e.g. "diagnosis done; stuck applying
the fix at step 3") — never a generic "blocked." The same history read is used when
re-picking a ticket, so completed work is not redone.

**Who's-in-control detection.** Before acting on any ticket, determine ownership by one
check: **is there an active AI run for it in the DB?** Active run → AI owns it. No
active run but In Progress → a human owns it. This single check arbitrates every
human-vs-AI decision.

**Stuck detection + smart comment.** A ticket in ANY non-terminal status (any category
that is not `done` — including In Progress AND parked statuses like On-Hold) longer than
`STUCK_THRESHOLD_MINUTES` (configurable) is examined by ownership, AFTER reading its
history:
- **AI-owned and wedged** → comment stating what was completed and where it stuck (do
  NOT change the Jira status — respect the human's board); @mention the owner.
- **Human-owned and stalled/parked** → comment asking what's blocking or what the plan
  is (e.g. "this has been parked in On-Hold for N — still needed, or ready to move?"),
  referencing what's already been done; @mention the owner.
- **Healthy in-flight AI run** → never nag.
- **Comment once per stuck episode** — don't repeat until the situation changes.

So the supervisor never *silently* forgets a ticket: anything non-done that sits too long
gets a nudge. It just waits for the threshold rather than commenting the instant a human
parks something (which would be noise — they just moved it on purpose).

The push signal stays **To Do** — reconciliation is what makes a human dragging a ticket
back to To Do (or reopening a closed one) reliably re-trigger the supervisor.

**Event-driven fast path (webhooks) — the supervisor's other input.** Polling is the
reliable *fallback*; **webhooks** are the *fast path* that reacts instantly:
- **GitHub** fires on PR opened/merged/closed; **Jira** fires on issue status change.
- Received by **Python/FastAPI webhook endpoints** (no separate service, no Go — a
  webhook is an HTTP POST FastAPI handles natively).
- **Idempotency:** each delivery carries an ID (`X-GitHub-Delivery` / Jira delivery id);
  check it against the DB and drop duplicates (webhooks can fire twice).
- **Key extraction:** parse the Jira issue key from PR title/branch/commits
  (`(?i)[A-Z]{2,10}-\d+`) to link a PR to its ticket.
- **PR↔ticket linkage table:** map `github_pr_id → jira_issue_key` with the PR's state.

Webhooks and polling reconcile into the **same** state/status logic — webhooks make it
fast, polling guarantees nothing is missed. Built after PRs exist (see the phased build).
If webhook volume ever becomes a *measured* bottleneck, a lightweight Go edge receiver is
a future option — not a present need.

**PR-state matrix (how ticket/PR transitions resolve):**

| Event | Source | Condition | Action |
|-------|--------|-----------|--------|
| PR opened | GitHub | branch/PR names the key | link PR; move ticket To Do→In Progress; comment "PR #N opened" |
| PR merged | GitHub | linked to key | mark link DONE; move ticket → Resolved/Done |
| Ticket reopened | Jira | Done→active AND linked PR **merged** | do NOT reopen the PR (merged is immutable); comment "reopened, but PR #N already merged — new PR likely needed" |
| Ticket reopened | Jira | linked PR **closed-unmerged** | leave PR closed; post context comment |
| Ticket reopened | Jira | linked PR **open** | comment on the PR "ticket moved back to In Progress" |
| Branch deleted / PR closed no-merge | GitHub | — | mark link ABANDONED; context comment on ticket |

This refines the supervisor's generic "reopened → active" into precise handling of the
ticket's *existing PR*.

## 5c. Repo resolution (which codebase does this ticket touch?)

A ticket does not carry its target repo in a fixed field, and a single ticket may
touch **multiple repos**. So before the Planner can decompose, the system must resolve
**which repo(s)** the ticket is about. This is a deterministic **repo-resolver**
(not an agent) followed by a **human confirmation gate**. It runs right after intake,
before planning:

```
Jira intake → RESOLVE REPOS (cascade + confirm gate) → PLANNER → …
```

**The resolution cascade** (stop when candidates are found, then always confirm):

1. **Web links** — read the ticket's remote/web links; extract any
   `github.com/owner/repo` URLs. (Primary, reliable path — the user links the repo via
   Issue → Link → Web link.)
2. **Description** — scan the ticket description for GitHub URLs.
3. **Reporter's GitHub repos (best-effort)** — if the reporter's GitHub account is
   known, list their repos as candidates. This path is noisy/often unavailable (no
   native Jira↔GitHub user mapping, too many repos, org-owned repos); if it can't
   produce a short clean candidate list, **skip to step 4**. Never a rabbit hole.
4. **Ask the user to paste** — if nothing else worked, open a gate asking for the
   repo URL(s) directly. This fallback covers every remaining case.

**Always confirm.** Even a single unambiguous match is shown to the user for
confirmation before any work starts — a wrong repo means diagnosing/editing the wrong
codebase, so a one-tap confirm is cheap insurance. Confirmation and paste both use the
existing human-gate (pause/resume) mechanism — no new machinery.

**Output:** a confirmed list of `owner/repo`. The Planner receives this list and,
during decomposition, **assigns each sub-task its repo** (see §2 principle 1 and the
per-sub-task `repo` field in §5). Repo assignment is part of the Planner's existing
reasoning — there is no separate repo-matching agent. If a sub-task's repo is
ambiguous, the Planner flags it at the gate rather than guessing (R-10).

## 5d. Jira status sync (keeping the board honest)

The Jira ticket is what humans watch, so the system keeps its **status** in sync with
where the work actually is — a production requirement, not a nicety. This is a
deterministic responsibility of the Jira component (not an agent), triggered by the
Orchestrator at stage boundaries.

**Stage → status mapping** (internal stage → configured Jira status name):

| System event | Internal stage | Typical status |
|---|---|---|
| Work starts on the ticket/sub-task | `in_progress` | In Progress |
| Plan posted, waiting on the human gate | `awaiting_approval` | Awaiting Approval |
| PR opened | `in_review` | In Review |
| Guard escalation / cannot proceed | `blocked` | Blocked |
| Completed | `done` | Done |

**Dynamic, category-based (the key design choice).** Jira workflows are project-specific,
and users add/rename statuses freely — so the app must NOT depend on a hardcoded
status-name map. Every Jira status carries a **statusCategory** (`new`=To Do,
`indeterminate`=In Progress, `done`=Done). The system:

1. **Fetches** the project's statuses + categories from Jira (cached, refreshed per
   cycle) and **buckets by category** — so any custom status (On-Hold, Parking Lot, QA
   Review) is classified correctly with zero `.env` editing.
2. **Reads** ticket state by category: `new`→ready-to-pick-up, `indeterminate`→active,
   `done`→finished.
3. **Sets** status by resolving the target category to an available transition (with an
   optional name override when a category has several statuses), skipping silently if
   none exists — a status update NEVER blocks or fails the real work (R-25 / R-11).
4. Treats only a status with an **unresolvable category** as "unknown" → escalate.

This means the same code works across standard and custom Jira workflows with no config
changes, and the user never hand-maps statuses.

### 5d-i. User-declared statuses (the setup page) — R-49, Phase 5.6

The category logic above always works, but sometimes a user wants to say exactly which
statuses their project uses and control which ones the tool moves tickets between. The
setup page gives them that, without losing the safety net.

**How it works, in plain terms.** The first time the app runs (and later from settings),
the user sees a setup page. The tool fetches their project's real statuses from Jira and
lists them. Next to each status is a **meaning** — the job the tool does at that status:
ready-to-pick-up, work-started, in-review, blocked/needs-human, or done. The tool
**guesses** each meaning from the status's category and name (e.g. a status called "In
Review" → the in-review meaning), and the user just confirms or corrects it. The user
saves, and this **status map** (status name + id + meaning) is stored in the DB.

**What the map changes.** From then on, whenever the tool moves a ticket — work starts,
a PR opens (→ in-review), something gets blocked — it resolves the target status in this
order: **map → category → comment-and-leave.** First it uses the user's *declared* status
for that meaning. If the user never set that meaning, it falls back to the category logic
above. If neither yields a reachable status, it does not force anything — it posts one
comment on the ticket ("couldn't move to <meaning> — no matching status") and leaves the
ticket exactly where it is. So the tool only "gives up" when both the map and the category
come up empty.

**The safety net stays (this is the important part).** The category logic underneath does
not go away. If a ticket lands in a status the user did *not* declare, it is still
classified by category so nothing crashes; and a status whose category can't be resolved
at all is still "unknown" → comment on the ticket + escalate (R-28). So a forgotten or
brand-new status can never break the flow — the worst case is a polite comment asking a
human. Setting a declared status is still non-blocking: if the target isn't reachable,
the tool leaves the ticket where it is and warns, never failing the real work (R-11).

**Comments and status.** A human comment can cause a status change, but only *through the
gate* (R-48): the comment triggers an action, and that action's stage-boundary sets the
declared status. A comment never sets status directly. Ticket intake/picking (R-27) is
untouched by any of this.

**Token memory (R-42).** Related convenience built in the same phase: once a user enters
a GitHub token for a private repo in the UI, it's saved encrypted per repo, so they're
not asked again for a repo that already has a working token.

**The "confirm suggestion" checkbox.** On the setup page, statuses the tool is confident
about (category `new` → ready-to-pick-up, category `done` → done) get their meaning filled
in with no extra step. Statuses it is NOT confident about — generic `indeterminate`
statuses like "In Progress," which could be work-started, in-review, or blocked — default
to a best-guess meaning and show a **"Confirm suggestion"** checkbox. The checkbox is a
sign-off, not an action: the user either agrees (tick it) or corrects the meaning via the
dropdown first, then ticks it. The tool never changes a meaning on its own. The checkbox
state itself is not stored — only the final status + meaning are saved.
> **Known gap (browser-only enforcement, deferred polish).** The "must confirm" rule is
> enforced in the page (the checkbox is `required` in the browser), not on the server —
> a save request that bypasses the browser is not re-checked server-side. Harmless for
> single-user manual testing (you always tick it); worth hardening server-side before any
> multi-user use. Not on the critical path for the current build.

## 6. The guard (how failure-exit and contracts are enforced)

After **every** agent runs, the Orchestrator enforces a deterministic **guard** before
proceeding. The guard is where principle #5 lives. It runs in two stages:

**Stage A — schema validation, inside the node.** The agent's output is validated against
its Pydantic model *before* the state reaches the guard node. Invalid/incomplete output →
reject and retry (≤N), else escalate.

**Stage B — the guard node**, in this exact runtime order:

1. **Budget check.** Has this sub-task/ticket exceeded its call/cost/token ceiling? If yes
   → stop and escalate (don't spend another attempt). Checked first so a broke ticket
   never pays for a retry.
2. **Honest-failure exit.** Did the agent itself signal "I can't" (e.g. Diagnosis found no
   root cause, `status == needs_human`)? If yes → don't fabricate, don't retry; route to
   the human with what it found.
3. **Retry / loop limit.** Otherwise, has this loop (e.g. Critic↔Executor) exceeded N
   attempts? If yes → final honest-fail: write `failure_reason`, set
   `status = needs_human`, route to the UI. If not → allow the retry.

So the full order is **schema → budget → honest-fail → retry**. The unifying rule:
**validate the contract, check the counters, decide continue-or-escalate.** No agent
output is trusted until the guard passes it.

> The guard runs after the reasoning/action nodes (diagnosis, step-planner, each execute
> step, freshness, critic, and the safe-node-wrapped prepare/apply/publish/notify steps).
> It does NOT run after pure human-decision nodes (human_gate, intent_gate, escalate,
> human_resolution, apply_resolution), which carry no agent output to validate.

---

## 7. Isolation & memory (how reuse coexists with isolation)

- **Isolation:** each sub-task's `SubTaskState` is separate; agents on sub-task A
  physically cannot read sub-task B's state. This is enforced at the data layer (one
  row/thread per sub-task) not by convention.
- **Memory (the "main model" you wanted):** a separate service. When a sub-task
  starts, the memory service embeds the sub-task description, runs a **pgvector
  similarity search** over past *resolved* sub-tasks, and injects only their
  **outcomes/resolutions** into `memory_refs` — not their full context. When a
  sub-task finishes, its resolution is embedded and written back. This gives
  cross-ticket reuse through a *controlled lookup*, so isolation is preserved: the
  agent sees "here's how a similar problem was solved," not another sub-task's live
  context.

Memory is detailed in `memory.md`.

---

## 7a. Intent confirmation (understand before decomposing)

A ticket can ask for many things, and its intent can be ambiguous ("the login is
weird"). Before the Planner's decomposition is trusted, it **states its interpretation
and asks the human**: "I read this as 2 tasks — fix the auth bug + restyle the button.
Correct?" Only on confirmation does it proceed. This is an **intent-confirmation gate**
at the very front of the flow (after repo resolution, before/with decomposition). It
enforces the broader principle: **the system never changes anything without first
confirming with the human** — batched at the plan level so it stays usable, not a
prompt per micro-action.

## 7b. Cross-sub-task integration (agents as a team)

Sub-task isolation prevents hallucination, but it also means sub-task A (frontend) has
no idea it may have broken sub-task B (backend). So after all sub-tasks in a ticket are
individually complete — but **before any PR** — an **integration stage** (owned by the
Orchestrator) assembles the *combined* change across affected repos, runs the **full**
test suite, and checks for cross-breakage. The team guarantee: the whole change is
validated as one, not just each piece in isolation.
- All integrated checks pass → proceed to PR(s).
- Cross-breakage detected → escalate to the human with what broke, rather than shipping
  a change that passes per-sub-task but fails as a whole.

This is the one place the isolated pieces are deliberately viewed together. It only
exists once a ticket has multiple sub-tasks, so it is built with multi-sub-task support
(Phase 8).

## 7c. Verifiability (no tests / no CI)

A fix is only trustworthy if it can be verified. Before trusting any change:
- **Does the target repo have tests?** If not, the fix cannot be meaningfully verified
  → flag "no tests exist to verify this" at the human gate (honest weak-confidence
  signal), and optionally have the Executor **write a test** covering its own change.
- **Did the change add code with no coverage?** The Critic surfaces it.
An unverifiable change is never silently treated as safe — it is escalated to the human
explicitly. (This is R-11 / "always ask" applied to verification.)

---

## 7d. Cost & efficiency (fewer, cheaper calls)

The pipeline can make 10-20+ LLM calls per sub-task, so cost and latency are
first-class design concerns, controlled by:

- **Model tiering (biggest lever after reuse).** The mechanism is **plan-then-execute**:
  the expensive model does the reasoning *once* — searches, understands the code, and
  emits a spec detailed enough that a cheap model executes it without thinking or
  searching. Big model = reasoning that needs understanding (Diagnosis, ambiguous
  planning); cheap models = well-specified execution (applying a spec'd edit, parsing,
  routing, Step-Planner formatting). A **deterministic model router** (plain rules, no
  LLM: task type → tier) decides the tier per task — not everything goes to the big
  model. Configured per-agent (R-33). Typically cuts cost 60-80%. The quality bar moves
  to *spec completeness*: the big model must hand over exact locations/changes/expected
  outcome so the cheap executor never guesses; the Critic backstops thin specs.
- **Solution reuse (R-29).** A strong memory match skips Diagnosis + Step-Planner
  entirely — the two most expensive calls avoided outright.
- **Per-ticket budget (R-34).** Calls and estimated cost are tracked and capped per
  ticket; on exceed the guard pauses and asks the human rather than spending silently.
- **Structural minimisation.** Merge cheap sequential steps into single calls where it
  doesn't hurt modularity; send only relevant code slices, not whole files (the
  blackboard already passes structured fields, not transcripts); cache stable prompt
  prefixes.

The tension to respect: tier by *difficulty* (a too-cheap model on a hard job hurts
quality — the Critic is the backstop), and don't over-merge agents (you lose the
isolation and debuggability that make the system reviewable).

**Async I/O (concurrency without a second language).** The intent is that external I/O
(Jira, GitHub, OpenAI, DB) is non-blocking, so the poller/supervisor processes independent
tickets concurrently (R-35). Independent sub-tasks already run concurrently via
`asyncio.gather`. **Honest status (1–10 audit): PARTIAL** — the DB engine is still sync,
some HTTP is sync, and `asyncio.to_thread` offloading is applied inconsistently (a few sync
calls run directly inside `async def`, which can stall the event loop under load).
Finishing this (true async DB/HTTP or consistent offloading) is **remaining Phase 10
hardening** — still in the Python stack, no Go.

**LLM response caching (Phase 9, when there's traffic to cache).** A two-tier cache —
exact (SHA-256 → response) then semantic (pgvector, cosine ≥ ~0.92) — checked before any
model dispatch, cuts repeat cost hard. It lives inside the Python LLM client using the
same Postgres/pgvector already present; no separate service and no Go gateway (a
hash+lookup is microseconds in Python). Built with the memory phase because it can only
cache calls once the agents make them (R-36).

---

## 7e. Repository intelligence & code understanding (Phase 11)

**The problem this solves.** To fix code well, the tool must *understand* the code it is
about to change — the function, what calls it, what it touches, what might break. On a
small repo the Diagnosis agent can just read files. On a large repo (thousands of files),
reading blindly either misses the relevant code or sends so much code to the LLM that the
cost explodes. So the tool needs to **find and understand the change and its neighbourhood
on demand** — without pre-digesting the whole repo, and without breaking isolation.

**This is not a separate tool.** Phase 11 does not add a parallel product bolted onto the
side; it gives the agents you already built a **shared code-understanding brain**. The same
Planner, Diagnosis, Executor, Critic, integration stage, and blackboard stay exactly as
they are — they just gain the ability to *ask* "where is the relevant code, what calls it,
what does it touch?" instead of reading blindly. Concretely: a new deterministic
repository-intelligence *service* (the third storage layer) plus a set of query *tools*,
surfaced through one new investigator *agent* whose result lands on the same blackboard the
rest of the flow already reads. Nothing about the existing flow is replaced — it gets
smarter at the one thing that was weak (finding the right code in a big repo).

### The third storage layer

The system now has three storage layers, separated by lifetime and scope:

| Layer | Holds | Lifetime | Scope |
|-------|-------|----------|-------|
| Blackboard (`SubTaskState`, §5) | diagnosis, plan, steps, approvals, PR | one ticket | one sub-task |
| Ticket memory (`subtask_memory`, memory.md) | problem + solution summaries | permanent | past resolved work |
| **Repository intelligence** (this section) | files, symbols, calls/imports/refs, SQL, embeddings, knowledge graph, indexed commit | persistent | the repository |

Repository intelligence is *what the code is*; ticket memory is *what we learned while
fixing problems*. Keep them separate.

### How it stays isolation-safe

Repo intelligence is derived from the **shared source repository**, not from any ticket's
private `SubTaskState`. So two tickets on the same repo can both read "what does this repo
look like" while still being unable to see each other's live state — exactly the memory
model (finished, shared facts; never live private context). Two guarantees make this
safe: **(a)** a ticket may only read intelligence for a repo it is authorised to touch
(private repos are access-scoped), and **(b)** the layer stores structure and paths only —
never secrets or credential-bearing code (extends M-3).

### Deterministic facts first, agent reasoning on top

Everything factual is built by **deterministic code, no LLM**: AST parsing, symbol
extraction, imports/calls/references, SQL reads/writes, code chunking, embeddings, and
the knowledge-graph edges (CALLS, IMPORTS, REFERENCES, READS/WRITES, CONTAINS). Proven
edges are stored as fact; any AI-suggested edge is marked `inferred` with a confidence
and never mixed in. Then exactly **one** reasoning agent sits on top — see below.

**Graph = navigation, code = evidence.** The graph only points where to look. Before the
tool acts on or answers anything, it opens the real source and verifies. The graph is
never the source of truth.

### The Code-Intelligence agent + its tools

The many capabilities are **deterministic tools**, not agents: `search_exact` (grep/rg),
`search_semantic` (hybrid embedding search), `find_symbol`, `get_callers`, `get_callees`,
`get_references`, `get_reads_writes`, `get_file`, `get_diff`. One **Code-Intelligence
agent** (the 7th agent, an investigator) orchestrates them for a ticket:

```
Ticket question
   → decide what kind of investigation this needs
   → exact search + semantic search  → candidate fusion + dedupe → rerank to a shortlist
   → inspect the shortlisted code
   → follow callers / callees / references (graph traversal)
   → form a hypothesis
   → VERIFY against real source
   → return: relevant files, functions, execution path, confidence
```

This output **feeds Diagnosis** — Diagnosis no longer "reads the repo" blindly; it reasons
about the fix from the verified slice + execution path the investigator supplies. The
agent is bounded (R-8: a hard cap on investigation steps, then return the best hypothesis
at lower confidence — an honest exit, R-10), runs under the guard (R-9), returns
Pydantic-validated output (R-2), and respects the per-ticket budget (R-34).

### Progressive indexing: cold → warm → hot

The tool does **not** fully graph a huge repo before a tiny ticket. It indexes lazily and
persists what it discovers, so a repo gets "warmer" over tickets:

- **Cold** (never seen): scan first — file inventory, language detection, symbol
  extraction, lexical index, **and embeddings for all searchable code chunks** (global
  semantic discoverability — needed so an ugly, badly-named repo is findable at all).
  Start solving. Keep only the *deeper* work lazy: expensive cross-file analysis, deep
  data-flow, inferred semantics, graph expansion beyond core edges.
- **Warm** (seen before): reuse stored intelligence; on a new commit, update
  **incrementally via git diff** — reparse only changed files, update their
  symbols/embeddings/edges, drop stale ones; bump the stored `indexed_commit_sha`. Never a
  full re-index.
- **Hot** (many tickets): rich symbols, embeddings, graph, and historical fixes make the
  agent considerably stronger on that repo.

So 100 tickets against one repo ≈ 1 initial analysis + small incremental updates + 100
ticket-specific investigations — not 100 full analyses. Repo size mainly affects
first-time indexing, not the code sent to the LLM per ticket.

### Ticket startup, with the layer in place

```
Ticket → resolve repo → get target branch/commit
   → repo indexed at this commit?
        yes → use it
        no  → git-diff from indexed commit → incremental update
   → Code-Intelligence agent retrieves the relevant slice (+ verifies)
   → memory returns similar solved tickets
   → Diagnosis → Step-Planner → gate → Executor → Critic → PR
```

### Where the clone still fits (unchanged)

Cloning is unchanged: to *edit* code and open a PR the tool still clones the repo into an
ephemeral working dir (§8a). The clone is the *working copy*; repository intelligence is
the *understanding*. Different things, different lifetimes — the clone is throwaway
per-ticket, the intelligence persists per-repo.

### Getting it right on real (messy, large, concurrent) repos

Several things separate a demo from a tool that survives a 50,000-file legacy repo — all
required (see R-50/R-51):

- **Embed everything searchable at cold-index** (above) — global discoverability, or you
  can't find the area you'd need to embed.
- **Provenance on every fact.** Each symbol/edge records source file + lines, the
  extractor, the commit, confidence, and PROVEN vs INFERRED — so the graph can be updated
  safely later and never silently trusted.
- **Branch/ref awareness.** Key intelligence by `repo + ref + commit` (a RepositorySnapshot),
  so a ticket targeting `release/2026` never reads intelligence built from `main`.
- **Exclusions.** Respect `.gitignore` + known vendor/generated dirs, binaries, minified
  and lock files, max file size — with per-repo overrides. Indexing junk explodes cost.
- **One index job per repo/ref (concurrency lock).** Two tickets on the same repo can't
  both mutate the index; the second waits/reuses. No interleaved edge writes.
- **Explicit states + atomic swap.** `NEW/INDEXING/READY/PARTIAL/STALE/FAILED/UPDATING`;
  build a new snapshot, validate, then swap — a ticket never reads a half-built graph.
- **Deterministic reranker**, not a hidden LLM call (lexical + semantic + symbol-match +
  graph-proximity + file-type, or reciprocal-rank fusion).
- **Config/build/dependency files are first-class** (deps, versions, build/test commands,
  CI, runtime config) — many tickets live there, not in source.
- **Static-analysis blind spots** (reflection, DI, config-driven wiring, generated SQL)
  are acknowledged: inspect config/tests/build, and if still uncertain return LOW
  confidence — never pretend the graph is complete.
- **Symbol resolution evolves** lexical → AST/language-aware (tree-sitter/language server)
  with lexical fallback; and the **edge schema grows** beyond a call graph (EXTENDS,
  ROUTES_TO, PUBLISHES_TO, USES_CONFIG, TESTS, …).

### It serves the whole flow, not just Diagnosis

The investigator agent is the *primary* consumer (it produces `code_context` for
Diagnosis), but the underlying deterministic tools (`search_exact`, `find_symbol`,
`get_callers/callees/references`, `get_reads_writes`, `get_file`, `get_diff`, plus the
graph) are shared infrastructure the existing agents call where it helps:

- **Diagnosis** — reasons from the verified `code_context` slice instead of reading blindly
  (primary path).
- **Executor** — before a surgical edit, checks `get_callers`/`get_references` on the symbol
  it's about to change, so it doesn't silently break callers elsewhere in the repo.
- **Critic** — can ask "did this change miss a caller or a reference?" via the graph when
  validating the result against the ticket.
- **Integration stage** — the cross-sub-task breakage check (§7b) is itself a
  code-intelligence question ("does sub-task A's change touch something sub-task B relies
  on?"); it uses the same graph/reference tools rather than a separate mechanism.

All of them read/write through the same blackboard and obey the same guard/budget rules
(R-9/R-34). So the brain is one shared capability the whole existing flow taps — not a
module only Diagnosis talks to.

### Built later, as its own track (why)

This layer is built in **Phase 11**, after the spine (8–10) is proven — same reasoning as
the memory layer: building a graph and vector search on an unproven pipeline is hard to
debug, and by Phase 11 there are real tickets and repos to drive lazy indexing. It is
built progressively (lexical → repo-intelligence service + persistence → hybrid retrieval
+ rerank → knowledge graph + verification → the investigator agent). The heavier
code-intelligence-*product* features (impact analysis, data lineage, branch-diff graphs,
confidence dashboards, AI-inferred edges) are designed-for but deferred beyond the base
layer.

---

## 8. Surgical edits (the Executor's edit engine)

The Executor never rewrites whole files and never writes to disk directly. For each
edit it emits a **SEARCH/REPLACE block** (quote the exact existing code + its
replacement). A deterministic applier then:

1. **Validate markers / pre-process** — reject malformed blocks; strip a spurious
   leading blank line if the SEARCH block is >2 lines and starts blank.
2. **Resolve each block to exactly one match** via a cascade:
   - **Exact** (character-for-character)
   - **Whitespace-normalised** (outdent to common minimum, match, restore original
     indentation on write)
   - **Fuzzy** (`difflib.SequenceMatcher`, accept only if ratio ≥ 0.8)
   - **0 matches →** self-correct: return `SearchReplaceNoExactMatch: did you mean
     [closest lines]?` to the LLM, retry (≤N)
   - **>1 matches →** fail closed: reject, ask the LLM for more surrounding context to
     make the block unique
3. **Apply bottom-up** — resolve all block positions against the *unmodified* file,
   then apply highest-index-first so earlier edits don't shift later matches.
4. **Post-apply guard** — before writing to disk: lint/syntax check, and sanity
   checks (abnormal line-count drop? emptied block? unexpected shrink?). Fail →
   rollback from the in-memory buffer, count as a failed attempt, retry (≤N).
5. **Write → run tests → next step.** N failures anywhere → escalate to human.

Invariant: **the LLM generates the edit; deterministic code matches, applies, and
guards it.** Every edit resolves to exactly one match or it does not apply — silent
corruption is impossible.

**Deferred (designed-for, not built early):** AST-based editing for structured config
files (JSON/YAML/TOML); script-generation for very large (>1000-line) files.

---

## 8a. Workspaces (ephemeral scratch — the laptop holds nothing durable)

Code is edited in a **local clone**, but the clone is pure, throwaway **scratch space**.
Everything durable lives elsewhere: the **change** is pushed to a GitHub branch/PR
(source of truth for code); the **summary of what was done** is on the Jira ticket and in
the DB event history (source of truth for the narrative). So the clone has zero lasting
value after the push — deleting it loses nothing.

**Isolation.** Each sub-task gets its own clone directory (e.g.
`/tmp/agentic-workspaces/<ticket>_<subtask>/`), its own branch. Sub-tasks never share a
working directory, so concurrent work can't stomp on each other's files — file-level
isolation matching the state-level isolation.

**Freshness (R-20).** Because diagnosis may happen long before the human approves, the
Executor **clones/pulls fresh right before editing**, never trusting a stale clone from
diagnosis time. And it checks against the snapshot the plan was built on: the
`diagnosed_commit_sha` recorded in `code_context`. If the source moved but the *affected
files* are unchanged, the approved plan stands; if the affected files changed, refresh
Code-Intelligence + Diagnosis before editing. This cleanly handles the case where a human
sits on the gate for 40 minutes while `main` advances underneath.

**Space — the key property.** Clones are **shallow** (`--depth 1`: latest commit only,
not full history) and **deleted when the sub-task reaches a resting state** — PR opened
(success), escalated to human, or abandoned. During an active retry loop
(Critic-reject → re-edit, test-fail → retry) the clone is kept until the loop resolves,
then deleted. A later reopen/retry **re-clones fresh** (cheap, and freshness-correct).
Because clones never accumulate — they exist only during active editing — disk usage
stays near-zero regardless of ticket volume. A **disk/workspace guard** pauses and alerts
if space runs low rather than filling the disk (R-11 applied to disk).

**Why this works:** the clone was never the memory — the ticket + DB summary are. A
future re-pickup reads that summary to understand history without needing the old clone.
The laptop is pure compute scratch.

**Deferred (scale option):** a shared per-repo cache + git **worktrees** (isolated
working dirs sharing one set of git objects) instead of independent clones — only if
clone time/space is ever a *measured* bottleneck.

---

## 9. The interface (how you see it work)

- **FastAPI** backend serves a simple web UI.
- **Live streaming (SSE)**: every time an agent activates, produces output, hits the
  guard, or pauses at a gate, an event streams to the browser so you watch the
  trajectory in real time.
- **The human gate** appears in the UI (and as a Jira comment): the plan + reasoning,
  with approve/reject. It can be answered from **two channels** — the UI button, or a
  Jira **comment reply** ("APPROVE"/"REJECT") that a permission check gates to the
  assignee/allowed users only (R-38). Both drive the same checkpointed resume; noise and
  unauthorised replies are ignored. Approving resumes the flow.
- Because state is checkpointed, you can refresh, close the tab, or restart the
  server and the run continues from where it paused.

---

## 10. What is intentionally deferred (so early phases stay shippable)

These are real and designed-for, but built later — never let their absence block the
core loop:

- Parallel execution of independent sub-tasks (single-flow first)
- The full memory layer (spine works without it)
- AST config edits and large-file script mutation
- Multi-user / auth / deployment
- Trajectory/context compaction for long *agent histories* (distinct from code slicing,
  which is handled by the Phase 11 code-intelligence layer, §7e)
- MCP / A2A protocols (only needed if agents ever run distributed)

> **Note:** understanding large codebases (retrieval, symbols, knowledge graph, code
> slicing) is no longer "someday" — it is **Phase 11 (code intelligence, §7e)**. Only the
> heavier *code-intelligence-product* features on top of the base layer (impact analysis,
> data lineage, branch-diff graphs, confidence dashboards, AI-inferred edges) remain
> deferred beyond it.

The build order in `codex_prompts.md` follows this: **spine first, breadth and polish
later, each phase producing something you can see and test.**

---

## 11. Glossary

- **Ticket** — the incoming request from Jira; may contain multiple asks.
- **Sub-task** — one isolated unit of work the Planner extracted from a ticket.
- **Step** — one edit-sized action the Step-Planner extracted from a sub-task.
- **Blackboard / state object** — the shared, per-sub-task source of truth.
- **Guard** — deterministic post-agent validation + counters + escalation.
- **Gate** — the human approval pause.
- **Cascade** — the tiered SEARCH/REPLACE matching strategy.
- **Checkpoint** — persisted state enabling pause/resume/crash-recovery.
