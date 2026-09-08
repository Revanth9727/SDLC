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
