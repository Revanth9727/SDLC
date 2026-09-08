# agent_context.md — Persistent Context for Codex (Keep This Loaded)

> **Purpose.** This is the short, dense file you keep in Codex's context at all times
> (pin it, or paste it at the top of a session). It is NOT the full spec — that's
> `architecture.md` and `ai_rules.md`. This is the one-page briefing so Codex always
> knows what the project is, how it's structured, and the non-negotiable rules, even
> in a fresh session. When a Codex prompt says "obey the rules," this is what keeps
> them in view.
>
> If this file and the full docs ever disagree, the full docs win — tell Codex to
> re-read `architecture.md` / `ai_rules.md`.

---

## What we're building (1 sentence)

An agentic SDLC system: a Jira ticket → agents split it into isolated sub-tasks →
plan each → human approves → read the GitHub repo → make surgical edits → run tests →
open a real PR, all watchable live in a FastAPI web UI, with checkpointed state so
nothing is ever lost.

## Stack (fixed)

- Python **3.11**, FastAPI + Jinja + SSE (live streaming UI)
- **LangGraph** for orchestration, with a **Postgres** checkpointer
- **Postgres 16 + pgvector** in Docker (`agentic_sdlc_db`)
- **OpenAI** behind one swappable model-client (`OPENAI_MODEL` env var)
- Integrations: **Jira** (tickets), **GitHub** (PRs) — both real
- Validation: **Pydantic** everywhere on handoffs
- Tests: **pytest**, per phase, under `tests/phaseNN/`

## The 6 agents (reason with an LLM)

1. **Planner** — ticket → isolated sub-tasks + dependency graph (only thing that sees all sub-tasks)
2. **Step-Planner** — one sub-task → ordered coding steps
3. **Orchestrator** — schedules sub-tasks, runs gates, manages flow + PR strategy
4. **Diagnosis** — reads repo, finds root cause for one sub-task
5. **Executor** — applies approved steps as surgical edits, runs tests
6. **Critic** — validates result vs. the ticket before PR

## NOT agents (deterministic code — no LLM)

Jira I/O (incl. **dynamic status sync**) · GitHub ops · test runner · edit applier
(SEARCH/REPLACE cascade) · memory similarity search · the guard. (Only exception:
memory may make ONE LLM call to summarise a resolution for storage.)

## Jira status sync (R-25)

Orchestrator sets ticket status at each stage boundary via a config-mapped, dynamic
tool: `in_progress` (work starts) → `awaiting_approval` (human gate) → `in_review`
(PR opened) → `blocked` (escalation) → `done`. Statuses are discovered at runtime and
matched by name from config; a missing status is skipped with a warning, never blocks
work. Never hardcode status names/IDs.

## The flow

```
Jira (poll every N min, claim only "To Do") → [RESOLVE REPOS: cascade + confirm gate]
  → PLANNER → sub-tasks → ORCHESTRATOR picks one
  → per sub-task (ISOLATED state, scoped to ONE repo):
       DIAGNOSIS → STEP-PLANNER → HUMAN GATE (pause) → EXECUTOR → CRITIC → [PR] → [memory write-back]
  → next sub-task → final report
```
Every arrow = read/write the blackboard. Every world-action = a tool call. Every step
= checkpointed.

## Intake: polling + claiming (R-27)

Poll Jira every `JIRA_POLL_INTERVAL_MINUTES` (default 30). Pick up ONLY tickets in
"To Do"; leave every other status alone (may be in-flight or awaiting a human). Claim
atomically: mark in local DB + flip to In Progress BEFORE any work, so no ticket is
claimed twice even across overlapping polls. Multiple ready tickets run sequentially
now; true parallelism (independent whole tickets, then independent sub-tasks) is
Phase 10.

## Supervisor: Jira is the truth (R-28)

The app is a SUPERVISOR watching tickets continuously; the DB is a cache, not the truth.
Each cycle it reconciles tickets in scope (active runs + Jira tickets updated since last
sweep — so a REOPENED old ticket re-enters automatically, never errors). Drift handling:
back-to-To-Do → un-claim + re-pick; moved-to-Done/blocked → stop AI run; reopened → treat
active again; unknown status (not in config) → escalate + needs_human. Ownership = "is
there an active AI run?" ISOLATION absolute: only that ticket's own row + history, never
another's. Comments are history-aware (state what's actually done). ESCALATION CANNOT
FAIL: always comment + @mention owner; try-and-skip status; email deferred — a ticket
never falls through a crack. Stuck past `STUCK_THRESHOLD_MINUTES` → one history-aware
comment @mentioning owner (AI: no status change; human: "what's blocking?"); never nag a
healthy run. Built in Phase 2.5.

## Webhooks: event-driven fast path (R-37)

Polling is the fallback; webhooks are the fast path. FastAPI endpoints receive
GitHub/Jira webhooks (Python, no Go), idempotent by delivery id, extract the Jira key
from PR title/branch/commits, keep a PR↔ticket linkage table. Both webhooks + polling
reconcile into the SAME logic. PR-state matrix: reopened ticket + MERGED PR → never
un-merge, comment "new PR needed"; open/closed PRs → context comments. Built Phase 5.5
(after PRs exist). Go edge receiver only if webhook volume is a measured bottleneck.

