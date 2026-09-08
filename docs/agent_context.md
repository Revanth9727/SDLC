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

Jira I/O · GitHub ops · test runner · edit applier (SEARCH/REPLACE cascade) ·
memory similarity search · the guard. (Only exception: memory may make ONE LLM call
to summarise a resolution for storage.)

## The flow

```
Jira → [intake] → PLANNER → sub-tasks → ORCHESTRATOR picks one
  → per sub-task (ISOLATED state):
       DIAGNOSIS → STEP-PLANNER → HUMAN GATE (pause) → EXECUTOR → CRITIC → [PR] → [memory write-back]
  → next sub-task → final report
```
Every arrow = read/write the blackboard. Every world-action = a tool call. Every step
= checkpointed.

## The blackboard (one per sub-task = isolation boundary)

`SubTaskState`: ticket_id, subtask_id, subtask_type, depends_on, diagnosis, plan,
current_step, steps_done, approval_status, retry_count, budget_used, status,
failure_reason, pr_url, memory_refs. Agents read/write named fields only.

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

## Build discipline

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
