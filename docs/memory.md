# memory.md — The Memory Layer (Deep Dive)

> **Purpose.** The memory layer is the most novel and most misunderstood part of the
> system, so it gets its own document. It is what lets a new sub-task benefit from how
> *similar past sub-tasks were resolved* — the "main model that references all tickets"
> you asked for — **without** breaking the sub-task isolation that prevents
> hallucination. This document explains what it is, why it's designed the way it is,
> the exact data and flow, and the rules it must obey. It is built in its own phase
> (see `codex_prompts.md`); the core system runs fine without it first.

---

## 1. The idea in one paragraph

Every time the system resolves a sub-task, it stores a compact record of *what the
problem was and how it was solved*, along with a vector embedding of the problem. When
a new sub-task arrives, the system embeds its description and does a similarity search
over those stored records. It pulls the few most similar past resolutions and injects
**only their outcomes** into the new sub-task's context as hints ("a similar problem
before was solved this way"). It does **not** dump past sub-tasks' full context in.
That single distinction — inject *resolutions*, not *raw context* — is what keeps
isolation intact while still giving reuse.

---

## 2. Why it's designed this way (the two traps it avoids)

**Trap 1 — the "give the model everything" trap.** The naive version of memory is
"stuff all past tickets into the prompt." This blows the context window, costs a
fortune, and *reintroduces the exact cross-contamination we isolated sub-tasks to
prevent*. A sub-task about a login bug should not have a payments-refactor's full
history bleeding into its reasoning.

**Trap 2 — the "no memory" trap.** Without memory, the system re-solves the same
class of problem from scratch every time and never gets smarter. The value of an
SDLC system compounds only if it learns from its own resolutions.

The design threads between them: **controlled lookup.** Memory is a *retrieval
service* that returns a small, curated set of past *resolutions* — structured summaries,
not transcripts — chosen by semantic similarity. The new sub-task sees "here are 3
similar problems and how they were fixed," which is genuinely useful, cheap, and
isolation-safe.

---

## 3. What is (and isn't) an agent here

The memory layer is **almost entirely deterministic code**:
- Embedding a description → an API/library call.
- Similarity search → a pgvector SQL query.
- Injecting results into `memory_refs` → plain code.

The **one** LLM call in the whole layer is at **write-back time**: after a sub-task is
resolved, a single call summarises "problem + solution" into a clean, storable
resolution record. That's it. Retrieval never calls an LLM. (See `ai_rules.md` R-5.)

---

## 4. The data model

One table, keyed at the **sub-task** level (not ticket level — matching granularity is
what makes "similar work" matches meaningful).

```
subtask_memory
------------------------------------------------------------
id                  UUID / serial, PK
subtask_id          FK → the resolved sub-task
ticket_id           FK → its parent ticket (for traceability)
subtask_type        bug | feature | ci | design
problem_summary     TEXT   -- what the sub-task was, in a few sentences
resolution_summary  TEXT   -- how it was solved (root cause + fix approach)
files_touched       TEXT[] -- which files the fix changed
outcome             TEXT   -- e.g. "merged", "tests passed", "reverted"
embedding           VECTOR(1536)  -- embedding of problem_summary
created_at          TIMESTAMPTZ
------------------------------------------------------------
```

Indexes:
- A pgvector index on `embedding` (e.g. HNSW or IVFFlat) for fast similarity search.
- A btree index on `subtask_type` so you can optionally scope searches by type.

> **Embedding dimension:** 1536 matches OpenAI `text-embedding-3-small`. If you swap
> the embedding model, the column dimension and index must match — this is a
> config-level decision, kept in one place (see R-18 spirit: swappable behind config).

---

## 5. The two operations

### 5.1 READ — inject similar resolutions when a sub-task starts

Runs right after the Planner creates a sub-task, before Diagnosis. Deterministic:

```
def fetch_similar(subtask) -> list[MemoryRef]:
    1. text   = subtask.description
    2. vector = embed(text)                      # deterministic API call
    3. rows   = SQL:
                 SELECT problem_summary, resolution_summary, files_touched, outcome,
                        1 - (embedding <=> :vector) AS similarity
                 FROM subtask_memory
                 WHERE (:type IS NULL OR subtask_type = :type)   -- optional scoping
                 ORDER BY embedding <=> :vector                  -- cosine distance
                 LIMIT :k                                        -- k = 3 to 5
    4. keep only rows with similarity >= :threshold  (e.g. 0.75)
    5. return them as MemoryRef objects
```

The Orchestrator writes these into the sub-task's `memory_refs`. Diagnosis and
Step-Planner may reference them as *hints* — clearly labelled as "prior similar
resolutions," never as authoritative fact. If nothing clears the threshold,
`memory_refs` is empty and the sub-task proceeds normally (no forced irrelevant
matches).

### 5.2 WRITE-BACK — store the resolution when a sub-task finishes

