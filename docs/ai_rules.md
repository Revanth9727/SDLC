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

**R-8. Every loop has a hard limit and a human exit; retries resume in place and learn.**
Any retry loop (Critic↔Executor, edit self-correction, schema retry) has a max attempt
count `N`. On exhaustion: stop, write `failure_reason`, set `status = needs_human`,
route to the UI. Never loop unbounded. This is non-negotiable — it's the rule that
protects your API budget and your sanity.
Two requirements on how a retry behaves:
- **Resume in place.** A retry (e.g. after a Critic rejection) keeps the ticket/subtask
  ACTIVE and resumes from its checkpoint (R-12) — it must not drop the active state and
  error with "ticket no longer active" or demand a fresh manual approval each time.
- **Learn from the last attempt.** Each retry is fed the previous attempt's context — what
  it tried and the exact failure (error / Critic feedback / validator message) — so it
  doesn't repeat the same mistake. After the cap, the honest-fail `failure_reason` carries
  the accumulated attempt history, not just the last error.
Do NOT stack a new retry loop on top of the existing caps — reuse `MAX_AGENT_RETRIES`.

**R-8b. Retries are for REASONING failures, not INFRASTRUCTURE failures.**

*What it is.* A retry loop that re-runs an LLM agent only makes sense when the failure could
be fixed by the model trying again. A failure in the DETERMINISTIC tooling (a tool crash, a
missing manifest entry, a None-dereference, a DB/IO error, a bug in a validator) cannot be
fixed by asking the model again — retrying just burns the budget and delays the real
diagnosis.

*Why.* Example: the test-interface validator crashed on an unresolved imported type. The
Executor retried the LLM edit three times — but the model never had anything to do with the
crash; the *tool* was broken. Three wasted expensive attempts, ending in the same crash.