## Repo resolution (R-26)

A ticket's target repo is NOT hardcoded and a ticket may touch multiple repos. A
deterministic resolver runs a cascade — web links → description → (best-effort)
reporter repos → ask user to paste — and the result is ALWAYS confirmed by the user
before work starts. The Planner assigns each sub-task its repo during decomposition
(no separate repo agent). Each sub-task is isolated to ONE repo. `GITHUB_REPO` in
`.env` is sandbox/testing only.

## The blackboard (one per sub-task = isolation boundary)

`SubTaskState`: ticket_id, subtask_id, subtask_type, **repo (owner/repo)**,
depends_on, diagnosis, plan, current_step, steps_done, approval_status, retry_count,
budget_used, status, failure_reason, pr_url, memory_refs. Agents read/write named
fields only.

## The 10 rules that matter most (full set in ai_rules.md)

1. Agents never call each other — only the blackboard (R-1)
2. Every handoff is Pydantic-validated (R-2)
3. LLM reasons; tools act — model never touches files/git/APIs directly (R-5, R-6)
4. Every loop has max-N and a human exit; never unbounded (R-8)
5. A deterministic guard runs after every agent (schema → retry → budget → honest-fail) (R-9)
6. Agents can say "I can't" — never fabricate when cornered (R-10)
7. Persist state after every node; resume after crash/restart (R-12)
8. Edits are SEARCH/REPLACE, exactly-one-match, guarded, reversible, bottom-up (R-14–R-16)
9. Secrets from env only, never logged; model swappable via `OPENAI_MODEL` (R-17, R-18)
10. Never touch main — branch + PR only; freshness-check before editing (R-19, R-20)

## Solution reuse (R-29)

Before the reasoning agents, memory searches top-K resolved tickets. Strong match
(similarity ≥ 0.9) → SKIP Diagnosis + Step-Planner: propose the past fix, freshness-check
it, then STILL run human gate + Critic + PR. Never blind-apply (similar ≠ proven; code may
have changed). Weak/no match → full pipeline. Saves heavy reasoning, never safety. Built
in Phase 7.5 (after full flow works, so resolved tickets exist to match).

## Intent, integration, verifiability (R-30/31/32)

- **Intent gate (R-30):** never change anything without a prior human gate (batched per
  plan, not per keystroke). Planner states its reading + asks before decomposing. Phase 7.
- **Integration stage (R-31):** sub-tasks are isolated, so after all are done and BEFORE
  any PR, assemble the combined change, run the FULL suite, catch cross-breakage (frontend
  breaking backend). Break → escalate, no PR. Phase 8.
- **Verifiability (R-32):** no tests in a repo → a fix can't be verified → flag at the gate,
  optionally write a test; Critic surfaces uncovered changes. Never assume safe. Phase 7.

## Cost & efficiency (R-33/34)

Many LLM calls per sub-task = real cost. Levers: (1) **Model tiering (R-33)** via
**plan-then-execute**: the STRONG model reasons ONCE and emits a spec detailed enough
that CHEAP models execute without searching/thinking (big=Diagnosis/ambiguous planning/
Critic; cheap=spec'd edit apply, parsing, routing, Step-Planner). A DETERMINISTIC router
(rules, no LLM: task type→tier) decides the tier — not everything goes to the big model.
Per-agent tier from config (`MODEL_CHEAP`/`MODEL_STRONG`), no hardcoded models — biggest
lever after reuse. Quality bar = spec completeness (Critic backstops thin specs).
(2) **Solution reuse (R-29)** skips the two priciest agents. (3) **Per-ticket budget
(R-34)** — track calls + cost, cap via `TICKET_CALL_BUDGET`/`TICKET_COST_BUDGET_USD`;
guard pauses + asks on exceed. (4) Structural: merge cheap steps, slice context, cache
prompt prefixes.

**Async (R-35):** all external I/O (Jira/GitHub/OpenAI/DB) is async so the supervisor
handles tickets concurrently without blocking — Python, no Go. Build from Phase 2.5 on.
**LLM cache (R-36):** exact (hash) + semantic (pgvector ≥0.92) cache inside the Python
LLM client, same Postgres — no separate service, no Go gateway. Built in Phase 9 (needs
agent traffic to cache).

- Build in the phase order from `codex_prompts.md`. **Do not build ahead.**
- Deferred (do NOT build until its phase): parallel sub-tasks, full memory layer,
  AST config edits, large-file scripting, multi-user/auth, context compaction, MCP/A2A.
- **Every phase must end with something visible in the UI AND a passing test.**
  If a prompt's result can't be seen or tested, stop and fix the approach.

## Project layout

```
app/agents/  app/core/  app/integrations/  app/db/  app/web/  tests/
```
- `core/` = state models, orchestrator, guard, model-client
- `integrations/` = openai, github, jira clients
- `db/` = schema, SQLAlchemy models, checkpointer setup

## When unsure

Re-read `architecture.md` (design) and `ai_rules.md` (rules). Prefer the smallest
change that makes the current phase's verification pass. Never expand scope to a later
phase to "finish" the current one.
