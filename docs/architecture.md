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
sub-task, running in its own isolated context: a **Diagnosis** agent reads the repo
and finds root cause, a **Step-Planner** breaks the fix into ordered steps, a
**human approves the plan**, an **Executor** applies surgical edits step by step, and
a **Critic** validates the result before a pull request is opened. Everything moves
through a shared, checkpointed **state object** (the "blackboard"), so no agent ever
loses context and the flow can pause or crash and resume exactly where it left off. A
separate **memory layer** lets a new sub-task look up how similar past work was
resolved, without breaking isolation.

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

## 3. The agents (6) and the non-agent components

### Agents (they reason with an LLM)

| # | Agent | Job (one line) | Decides |
|---|-------|----------------|---------|
| 1 | **Planner** | Split the ticket into isolated sub-tasks + dependency graph | *What* |
| 2 | **Step-Planner** | Split one sub-task into ordered coding steps | *How, in detail* |
| 3 | **Orchestrator** | Schedule sub-tasks, run gates, manage flow & PR strategy | *When* |
| 4 | **Diagnosis** | Read the repo, find root cause for one sub-task | — |
| 5 | **Executor** | Apply the approved steps as surgical edits, run tests | — |
| 6 | **Critic** | Validate the result against the ticket before PR | — |

> The Orchestrator is a coordinator; it does minimal reasoning (mostly scheduling and
> gate decisions). Some builds make it pure code — that's fine. It's listed as an
> agent because it may use an LLM for PR-strategy and ambiguous scheduling calls.

### Non-agent components (deterministic code — NOT agents)

- **Jira intake** — receive/poll tickets, post comments.
- **GitHub ops** — clone, branch, commit, push, open PR.
- **Test runner** — run the repo's tests, capture pass/fail + output.
- **Edit applier** — the surgical-edit cascade (match, apply, guard). See §8.
- **Memory service** — embed + pgvector similarity search + write-back. One small
  LLM call to summarise a resolution for storage; otherwise pure retrieval.
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
[RESOLVE REPOS]  (deterministic cascade + confirm gate — §5b)
   │
   ▼
PLANNER ──► splits into isolated sub-tasks + dependency graph
            (assigns each sub-task its repo from the confirmed list)
   │
   ▼
ORCHESTRATOR ──► picks next ready sub-task (parallel where independent)
   │
   │   ┌─────────── per sub-task, in ISOLATED state ───────────┐
   │   │                                                        │
   │   │  DIAGNOSIS ─► reads repo (tool), writes root_cause     │
   │   │      │                                                 │
   │   │      ▼                                                 │
   │   │  STEP-PLANNER ─► writes ordered steps                  │
   │   │      │                                                 │
   │   │      ▼                                                 │
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
   │   │  [GitHub ops] ─► branch, commit, open PR          │    │
   │   │      │                                            │    │
   │   │      ▼                                            │    │
   │   │  [Memory write-back] ─► store resolution          │    │
   │   └───────────────────────────────────────────────────────┘
   │                    │  (N rejects → escalate to human)
   ▼                    ▼
next sub-task …      done → final report on the ticket
```

Every arrow between agents is a **read-from / write-to the blackboard**, mediated by
the Orchestrator. Every action on the world (repo, tests, PR, Jira) is a **tool
call**, i.e. deterministic code. Every stage is **checkpointed**.

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

  # produced by agents (structured, validated)
  diagnosis            # {root_cause, files, reasoning}
  plan                 # [ordered steps]
  current_step         # index into plan
  steps_done           # [{step, edit, apply_result, test_result}]

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
`resolved_repos` is the confirmed list from §5b, which the Planner assigns across
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

## 5b. Repo resolution (which codebase does this ticket touch?)

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

## 5c. Jira status sync (keeping the board honest)

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

**Dynamic, not hardcoded (the key design choice).** Jira workflows are project-specific
and only allow *configured* transitions from the current status. So the system:

1. **Discovers** allowed transitions at runtime from the ticket's current status (Jira's
   transitions endpoint) — never assumes the full workflow.
2. **Maps** internal stages to status *names* via config (env), with optional fallbacks,
   matched case-insensitively.
3. **Degrades gracefully** — if a mapped status doesn't exist in the project's workflow,
   it logs a warning and continues. A status update NEVER blocks or fails the real work
   (see `ai_rules.md` R-25, which is R-11 applied to status).

This means the same code works across standard and custom Jira workflows with only
config changes, and a mis-mapped status is a harmless warning, not an outage.

## 6. The guard (how failure-exit and contracts are enforced)

After **every** agent runs, the Orchestrator runs a deterministic **guard** before
proceeding. The guard is where principle #5 lives. It does four checks:

1. **Schema validation.** Did the agent produce valid, complete structured output?
   If not → reject, retry (≤N), else escalate.
2. **Retry / loop limit.** Has this loop (e.g. Critic↔Executor) exceeded N attempts?
   If yes → stop, write `failure_reason`, set `status = needs_human`, route to the UI.
3. **Budget check.** Has this sub-task exceeded its token/time ceiling? If yes →
   pause and ask the human (catches slow sprawl that never technically loops).
4. **Honest-failure exit.** Did the agent itself signal "I can't do this" (e.g.
   Diagnosis found no root cause)? If yes → don't fabricate; route to the human with
   what it found.

The unifying rule: **validate the contract, check the counters, decide
continue-or-escalate.** No agent output is trusted until the guard passes it.

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

## 9. The interface (how you see it work)

- **FastAPI** backend serves a simple web UI.
- **Live streaming (SSE)**: every time an agent activates, produces output, hits the
  guard, or pauses at a gate, an event streams to the browser so you watch the
  trajectory in real time.
- **The human gate** appears in the UI (and as a Jira comment): the plan + reasoning,
  with approve/reject. Approving resumes the checkpointed flow.
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
- Context compaction for long trajectories
- MCP / A2A protocols (only needed if agents ever run distributed)

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