*Mechanism.* Classify a failure before deciding to retry:
- **Reasoning/output failure** (invalid model output, failed assertion on a valid test, edit
  that didn't match, schema-invalid response) → the model retrying may help → bounded retry
  (R-8).
- **Deterministic infrastructure failure** (an exception raised inside the tool/validator/
  guard/IO layer rather than from model output — e.g. AttributeError/KeyError/None-deref in
  the tooling, a resolver that couldn't run) → retrying the same agent CANNOT help → do NOT
  spend LLM retries. Stop, record structured context, and escalate to the human (or, if it's
  a resolvable tooling dependency, resolve it deterministically) — an honest-fail (R-10).

*Structured failure context (observability).* On ANY failure, persist enough to recognise it
later — never just the exception message. At minimum: component, function, file:line,
exception type, message, a sanitized stack/call site, and the relevant identifiers. E.g.
`component: test_interface_validator, function: validate_test_interfaces, line: 140,
owner: HardCheckResult, reason: interface_manifest_entry_missing`. Storing only
`"AttributeError: 'NoneType'..."` makes distinct bugs look identical and wastes debugging
time. Secrets are never included (R-17).

*Done / verification.* A deterministic tool exception does NOT trigger LLM retries — it stops
with structured context and escalates; a genuine reasoning failure still retries as before.
Failure records include component/function/line/reason, not just the message. Tests assert
both.

**R-9. The guard runs after every agent, before proceeding.**
Two stages. **(a)** Schema validation happens inside the node itself — the agent's output
is validated against its Pydantic model before the state ever reaches the guard node.
**(b)** The deterministic guard node then performs, in order: (1) budget check,
(2) honest-failure exit (accept a self-declared `needs_human`), (3) retry / loop-limit
check (with a final fallback honest-fail if the cap is hit). So the full runtime order is
**schema → budget → honest-fail → retry**. No agent output advances the flow until the
guard passes it. See architecture.md §6.
> Order notes: **budget before retry** is deliberate — if a ticket is already over its
> call/cost budget, don't spend another attempt retrying; stop and escalate.
> **Honest-fail before retry** is also deliberate — if the agent already said "I can't"
> (R-10), route to the human instead of blindly retrying. The guard runs after the
> reasoning/action nodes (diagnosis, step-planner, each execute step, freshness, critic,
> and the safe-node-wrapped prepare/apply/publish/notify steps); it does NOT run after
> pure human-decision nodes (human_gate, intent_gate, escalate, human_resolution,
> apply_resolution), which carry no agent output to validate.

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
target files haven't changed since diagnosis before the Executor edits. Use the snapshot
recorded on the blackboard: the `diagnosed_commit_sha` / `repo_snapshot_id` that
Code-Intelligence and Diagnosis were built on. Freshness = compare that commit to the
current source; if the *affected files* changed, refresh Code-Intelligence + Diagnosis
(re-run the slice) rather than editing a stale codebase; if they didn't, the approved plan
still stands. Don't edit against a commit the plan was never validated on.

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

**R-57. A re-picked ticket inherits its OWN prior attempt as advisory context — informed retry, not amnesiac restart.**
Today, when a ticket that already ran (failed → needs_human, or dragged back to To Do) is
picked up again, it starts completely fresh: a new empty sub-task, rebuilt from ticket text
only, discarding the prior attempt's diagnosis, plan, completed steps, and failure reasons.
That wastes expensive reasoning and lets it repeat the same mistake. Instead, a new run must
be given the PRIOR attempt on the SAME ticket as context:
- the prior **failure reason(s) / attempt_history** ("last attempt failed at step 3 because
  the generated test lacked an import") — so it doesn't repeat the mistake;
- the prior **diagnosis and plan**, and which **steps were already completed** — as HINTS.
Crucially, this is **advisory, not authoritative** (same spirit as R-29/memory): the new run
still RE-VERIFIES against the current code (freshness, R-20) before trusting any of it — the
codebase or the situation may have changed since the prior attempt. It never blind-resumes
completed steps or re-applies an old diagnosis without checking. This differs from R-29
(which reuses OTHER tickets' successful resolutions): R-57 is about THIS ticket's own prior
attempts, including FAILED ones — a failed attempt on the same ticket is the highest-value
hint there is, so the reuse/memory path must not filter it out here. Isolation still holds:
a run only ever sees its OWN ticket's prior attempts, never another ticket's.

**R-58. A reasoning retry must receive structured evidence from all prior attempts — not just the latest error string.**

*Principle.* R-8 requires that "each retry is fed the previous attempt's context — what it
tried and the exact failure." R-58 makes this precise: what that context must include, how it
is modelled, and how it accumulates within a retry cycle.

**R-58a. RetryAttempt — the reasoning evidence record.**
A `RetryAttempt` is distinct from `FailureContext` (operational/observability record). A
`RetryAttempt` is *reasoning evidence intentionally supplied to the next model attempt*. At
minimum it preserves:
- attempt number within the current cycle
- operation (e.g. `apply_edit`, `validate_test_semantics`)
- target (file or test path)
- candidate type (`edit_blocks` | `generated_test`)
- candidate fingerprint — computed from the **full normalized candidate content BEFORE any
  truncation** (see R-58d). This is the authoritative duplicate-detection key.
- candidate content — the SEARCH/REPLACE blocks or generated test body that failed. Persisted
  as a **bounded head+tail preview** capped at **8,000 characters**. If the content exceeds
  this limit, store a head slice + a tail slice and set `candidate_content_truncated = True`.
  The fingerprint is always computed from the full content, never the truncated form.
- `candidate_content_truncated` flag (`bool`) — set `True` when the above cap was applied
- failure type (`NoMatch` | `Ambiguous` | `GuardFailure` | `InvalidGeneratedTestError` |
  `RequirementContradiction` | …)
- failure reason (the exact message)
- deterministic failure evidence (closest-match lines + similarity score for NoMatch; match
  count and line ranges for Ambiguous; validation message for others)
- relevant requirement evidence (approved ticket requirement and active constraints, scoped to
  this step)
- corrective instruction (what the next candidate must do differently, derived deterministically
  from the failure type — not invented by the model)

`RetryAttempt` is Pydantic-modelled and persisted in the blackboard alongside `FailureContext`.
`FailureContext` remains for debugging and observability; `RetryAttempt` is the LLM-facing
evidence.

**State field shape.** Retry attempts are stored on `SubtaskState` as:

```python
retry_attempts: dict[str, list[RetryAttempt]]  # key = step_id
```

The outer key is the `step_id`. Each step has its own independent retry list. The number of
entries per step is bounded by the configured retry budget (`max_agent_retries + 1` candidates
maximum for a normal retry cycle). Historical entries from older or superseded steps remain in
the dict but are not treated as active retry evidence for the current step.

**R-58b. History accumulates per step; prior attempts are not overwritten.**
Within the retry cycle for a given step, attempt N receives the full list from
`retry_attempts[step_id]` — not only the latest entry. A mutable "latest error string" that
overwrites prior failures is insufficient; earlier entries contain evidence (e.g. the exact
failed SEARCH block, the closest-match text) that later attempts need. On graph/node restart
from a checkpoint, the persisted dict is fully available — cross-invocation history is
preserved by normal checkpoint restore.

**Step-id isolation.** A newly planned step with a new `step_id` always starts with an empty
list in `retry_attempts`. Historical evidence from older or replaced steps is accessible only
through the broader `attempt_history`/`prior_attempt`/advisory mechanisms (R-57) and must not
be treated as active retries for the new step.

**R-58c. Failed candidate must be part of retry evidence.**
- **For NoMatch:** the next attempt must receive the exact SEARCH block(s) that failed, not
  only the closest-match text. The model cannot reason about what to change without knowing
  what it proposed. The closest-match text from the applier is also included (it already is
  in the exception message — preserve it).
- **For Ambiguous:** the next attempt must receive the failed SEARCH text, the match count,
  and where the matches are (line ranges or surrounding lines for each), so it can construct
  a uniquely-identifying candidate rather than guessing what "more context" means.

**R-58d. Deterministic duplicate-candidate protection.**
Before passing a new LLM candidate to the execution layer, compute its deterministic
fingerprint (hash of candidate content). If the fingerprint matches any prior attempt in the
same retry cycle:
- do NOT attempt it
- count it as a failed attempt (it still consumes one retry budget slot — exempting duplicates
  from the budget count would allow unbounded retries)
- include the duplication as feedback in the next attempt (evidence that the model is looping)
Exact matching only — no semantic or fuzzy duplicate detection in this phase.

**R-58e. Exact duplicate protection applies to generated tests.**
The same fingerprinting (R-58d) applies to generated test content: an identical test body
produced again in the same retry cycle is rejected as a duplicate before execution. This is
deterministic content-hash matching only.

**R-58f. Ambiguity matching remains strictly deterministic (R-15 unchanged).**
Richer Ambiguous evidence (R-58c) is evidence FOR the next model attempt, not permission to
loosen the matching rules. An LLM must NEVER be given authority to decide "this looks like
the right location, apply it anyway." R-15 is unchanged: >1 match → fail closed, always.

*Done / verification.* A NoMatch retry prompt includes the exact failed SEARCH block;
an Ambiguous retry prompt includes the failed SEARCH text and match locations; all attempts
in a cycle accumulate (attempt 3 can see attempts 1 and 2); an identical candidate resubmitted
within one cycle is rejected, counted against the budget, and its duplication noted in the
next attempt's evidence; the list is bounded and checkpoint-safe. Tests assert each.

**R-59. Authoritative implementation constraints must reach the Executor — deterministically scoped, not keyword-matched or LLM-inferred.**

*What it is.* Human corrections and approved clarifications that are relevant to the current
implementation step must be explicitly present in the Executor's structured input as
authoritative constraints — not buried in `repair_feedback` alongside error strings, and not
omitted because they arrived through a different channel (approval gate, Jira comment).

*Sources of execution constraints.*
- **Approved ticket requirement** — always present; this is the specification.
- **Human-approved clarifications** from the approval gate (e.g. "do not treat empty strings
  as forbidden") or an approved revision note.
- **Critic corrections** that were accepted and amended into the plan.
- **Prior-attempt advisory** (R-57) where directly relevant to the current step's logic.

*What this is NOT.*
- NOT a dump of the raw Jira comment thread into every Executor prompt.
- NOT textual or keyword relevance matching — relevance is determined by explicit scope only.
- NOT an LLM call to decide which constraints are "relevant."
- NOT a replacement for `repair_feedback`; both must be present as separate named fields.

**Execution-constraint record.** Represent each constraint as a Pydantic-modelled record:
- `source`: `ticket_requirement` | `human_approval_note` | `critic_correction` |
  `prior_attempt`
- `text`: the constraint in plain terms
- `scope_type`: `ticket` | `subtask` | `step` | `file` | `symbol`
- `scope_value`: the ticket_id, subtask_id, step_id, file path, or symbol name — must match
  the current execution context for the constraint to be included
- `provenance`: where it came from (gate timestamp, Critic verdict ID, Jira comment reference)

**Deterministic relevance rule (concrete — no heuristics).** A constraint is included in the
`execution_constraints` field for the current step if and only if its scope matches:

| `scope_type` | Included when |
|---|---|
| `ticket` | Always — applies to every step in this ticket |
| `subtask` | `scope_value == current subtask_id` |
| `step` | `scope_value == current step_id` |
| `file` | `scope_value == current step.target_file` (exact path match) |
| `symbol` | `scope_value` is a symbol the current step targets or interacts with |

If multiple explicit scopes are present, a constraint is included if **any** scope matches.
No other relevance logic is applied — an unmatched constraint is excluded silently.

**Scope assignment rule for behavioral clarifications.** Human clarifications that redefine
ticket or subtask intent (e.g. "empty strings must be ignored throughout this ticket") must be
stored at `ticket` or `subtask` scope. They must NOT be stored at `step` or `file` scope and
then depend on a heuristic to propagate — broad behavioral corrections belong at the broadest
matching scope.

**Delivery.** Include only constraints whose scope matches the current step. Deliver them as a
named `execution_constraints` list field in the Executor payload — not embedded in
`repair_feedback`.

*Authoritative meaning.* A generated edit or test that violates an explicit execution
constraint is a **reasoning failure** → regenerate with the violated constraint as evidence.
This is not an infrastructure failure and does not skip the retry budget.

*Done / verification.* A `ticket`-scoped human clarification appears in the
`execution_constraints` field for every step in that ticket; a `file`-scoped constraint
appears only for steps whose `target_file` matches; a `step`-scoped constraint appears only
for its `step_id`; no constraint requires LLM or keyword inference to determine inclusion;
a candidate that violates any included constraint is rejected with the constraint named.
Tests assert presence and exclusion for each scope type.


Overlapping authoritative constraints with deterministically incompatible normalized behavior
must block execution until human intent is resolved. No LLM may choose authoritative intent
precedence. There is no latest-wins, human-over-ticket, or narrower-scope precedence.
Compare only explicit scope overlap in the isolated ticket/subtask and approved plan, and
only supported normalized subjects and PASS/FAIL behavior. Arbitrary text is UNKNOWN.
Identical records are deduplicated; advisory Critic/retry evidence is not authoritative.
Conflict detection admits `ticket_requirement` and captured `human_approval_note` records.
A Critic suggestion becomes authoritative only through explicit human approval capture;
merely labeling advisory data `critic_correction` or `prior_attempt` does not promote it.
Persist both constraint identities, original text, source/provenance, scope, incompatible
behavior, overlap evidence, and affected plan targets. This is a needs_human decision,
not an infrastructure failure or an Executor reasoning retry. The existing human-resolution
flow must support explicit withdrawal/replacement, clarification and re-planning, preserve
an audit of the decision, and recheck conflicts before execution can resume. Withdrawn
constraints must not be recreated from legacy requirement capture on restart.

**R-30. Never change anything without a prior human gate; confirm intent first.**
No state-changing action (code edit, PR, status forcing) happens without a preceding
human approval, batched at the plan level (one meaningful approval per sub-task's plan —
not a prompt per micro-action, which would be unusable). Additionally, when a ticket's
intent is ambiguous, the Planner states its interpretation and asks the human to confirm
BEFORE decomposition is trusted ("I read this as X and Y — correct?").

**R-31. Validate the whole change as a team; combine by deterministic merge, escalate only on ambiguity.**

*What it is.* Sub-task isolation prevents hallucination but hides cross-impact. After all
sub-tasks are individually complete and BEFORE any PR, an integration stage assembles the
combined change across affected repos, deterministically merges overlapping edits, runs the
FULL test suite on the combination, and only then publishes (R-31b).

*The threshold for escalating to a human is NOT "same file" or "same function" touched — it
is: can the system produce and VERIFY one unambiguous combined implementation?* If yes →
continue automatically. If no → human. Proximity of edits is not conflict; ambiguity of the
combined result is.

*Mechanism — deterministic merge, never an LLM deciding conflicts.* For each affected repo:
take the base, apply each sub-task's changes, and do a **3-way / line-level merge** (git-style)
— deterministic code, no LLM judging whether edits conflict.
1. **Textual merge conflict?** (same lines changed incompatibly) → cannot auto-combine → the
   ambiguity path (escalate, below).
2. **Merged cleanly? Still run the FULL combined test suite.** A clean text merge is necessary
   but NOT sufficient — the combined result can break logically even when git merges fine
   (e.g. two sub-tasks each add a fixture named `shared_fixture`: text merges, pytest fails on
   the duplicate). Behavioral verification is load-bearing.
3. **Combined tests pass** → proceed to publish the integrated artifact (R-31b).
4. **Combined tests fail** → analyze: can the system safely resolve it (a bounded repair the
   Critic can then re-verify)? If yes → bounded repair + re-verify. If not → escalate.

*The four outcomes (gradient — escalate only at the last).*
- **Normal overlap** — sub-tasks touch the same file in non-overlapping places (e.g. both add
  different test functions to one test file) → merge automatically, run combined tests.
- **Compatible overlap** — sub-tasks touch even the same function, but the 3-way merge is
  clean and combined tests pass → proceed. Do NOT bother a human just because the same
  function was involved.
- **Semantic interaction** — the merge is clean but combined behavior is wrong because the
  changes affect each other, i.e. the sub-tasks were not truly independent → record that
  evidence and attempt bounded reconciliation. Every reconciled candidate is run through
  the full combined suite and Critic again; prior proof never transfers to a changed artifact.
- **Unresolvable ambiguity** — the system cannot determine one correct combined behavior
  (a real merge conflict, or a combined failure it can't safely resolve) → HUMAN.

*The human escalation is a decision surface, not an approve button.* When it reaches a human,
the UI explains WHAT conflicts and WHY in plain terms ("S1 changed check() to handle None; S2
changed the same section to handle empty strings; both passed their own tests but can't be
auto-combined without deciding intended behavior") and offers real choices: (a) resolve
manually; (b) state the intended combined behavior → the agents build a new combined solution,
re-test, Critic re-reviews, then publish; (c) reject one change, keep the other; (d) re-plan
(these shouldn't have been independent sub-tasks). The LLM may help *resolve* once a human
sets intent, but never *decides* whether edits conflict — that's deterministic merge + tests.

*Done / verification.* Two sub-tasks adding different tests to the same file merge and publish
as one PR (not escalated). Two sub-tasks with a real line-level conflict, or a clean merge
whose combined tests fail unresolvably, escalate with an explained decision surface. The
merge decision uses no LLM. A single-sub-task ticket is unaffected.

*Reconciliation and infrastructure.* A model may propose a reconciliation only after the
deterministic merge succeeded and concrete combined-test evidence proves an interaction.
Attempts use the normal bounded retry cap. A merge-tool/filesystem/test-adapter crash is an
infrastructure failure (R-8b), consumes zero model retries, records `FailureContext`, and
escalates. A textual conflict is a valid deterministic result, not an infrastructure crash.
If reconciliation is ambiguous or exhausted, the human receives a decision surface:
resolve manually, state intended combined behavior, reject one change, or re-plan.

Record semantic interaction as structured evidence (`originally_independent=true`,
`integration_interaction=true`) for later replanning. One event does not silently retrain or
change Planner policy.

**R-31b. The published PR MUST be exactly the artifact that was tested and approved (critical).**

*What it is.* One invariant, non-negotiable: **approved artifact = tested artifact =
published artifact.** Whatever the integration stage actually tested is exactly what the PR
must contain — no more, no less.

*Why (the failure it prevents).* The integration stage (R-31) assembles the COMBINED change
of all sub-tasks (A+B+C) on one checkout and runs the full suite. If publish then opens the
PR from only `states[0]` (sub-task A) — or any single sub-task or subset — the PR contains
something that was never verified in that form. The tool reports PASS and opens a green PR
that is a lie: the green check attaches to a combined artifact that no human ever receives.
This is worse than a visible failure because it is SILENT — a reviewer trusts "verified"
on unverified code. It also invalidates any cost/verification claim about the system.

*Mechanism (how it must work).*
- The integration stage produces a single **integrated result** — the combined, tested
  working tree / diff per affected repo. This is the ONLY thing publish may open a PR from.
- `publish()` takes that integrated result as its input (not the sub-task list, not
  `states[0]`). It opens the PR from the exact commit/tree that the full suite passed on.
- **One ticket → one PR per affected repo.** If all sub-tasks touch one repo, that's one PR
  containing all their changes. If sub-tasks span N repos, open N PRs, each containing
  exactly that repo's tested integrated change — never mixing repos, never dropping a repo.
- This still satisfies one-open-PR-per-ticket (R-53): the integrated change (per repo) is
  the ticket's PR; the R-53 pre-publish "already an open PR?" check runs first, unchanged.

*Edge cases to handle explicitly.*
- **Single sub-task:** integration of one sub-task is that sub-task — the PR contains it.
  (This is the common case today; it must keep working.)
- **Dependent sub-tasks:** the integrated result already reflects the dependency order, so
  publish still just takes the integrated tree — it must not re-pick an individual sub-task.
- **Integration fails / cross-breakage:** NO PR (R-31). Never publish a subset "that passed"
  when the combination failed.

*Done / verification.* A ticket that decomposes into 3 sub-tasks, all approved and
integrated, opens a PR whose diff contains ALL THREE changes — verified by a test asserting
the published diff equals the integrated diff (not `states[0]`). A multi-repo ticket opens
one correct PR per repo. Integration failure opens none.

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
> **Missing-import repair is deterministic (tools act, not model-hope).** A generated test
> intermittently references a code-under-test symbol without importing it. When the AST
> import-validator detects this, the tool RESOLVES the symbol's real module via the
> code-intelligence symbol lookup (R-51) and AUTO-INSERTS the correct import — it does not
> just retry and hope the cheap model complies next time. Only if the symbol genuinely can't
> be resolved from the repo does it fall back to a retry (naming the exact missing symbol)
> or honest-fail. Never invent an import path; use the real resolved one. This fixes the
> file to satisfy the validator — it never weakens the validator. (A secondary, cheap
> mitigation: the test-generation prompt asks the model to list symbols + their imports
> before writing the body.)

**R-32b. A failing test is not automatically a wrong fix — validate the TEST before letting it drive a code change.**
The most dangerous verification failure is a bad test making the agent "fix" CORRECT
production code to satisfy it. A failing test has THREE possible meanings — (A) the
implementation is wrong (→ repair, R-32b), (B) the generated **test** is wrong (→ regenerate,
R-32b), or (C) the test is valid but the failure is a REAL bug OUTSIDE the ticket's scope
(→ stop and ask, R-32c) — and the tool must distinguish them before editing code.
- **Validate the generated test for internal consistency BEFORE running it / before treating
  a failure as an implementation bug.** A test whose own setup contradicts its assertion is
  invalid — e.g. the forbidden-phrase list contains `"forbidden"`, the fixture output
  contains "forbidden", yet the test asserts PASS. Deterministic checks catch obvious
  contradictions; the Critic reviews the test's semantics against the ticket for subtler
  ones.
- **The Critic evaluates two things separately: TEST validity** (does this test correctly
  represent the ticket/requirement?) **and IMPLEMENTATION validity** (does the code satisfy
  that valid test?). Because the same model wrote both the code and the test, a shared wrong
  assumption can appear in both — so the test is not trusted just because the code produced
  it.
- **On a failing test: first ask "was the test valid?"** If the test is invalid → REGENERATE
  THE TEST, do NOT modify production code to satisfy it. Only once the test is valid does a
  failure implicate the implementation (→ the repair loop). Never change correct code to
  make a broken test pass.
- **Repair path must not fight the state invariant.** When a failing (valid) test sends work
  back for repair, the flow rewinds `current_step`; the guard expects `current_step + 1` and
  rejects the returned state — an internal control-flow defect that breaks the repair loop.
  The repair/retry mechanism and the guard's step-progression check must agree: a legitimate
  rewind for repair is not a guard violation. Fix so a repair re-entry advances cleanly
  (resume-in-place, R-8) instead of being rejected by the step-increment invariant.

*How to detect an invalid test (mechanism).* Deterministic checks first, LLM only for the
subtle residue: (a) the test's own setup contradicts its assertion — e.g. a value is placed
in a "must-fail" input and the test asserts pass, or vice-versa; (b) the test asserts on
symbols/attributes that don't exist on the real interface (already covered by the
interface-grounding check, R-46); (c) the assertion contradicts the ticket's stated expected
behavior. What deterministic rules can't settle, the Critic judges as TEST validity against
the ticket text. Bound this like any loop (R-8): a fixed number of test regenerations, then
honest-fail — never an infinite regenerate cycle.

*Done / verification.* An internally-contradictory generated test (the forbidden-word
example) is caught and REGENERATED with production code untouched; a valid test that fails
still routes to the code repair loop; a repair re-entry is not rejected by the guard's step
check; and the regenerate loop is bounded. Tests assert each of these.

**R-32c. A valid test that uncovers a bug OUTSIDE the ticket scope is a distinct outcome — stop, record, ask; do not silently fix and do not retry.**

*What it is.* A THIRD failure category, separate from "implementation wrong" (repair) and
"test invalid" (regenerate): the generated test is valid for the ticket's intent, but it
fails because it exposed a REAL, DIFFERENT bug the ticket never asked to fix.

*Concrete example.* Ticket: "forbidden-phrases check should ignore empty entries." The tool
writes a valid test — a clean output should pass — using output "This output contains no
forbidden phrases." with a forbidden entry "forbidden phrase". It fails, because the check
does raw SUBSTRING matching, so "forbidden phrase" matches inside "forbidden phrases". That
substring bug is real but OUT OF SCOPE for this ticket.

*Why a distinct branch (the two wrong responses).* (1) Do NOT silently fix the discovered
bug — that changes code the human never approved (violates the intent/approval gate, R-30).
(2) Do NOT keep retrying the original fix — the failure is NOT evidence the ticket change is
wrong, so retrying burns the budget solving a problem it was never approved to solve.

*Mechanism.* After a VALID test fails, classify the failure cause: is the failure caused by
THIS ticket's change, or by an unrelated pre-existing defect the test happened to exercise?
- Caused by the ticket change → normal repair loop (R-32b).
- Unrelated discovered defect → STOP retrying, RECORD the evidence (what the test showed, the
  suspected root cause), and ESCALATE to the human with three options:
  1. **Stay in scope** — refine the test so it isolates ONLY the original requirement, finish
     the ticket, and FLAG the discovered bug as a separate issue. (Refinement is allowed ONLY
     if the refined test still correctly tests the original ticket requirement — e.g. swap the
     output to a neutral "This output is completely clean." which still tests "empty entries
     don't cause failure" without exercising the substring bug. NEVER change the test merely
     to go green — that is the R-32b danger.)
  2. **Expand scope** — the human adds the discovered bug to the approved plan; fix both.
  3. **Abort / escalate.**
The report to the human names the discovered bug precisely, e.g. "while validating this fix I
found a likely separate bug: forbidden matching uses raw substring semantics, so 'forbidden
phrase' matches inside 'forbidden phrases' — outside this ticket's scope."

*Guardrail on option 1.* Test refinement to isolate the original requirement is permitted;
test weakening to dodge a real failure is forbidden. The refined test must still fail if the
original ticket bug (empty-entry handling) were unfixed.

*Done / verification.* A ticket whose valid test exposes an unrelated defect does NOT loop on
the original fix and does NOT silently patch the unrelated bug; it stops, records the
evidence, and presents the three options. A test refined under option 1 still tests the
original requirement (would fail if the ticket bug were unfixed). Tests assert both.

**R-32d. A skipped or unresolved verification is NOT a passed verification — surface it to the Critic.**

*What it is.* When any deterministic check (interface validation, import validation, a
graph/reference lookup, etc.) cannot fully verify something, that "couldn't verify" is
UNCERTAINTY, not success. It must be persisted in subtask state and passed to the Critic as
evidence — never silently dropped, and never counted as if the check passed.

*Why.* Fix-forward robustness rules (e.g. "if a return type can't be resolved, skip checking
it and let the test proceed") prevent crashes — but if a skip reads as a pass, the tool
quietly approves things it never actually verified. That's the same failure class as treating
"no tests" as "safe" (R-56). A green result must never include invisibly-unchecked parts.

*Mechanism.* Every check reports one of: verified-ok / verified-failed / **unresolved-skipped**
(with a reason). Unresolved-skipped results are written to the blackboard as structured
records — e.g. `{owner: HardCheckResult, attribute: details, reason: return type could not be
resolved, source: tests/test_x.py, impact: assertion not statically validated}` — and
included in the Critic's input. The Critic MUST NOT treat a skipped/unresolved check as a
successful one: it weighs the uncertainty when deciding approval (it may still approve, but
knowingly, e.g. lower confidence or flag the unverified part), and surfaces it in its verdict
(R-45).

*Done / verification.* An interface (or other deterministic) check that is skipped for an
unresolvable target produces a structured unresolved record in state; that record reaches the
Critic; and the Critic's verdict reflects the uncertainty rather than treating the skip as a
pass. Tests assert the record is created, propagated, and visible to the Critic.

**R-32e. Assertion analysis must normalize semantic expected outcome, not just match syntax.**

*What it is.* The deterministic test validator (R-32b) analyses a test's assertions to
determine whether they contradict the setup or the ticket requirement. Checking only a narrow
set of exact AST patterns (e.g. `assert result.passed`) misses semantically equivalent or
inverted forms — in particular any negation (`assert not result.passed`) — that represent
the opposite expected outcome.

*Normalization.* Extract the expected outcome from every assertion on a result object as
`PASS` or `FAIL`:
- **PASS forms:** `assert result.passed`, `assert result.passed is True`,
  `assert result.passed == True`, and equivalents for `.ok`, `.valid`, `.success`, `.allowed`.
- **FAIL forms:** `assert not result.passed`, `assert result.passed is False`,
  `assert result.passed == False`, and the negated equivalents of all PASS attributes.
The normalizer must handle: direct attribute, `UnaryOp(Not, …)`, `is True/False`, and
`== True/False` comparisons. An assertion that does not fit any known form is logged as
`unresolved` (R-32d).

*Why.* A validator that only detects PASS forms treats `assert not result.passed` as
unclassified — leaving a semantically inverted assertion invisible to the check. Both the
fix and the test come from the same model; a shared wrong assumption makes both look correct
in isolation. The normalizer is the deterministic layer that surfaces the inversion before
an LLM retry is needed.

*Done / verification.* The normalizer correctly classifies PASS from all documented positive
forms and FAIL from their negations; `assert not result.passed` is classified as FAIL, not
left unclassified; all four AST shapes (attribute, `UnaryOp`, `is`, `==`) are covered for
both PASS and FAIL; tests assert each form and its inverse.

**R-32f. A generated test that contradicts the approved ticket requirement is invalid — regenerate, do NOT modify production code.**

*What it is.* An extension of R-32b: not only must a generated test be internally consistent
(R-32b), it must assert behavior that AGREES with the approved ticket requirement and any
approved human constraints. A test may be internally non-contradictory yet assert the exact
opposite of what the ticket asks for.

*Concrete example.* Ticket requirement: "Empty forbidden phrases should be ignored."
Generated test (after normalization — R-32e): expected outcome = FAIL for an empty-phrase
input. This contradicts the requirement, which specifies that empty phrases must cause a PASS.

*Why it matters.* The approved requirement is the authoritative specification. A generated
test that contradicts it is wrong. Letting it drive production-code changes silently inverts
the implementation. The model produced both the code and the test; a shared wrong assumption
makes both appear consistent with each other — so the test cannot be trusted just because the
code it generated satisfies it.


Scenario association (R-32 Phase 1): Internal consistency is evaluated per deterministic
scenario/result binding, scoped by function and assignment. Requirement alignment applies
only to scenarios safely associated through deterministic literal input or setup evidence,
checking every mapped scenario. Unrelated scenarios may legitimately expect different
outcomes; file-level MIXED is not automatically invalid. Preserve scenario evidence and
outcomes. Inability to map a requirement to a scenario resolves to UNKNOWN, never global
application or contradiction. Internal consistency still runs when alignment is UNKNOWN.

*Mechanism.*
- Requirement alignment evaluates every recognized outcome of every deterministically
  mapped scenario, never just the first scenario or assertion. Unmapped scenarios remain UNKNOWN.
- Contradictory recognized outcomes inside one generated test for the same result are
  invalid even when the ticket requirement itself is unresolved. Reject PASS + FAIL as
  an internal reasoning failure before pytest; regenerate the TEST only. Repeated identical
  outcomes are valid; unrelated assertions do not acquire PASS/FAIL semantics.
- Preserve the recognized outcomes in the structured result. Summarize uniform outcomes
  as PASS/FAIL, differing outcomes as MIXED, and no recognized outcomes as unresolved.
  MIXED only proves an internal conflict when both outcomes belong to the same scenario.
- **Deterministic layer:** if the requirement is unambiguous and the normalized expected
  outcome directly contradicts it (e.g. requirement says "empty entry → result passes" but
  the test asserts FAIL for empty entry), reject immediately without running the test.
- **Critic layer:** for subtler or indirect contradictions the Critic's TEST validity check
  (R-32b) evaluates the test against the ticket text — this is the existing residue path.
- A requirement-contradicting test is a **reasoning failure**: regenerate with the specific
  contradiction as feedback. Do NOT modify production code to satisfy it.
- Bound the regeneration loop (R-8).

*Critical invariant.* A generated test that contradicts the approved requirement MUST NOT
cause production code to be modified. The outcome: production code untouched; test
regenerated; feedback names the exact contradiction.

*Done / verification.* A generated test that (post-normalization) asserts FAIL for an input
the ticket says should PASS is rejected before execution; production code is not touched; the
regeneration feedback names the specific contradiction; the loop is bounded. Tests assert all
of these.

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

**R-35. I/O should be async; the supervisor handles tickets concurrently.**
The *goal* is that external I/O (Jira, GitHub, OpenAI, DB) is non-blocking so the poller/
supervisor processes independent tickets concurrently without stalling on one slow call.
FastAPI is already async, and independent sub-tasks run concurrently via `asyncio.gather`.
> **Current reality (honest status, as of the 1–10 audit):** this is only PARTIAL. The DB
> layer uses a *synchronous* SQLAlchemy engine, and Jira/repo I/O use sync `httpx.Client`;
> blocking calls are offloaded with `asyncio.to_thread` inconsistently (some sync calls run
> directly inside `async def`). It works today, but "async throughout" is not yet true —
> converting DB + HTTP to true async (or consistently offloading every blocking call) is
> **remaining Phase 10 hardening**, not a done item. Don't claim full async in new work.
(True parallel *agent* execution is Phase 10; consistent async I/O is the foundation still
being finished under it.)

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

**R-47. Ticket comments are a two-way chat, not one-way narration.**
The Jira ticket is a conversation: the tool narrates steps (R-40) AND reads + responds
to human replies (R-44), back and forth, like a chat thread. A human can ask, instruct,
or correct mid-flow; the tool reads it, responds (answer, or a gated proposal for
commands), and continues — so the ticket reads as a genuine dialogue between the human
and the tool, each aware of the other's messages (tool never treats its own messages as
input; permission-checked; ambiguity → ask). Comment classification uses the CHEAP model
(MODEL_CHEAP), never the strong one — it's simple intent detection, not reasoning (R-33);
only actual re-planning from feedback uses the planner's tier.

**R-48. The approval gate is a feedback loop: approve / reject-and-re-plan / revise.**
At the human gate, a decision (from the UI OR a Jira comment — both drive the SAME logic)
can be:
- **APPROVE** → resume the flow (execute the plan).
- **REJECT** → do NOT dead-stop. Re-plan: if the human gave a reason/feedback, generate a
  NEW plan addressing it; if no reason was given, ask "what should change?" then re-plan
  from the answer. The new plan returns to the gate for approval.
- **REVISE / SUGGESTION** (e.g. "reject, but also handle None" / "change step 2 to use
  try/except") → treat as feedback: generate a new plan incorporating the suggestion,
  back to the gate.
- **QUESTION** → answer it, stay at the gate (still awaiting decision).
Comment-based decisions match the pending gate for that ticket and call the identical
resume/re-plan path as the UI buttons — never a separate code path, never double-apply.
Permission-checked (R-38); re-planning is bounded (R-8) so it can't loop forever.

**R-49. The user declares which Jira statuses to use; categories remain the safety net.**
Extends R-25 (does not replace it). On first run, a setup page lets the user list the
exact statuses their project uses and assign each one a MEANING (its role in the flow:
ready-to-pick-up, work-started, in-review, blocked/needs-human, done). The tool then
moves tickets ONLY among these user-declared statuses. Two layers, in order:
- **Declared map is the target.** When the tool performs a transition (claim → work
  started, PR open → in-review, escalation → blocked), it moves the ticket to the
  user's declared status for that meaning.
- **Categories stay underneath (R-25).** The tool auto-suggests each status's meaning
  from its Jira statusCategory, and the user confirms/corrects it. Any status the user
  did NOT declare is still classified by category so nothing crashes; a status whose
  category can't be resolved at all is still "unknown" → comment + escalate (R-28).
- **Resolution order for any transition: map → category → comment-and-leave.** First use
  the declared status for that meaning; if the map has none, fall back to the category
  logic (R-25); if THAT also finds no target, do not force anything — comment on the
  ticket ("couldn't move to <meaning> — no matching status") and leave the ticket where
  it is. The tool only "gives up" (comments) when BOTH map and category come up empty;
  it never crashes and never silently drops the move.
Setting status remains NON-BLOCKING (R-11): if a declared target status isn't reachable,
skip with a warning and leave the ticket where it is — never fail the actual work. A
human comment can drive a status change only THROUGH the gate (R-48): the comment
triggers the action, and the action's stage-boundary sets the declared status — a
comment never sets status directly. Ticket intake/picking (R-27) is unchanged.
Token persistence (R-42) applies here too: a UI-entered GitHub token is saved encrypted
per repo, so the user is not re-prompted for a repo that already has a working token.
Built in Phase 5.6.

---

## H. Code intelligence (understanding the repo before changing it)

> Built in **Phase 11** (its own track, after the spine is proven). These rules govern
> how the tool understands a codebase — especially a large one — without reading it
> blindly or exploding cost. They extend, never replace, the isolation and
> deterministic-vs-agent rules above.

**R-50. Repository intelligence is a persistent, per-repo, DETERMINISTIC knowledge layer.**
This is the THIRD storage layer, separate from the per-sub-task blackboard (§5) and the
solved-ticket memory (memory.md). It holds what the *code is*: file inventory, symbols,
imports/calls/references, SQL reads/writes, code chunks + embeddings, and a knowledge
graph of proven relationships. All of it is built by **deterministic parsing (AST,
static analysis, SQL parsing, embedding calls) — never by an LLM deciding edges.**
- **Persistent + keyed by repo, REF, and indexed commit.** Key intelligence by
  `repo + ref (branch) + commit_sha`, not repo+commit alone — `main`, `release/2026`, and
  a feature branch can hold different code, and a ticket may target a specific ref. Model
  it as a **RepositorySnapshot** (repo_id, ref, commit_sha, indexed_at, status). You need
  not keep a full graph per branch — use the closest snapshot + diff — but ref-awareness
  is part of the design now, not an afterthought.
- **Incremental via git diff.** On a new commit, reparse only changed files, update their
  symbols/embeddings/edges, delete stale ones; never a full re-index.
- **Progressive / lazy — but embeddings are NOT lazy.** Do NOT fully graph a huge repo
  before a tiny ticket. BUT global semantic discoverability must exist at cold-index:
  embed *all searchable code chunks* up front (otherwise, in an ugly repo with no
  greppable names, you can't find the area you'd need to embed — a chicken-and-egg trap).
  Only the *deeper* work stays lazy: expensive cross-file analysis, deep data-flow,
  inferred semantics, graph expansion beyond the core edges.
- **Provenance on every stored fact.** Each fact/edge records where it came from: source
  file + line range, the extractor that produced it (e.g. `sql_parser`, `ast`), the
  commit, a confidence, and PROVEN-vs-INFERRED type. Six months later, safe updates depend
  on knowing what evidence created a piece of knowledge.
- **Facts before inference.** Proven edges are fact; AI-suggested edges are marked
  `inferred` with confidence + evidence, never mixed with proven ones.
- **Graph = navigation, code = evidence.** The graph points where to look; the tool
  ALWAYS opens and verifies the real source before acting or answering.
- **Exclusions (indexing policy).** Respect `.gitignore` plus known generated/vendor dirs
  (`node_modules/`, `vendor/`, `dist/`, `build/`, `target/`, `generated/`, `coverage/`,
  `.git/`), binary detection, a max file size, minified/lock-file detection — with
  per-repo overrides. Indexing garbage explodes cost and wrecks retrieval quality.
- **One index job per repo/ref at a time (concurrency).** If two tickets hit the same
  repo needing an update, they must not both mutate the index. Use a per-repo/ref indexing
  lock or an `index_jobs` table (repo_id, ref, target_sha, status); the second ticket
  waits for or reuses the first job. No interleaved edge writes.
- **Explicit index states + safe recovery.** An index is `NEW / INDEXING / READY /
  PARTIAL / STALE / FAILED / UPDATING` — never mark a half-built index READY. Prefer
  building a new snapshot then **atomic swap** (build → validate → swap active), so a
  ticket never reads a half-updated graph. A crash mid-index leaves `PARTIAL/FAILED`, not
  a corrupt READY.
- **Isolation + privacy.** Repo intelligence is derived from the *shared source repo*,
  not from any ticket's private state, so sharing it across tickets does NOT break
  sub-task isolation (R-1, R-28). But a ticket may only read intelligence for a repo it
  is authorized to touch (private repos are access-scoped), and the layer stores
  structure/paths only — never secrets or credential-bearing code bodies (extends M-3).

**R-51. One code-intelligence AGENT investigates; the capabilities are TOOLS, not agents.**
The many retrieval/analysis capabilities — exact search (`search_exact`/grep), semantic
search (`search_semantic`), `find_symbol`, `get_callers`, `get_callees`,
`get_references`, `get_reads_writes`, `get_file`, `get_diff` — are DETERMINISTIC tools
(R-5/R-6). Exactly ONE reasoning agent (the 7th agent, an *investigator*) orchestrates
them: decide what kind of investigation the ticket needs → search (exact + hybrid) →
rerank to a shortlist → inspect → follow callers/callees → form a hypothesis → verify
against source → return relevant files, functions, execution path, and a confidence.
Its result is written to the blackboard as the validated `code_context` field (R-1: the
blackboard is the only channel — Code-Intelligence never hands Diagnosis anything by a
side path), including the `repo_snapshot_id`/commit it was built on for freshness (R-20).
Do NOT create a LexicalAgent / EmbeddingAgent / GraphAgent / SQLAgent — that is
over-engineering; those are tools. The investigator **feeds Diagnosis** (it supplies the
verified slice + execution path; Diagnosis reasons about the fix from that), runs under
the guard (R-9), returns Pydantic-validated output (R-2), is **bounded** (R-8: a hard
cap on investigation steps, then return the best hypothesis at lower confidence — an
honest exit per R-10), and respects the per-ticket budget (R-34).

Additional requirements this agent and its tools must honour:
- **Deterministic reranker (not an LLM).** Candidate fusion → dedupe → rerank must use a
  defined deterministic score (e.g. lexical score + semantic score + symbol-match +
  graph-proximity + file-type relevance, or reciprocal-rank fusion), NOT a hidden LLM
  call. This protects the cost model (R-34).
- **Config/build/dependency files are first-class.** `pom.xml`, `build.gradle`,
  `package.json`, `requirements.txt`, `pyproject.toml`, `Dockerfile`, CI workflows,
  `application.yml`/properties, IaC — treated as understood artifacts (dependencies,
  versions, build/test commands, runtime config), not arbitrary text. Many tickets
  ("service breaks after upgrading library X") live almost entirely here.
- **Static-analysis blind spots are acknowledged, not hidden.** Reflection, dynamic
  imports, runtime DI, config-driven class names, generated/stored-proc SQL, plugin
  wiring — static parsing can't fully resolve these. When evidence is incomplete the agent
  inspects config/tests/build wiring, and if still uncertain returns LOW confidence rather
  than pretending the graph is complete (honest failure, R-10). The graph must never be
  assumed more complete than it is.
- **Symbol resolution evolves.** Lexical `get_callers/callees/references` is fine for the
  first prototype but is weak (grepping `save()` doesn't prove a call). The intended
  evolution is AST / language-aware symbol resolution (e.g. tree-sitter / language
  servers) with lexical fallback.
- **Graph is not only a call graph.** The edge schema is designed to grow beyond
  CALLS/IMPORTS/REFERENCES/READS/WRITES/CONTAINS to EXTENDS, IMPLEMENTS, OVERRIDES,
  ROUTES_TO/HANDLES, PUBLISHES_TO/CONSUMES_FROM, EXECUTES/INVOKES, USES_CONFIG,
  DEPENDS_ON, TESTS. Build the core edges first; don't hardcode assumptions that block the
  rest.
- **Shared across the existing flow, not just Diagnosis.** The investigator produces
  `code_context` for Diagnosis (primary path), but the deterministic tools are shared
  infrastructure the other existing agents call where it helps: Executor checks
  callers/references before a surgical edit (don't break callers), Critic uses
  callers/references for IMPACT ANALYSIS, and the integration stage's cross-sub-task
  breakage check uses the same graph/reference tools. Phase 11 is the existing flow gaining
  a shared brain — not a subsystem only one agent talks to.
- **Caller count informs impact; only evidence of breakage rejects (Critic).** Having many
  callers is a RISK SIGNAL to investigate, NEVER a rejection reason on its own — a shared
  utility is *supposed* to have many callers. The Critic must ask: did this change alter the
  CONTRACT those callers depend on (signature, return type/shape, raised exceptions,
  documented behavior)? If the contract is unchanged, many callers are irrelevant → do not
  reject on that basis. If the contract changed, the callers become relevant → verify the
  affected behavior (run affected tests / check the callers) and reject only on actual
  evidence of breakage. Rule: **dependency count informs impact analysis; evidence of a
  broken caller contract determines rejection.** Never treat popularity as danger.

**R-53. One shared publish step; one open PR per ticket; check BEFORE any LLM calls.**
All work that could open a pull request — whether it came from a normal ticket flow or a
human comment-command — goes through ONE shared publish step (a single "door"). There are
never two separate PR-opening code paths that can drift or be bypassed. Inside that step,
the **one-open-PR-per-ticket** rule is enforced, and the check happens **at the very front,
before spending any LLM calls** (so a request against a ticket that already has an open PR
costs nothing until the human decides — R-34).
- **No open PR for this ticket** → proceed and open one. (Open-PR status is read from the
  `pr_links` table + PR-state matrix built in Phase 5.5.)
- **An open PR already exists** → STOP before any reasoning/LLM work, post a **summary of
  the existing PR**, and **ASK the human** what to do: keep it, or replace it. NEVER assume
  the existing PR is complete/good and silently skip, and NEVER silently replace it. The
  human decides (propose-don't-auto-act, R-30/R-48).
- **Replace path (both supported):** the human may close the PR on GitHub (auto-detected
  via the PR-state matrix, which lifts the block), OR use a "redo PR" button/comment-command
  → the tool closes the old PR and opens a fresh one.
- **After a PR is accepted (merged)** → the code has changed, so re-read it (freshness,
  R-20) and make the next changes **incrementally** on top of the merged result — never
  stack a second PR on soon-to-be-stale code.
This replaces the older split where a "legacy"/comment path published via separate
`publish_pr`/`notify_pr` nodes; both paths now share the one publish step. Built as a
pre-Phase-11 consolidation.

**R-54. A light repo overview runs BEFORE the Planner splits; deep reading stays per sub-task.**
Before the Planner decomposes a ticket, a **cheap, deterministic** pass produces a
repo *overview* — file inventory + folder/module structure (reuses the cold-index
inventory; NOT embeddings, NOT the full graph, NOT investigation). The Planner uses this
overview to split the ticket more sensibly (e.g. recognising module boundaries). This is a
light "understand the shape first" step — explicitly NOT full repo reading before the
split, which would reintroduce the big-repo cost the per-sub-task design avoids. The
**deep** code understanding (Code-Intelligence investigation, R-50/R-51) stays scoped to
each sub-task, unchanged. The overview is deterministic (no LLM) and must be **visible**:
an "Understanding the repository…" phase shown before the sub-task cards appear (R-45).

**R-55. Each sub-task shows its own live timeline while running.**
Extends R-45. A sub-task card must not sit on a bare "running" label while a node
executes. It shows that sub-task's own live steps — reading/indexing → investigating →
diagnosis → edits → tests → critic — filtered to that sub-task, so the user can see what's
happening inside the piece they're looking at, not only in a separate shared feed. The
shared ticket-level Live Activity feed stays; this adds the per-sub-task view on the card.

**R-56. Verification has three outcomes; "couldn't test" is not "the fix is wrong."**
Extends R-32. Running a repo's tests can fail for two very different reasons, and the tool
must not conflate them. The **deterministic** test-runner (never the LLM) classifies the
result by WHY it failed:
- **PASS** — the required tests actually ran and passed.
- **FAIL** — the environment was fine, tests executed, and the code failed (e.g. an
  assertion). → back to the Executor/Critic repair loop.
- **UNVERIFIABLE** — the tests could not properly run (missing env vars, DB/service not
  reachable, auth failed, collection/import error). This is NOT a code failure — but it is
  also NOT success. It **blocks the success path**: no PR is opened on unverified work; the
  ticket surfaces the real reason and asks for what's needed.
Classification is by exit code + error signature, deterministically; the LLM never decides
the category (that would collapse the safety property). The LLM only sees structured facts
("db reachable: yes/no"), never raw secrets.

**Secrets for verification are UI-only, ephemeral, and never stored.** When a run is
UNVERIFIABLE for missing credentials, the tool asks the user through the UI, **batching all
currently-known-needed secrets into one prompt**, injects them into the spawned test
process's environment only, runs, then discards them. They never go to the DB, Jira,
comments, memory, LLM prompts, logs, traces, checkpoints, the repo, or a PR. The user
re-enters them on a future run (intentional trade: security over convenience). If the user
cannot supply a credential (they lack that access), the result stays UNVERIFIABLE — the
tool never creates access or bypasses company permissions to run tests. Wrong credentials →
UNVERIFIABLE ("auth failed"), not FAIL. The **runtime result is always the source of
truth**; any preflight prediction (R-56b) is convenience only and never overrides it. Built
in Phase 12.

**R-56b. Preflight credential prediction is best-effort convenience, never a gate.**
(Phase 12.2, built after 12.1.) Before running tests, the tool may scan obvious setup files
(`.env.example`, `docker-compose.yml`, `conftest.py`, CI config) to predict likely-needed
secrets so the first batch prompt is fuller — fewer round-trips. But preflight is never
treated as truth: it cannot skip verification, cannot block on a predicted-but-unused
secret, and never overrides the runtime PASS/FAIL/UNVERIFIABLE. Runtime failure remains the
fallback that catches anything preflight missed (ask only for the surprise).

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
- [ ] Retries consume reasoning-failure budget only; infrastructure failures stop + escalate (R-8b)
- [ ] Agents can signal "I can't" (R-10)
- [ ] State persisted after each node; resumes after restart (R-12, R-13)
- [ ] Edits are SEARCH/REPLACE, one-match-only, guarded, reversible (R-14–R-16)
- [ ] No secret in code or logs; model swappable via env (R-17, R-18)
- [ ] Never touches main; PR only; freshness-checked (R-19, R-20)
- [ ] Assertion normalizer classifies PASS and FAIL including negated forms (R-32e)
- [ ] Generated test checked against approved requirement before running; contradiction → regenerate, not code-fix (R-32f)
- [ ] Retry attempt includes failed candidate + accumulated history 1…N-1 (R-58b, R-58c)
- [ ] Duplicate candidate fingerprinting prevents budget exhaustion on repeated failures (R-58d)
- [ ] Executor payload carries scoped execution_constraints separate from repair_feedback (R-59)
- [ ] Repo intelligence is deterministic + per-repo + incremental; graph verified against source (R-50)
- [ ] Code-intelligence capabilities are tools; exactly one investigator agent, bounded + guarded (R-51)
- [ ] Phase has passing tests + a visible verification (R-22)