Runs after a sub-task's PR is opened (or it's otherwise resolved):

```
def write_back(subtask, result):
    1. summary = LLM_summarise(subtask, result)   # the ONE LLM call in this layer
       -> {problem_summary, resolution_summary}
    2. vector  = embed(summary.problem_summary)    # deterministic
    3. INSERT INTO subtask_memory (...) VALUES (...)  # store record + embedding
```

The summary is deliberately compact and generalised — it should describe the *class*
of problem and fix, so future semantically-similar problems match, not just identical
ones.

---

## 6. How isolation is preserved (the crucial property)

- The new sub-task **never receives another sub-task's `SubTaskState`**. It receives
  only `MemoryRef` objects: short, structured, past *resolutions*.
- Memory is a *lookup*, not a shared context. Sub-task A and sub-task B still cannot
  see each other's live state. What B can see is the *stored, summarised outcome* of A
  **after A finished** — the way a new engineer reads a closed ticket's resolution, not
  the previous engineer's private notes.
- Because it's retrieval of finished summaries, there is no live cross-talk, no context
  bleed, and no path for one sub-task's in-flight reasoning to contaminate another.

This is the whole reason memory and isolation coexist: **isolation governs live
context; memory serves finished resolutions through a controlled query.**

---

## 7. Failure & quality rules (memory-specific)

- **M-1. Memory is advisory, never authoritative.** A retrieved resolution is a hint.
  Agents must still diagnose the actual repo. Never let a memory hit short-circuit real
  diagnosis. (Prevents "it worked last time" false confidence.)
- **M-2. Threshold, not top-k-always.** Return matches only above the similarity
  threshold. An empty result is correct and expected for novel problems — better than
  injecting a weak, misleading match.
- **M-3. Never store secrets or full file contents.** Store summaries and file *paths*,
  not code bodies or anything from `.env`. (R-17 applies here too.)
- **M-4. Write-back failure must not fail the sub-task.** If summarisation or the
  insert fails, log it and continue — the PR is already the real outcome. Memory is an
  enhancement, not on the critical path.
- **M-5. Embedding model is config-pinned.** The model and the vector dimension are set
  in one place; changing them is a deliberate migration (re-embed existing rows), never
  an accidental mismatch.

---

## 8. Where it plugs into the flow

```
PLANNER creates sub-task
      │
      ▼
MEMORY.fetch_similar()  ──►  writes memory_refs into SubTaskState   [READ]
      │
      ▼
DIAGNOSIS (may reference memory_refs as hints)
      │
      ▼
… STEP-PLANNER → GATE → EXECUTOR → CRITIC → PR …
      │
      ▼
MEMORY.write_back()     ──►  stores resolution + embedding           [WRITE]
```

Read is a pre-diagnosis enrichment; write-back is a post-PR recording. Both are outside
the isolated reasoning core — they touch the shared memory table, never another
sub-task's live state.

---

## 8a. Solution reuse — memory as a gate, not just a hint

Beyond injecting hints, memory can **short-circuit the expensive agents** when a past
resolved ticket already solved essentially this problem. This is a **tiered reuse gate**
at the front of the per-ticket flow (after repo resolution, before Diagnosis):

1. Memory searches the top-K most similar **resolved** tickets.
2. **Strong match** (similarity ≥ 0.9, the hardcoded bar): skip **Diagnosis and
   Step-Planner** entirely. Take the past ticket's resolution as a **proposed fix**,
   run a freshness/applicability check (does it still apply to the current code?), then
   go straight to the **human gate** — "we've solved this before (from SCRUM-X), here's
   the known fix, approve?" — then Executor applies, Critic verifies, PR. You save the
   two expensive reasoning agents but KEEP the gate + verification.
3. **Weak / no match**: fall through to the full pipeline as normal.

**Why tiered, not aggressive.** A similar past ticket is NOT a proven-correct fix for
this one: the match may be similar-not-identical, and the codebase may have changed. So
reuse never blind-applies — it proposes, checks applicability, and still requires human
approval + Critic verification. The savings come from skipping the heavy *reasoning*
(Diagnosis, Step-Planner), not from skipping *safety* (gate, verification).

This gate only helps once resolved tickets exist to match against, so it is built AFTER
the full pipeline works (Phase 7.5), not at the same time as the base memory hints.

## 9. Build note (when this happens)

The memory layer is built in its **own phase**, *after* the core loop (spine →
isolation → Critic → surgical edits) works end-to-end. Reasons:
- It's an *enhancement*; the system produces real PRs without it.
- Building it early means debugging vector search on top of an unproven pipeline.
- By its phase you'll have real resolved sub-tasks to populate it with — so you can
  actually *see* a later sub-task get a relevant hint from an earlier one, which is the
  visible "it works" moment for this feature.

Its verification (in `codex_prompts.md`): resolve two similar sub-tasks; confirm the
second one's `memory_refs` contains the first one's resolution, shown live in the UI.
