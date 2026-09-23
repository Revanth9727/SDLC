# HARDENING_ROADMAP.md — From Working Demo to Production System

> **Purpose.** The core tool (Phases 1–12) works and is tested. This document is the
> separate, deliberate roadmap for turning it from an impressive working system into a
> production-grade one. It exists so hardening is a *plan*, not reactive whack-a-mole:
> the full list is known, sequenced by dependency, so nothing surprises us.
>
> **Sequencing principle (do not reorder casually):** Correctness → Security → Recovery →
> AI Quality → Telemetry → Deployment. You cannot meaningfully measure product quality
> (Stage 4) on a system that can publish the wrong artifact (Stage 1). Finish each stage
> before leaning on the next.
>
> **Rule while hardening:** the feature roadmap is FROZEN. No new capabilities until
> Stage 1 (correctness) is genuinely done and a clean, honest end-to-end proof run exists.

---

## Stage 1 — Correctness (IN PROGRESS — do first)

The system must never report success on something it didn't actually do.

1. **Tested artifact == published artifact (R-31b).** Integration tests the combined change
   of all sub-tasks, but publish must open the PR from THAT integrated result — not
   `states[0]` or a subset. A green check must attach to exactly what gets published.
   *(In progress.)*
2. **Generated-test repair path / control-flow defect (R-32b).** A failing test rewinds
   `current_step`; the guard expects `current_step + 1` and rejects the repair re-entry. The
   repair mechanism and the guard's step invariant must agree. *(In progress.)*
3. **Dependent-subtask artifact propagation.** When sub-tasks depend on each other, later
   ones must build on the earlier ones' actual changes, not a stale base.
4. **Integration / publication race.** No interleaving that lets publication run against a
   half-assembled or superseded integration result.
5. **Remote side-effect idempotency.** PR creation / Jira updates / comments must be safe
   under retries and replays — never double-open a PR or double-post.

Also in this stage (bug-class fixes already identified):
- Critic: caller count informs impact analysis, not automatic rejection (R-51) — *done*.
- Verification classifies by whether tests ran, not by keywords in output (R-56) — *done*.
- Bad generated test must not drive a production-code change (R-32b) — *in progress*.
- Defensive contracts: no unhandled `None`/malformed-result crashes (R-2/R-10).

---

## Stage 2 — Security & isolation

The tool runs untrusted third-party repositories. It needs real boundaries.

6. **Sandbox repository execution.** Today a cloned repo's tests run under the app's own OS
   identity — env is cleaned and there's timeout/cleanup, but no real execution sandbox, so
   tests could touch the filesystem, other workspaces, app-readable files, or the network.
   Target: isolated test runner with a disposable filesystem, CPU/memory limits, timeout,
   restricted network, and a minimal environment. (Matters more than any "security agent.")
7. **Repo-index symlink containment.** Indexing must not follow symlinks out of the repo.
8. **Secret exclusion before embeddings.** Ensure secrets/credential-bearing content are
   excluded before code is embedded/stored (extends M-3).
9. **Memory / repo-access scoping.** Private-repo intelligence and memory strictly
   access-scoped; a run only ever reads what it's authorized to.

---

## Stage 3 — Recovery (from "state persisted" to "system recovers itself")

Good pieces exist (LangGraph checkpoints, retries, stuck detection, webhook replay,
persistent approval state) — but there's no general worker/job recovery.

10. **Durable coding-job queue / lease.** Claims carry a lease + heartbeat.
11. **Abandoned-job recovery.** Worker crashes → lease expires → a new worker resumes from
    the checkpoint automatically (today a crashed run is only *commented* as stuck, not
    reclaimed).
12. **Stronger publish recovery.** A crash mid-publish recovers to a consistent state.
13. **Dead-letter / replay handling.** Failed jobs/events land somewhere and can be replayed.

---

## Stage 4 — AI quality (the biggest portfolio gap)

Today there is a strong *software* regression suite ("does the function work?") but not an
*AI-product* evaluation system ("how good is the product?"). This is the single most
valuable gap to close for an AI-systems-reliability portfolio.

14. **SDLC benchmark dataset.** ~100 coding tickets, each with: ticket text, repo snapshot,
    expected relevant files, expected root cause, expected behavior, known-good patch/tests.
15. **Retrieval metrics** — relevant-file recall, relevant-symbol recall, path accuracy.
16. **Diagnosis accuracy** — root-cause accuracy.
17. **Generated-test validity rate.**
18. **Critic FP/FN** — false-positive and false-negative rates.
19. **Patch success rate** — compile success, test success, end-to-end resolved-ticket rate,
    human-intervention rate, retry rate, tokens/task, cost/task, latency/task.
20. **Baseline-vs-candidate release gate** — the same evaluation-gate mentality applied to
    the SDLC product: current system vs a new prompt/model/retriever, gated on the metrics.

---

## Stage 5 — Production telemetry (also unlocks honest cost/latency claims)

Today: ticket/subtask/agent events, aggregate tokens/cost, retries, failure, PR state.
Missing: per-operation detail — which is why cost/latency numbers today are *estimates*.

21. **Per-call traces** — per LLM call: model, prompt version, input/output tokens, duration,
    cost; plus retrieval and pytest durations.
22. **Prompt / model / workflow version lineage.**
23. **Latency attribution** — where the wall-clock actually went.
24. **Complete token / cost accounting** (including embeddings, which today are outside the
    cost ledger).
25. **Throughput / concurrency metrics.**

Target artifact — a real per-ticket trace, e.g.:
```
TRACE: <TICKET>
Planner            6.3 s   $0.04
Code Intelligence  8.8 s   $0.06   (lexical 0.2s / semantic 0.4s / graph 0.1s)
Diagnosis          9.2 s   $0.07
Step Planner       2.1 s   $0.004
Human wait         43 min
Executor          18.4 s   $0.02
pytest            31.6 s
Critic             7.9 s   $0.06
Integration       49.3 s
GitHub PR          1.6 s
```
> Note: this stage is what makes the LinkedIn/portfolio cost numbers **measured** instead of
> estimated. Until it exists, present numbers as estimates from a trace.

---

## Stage 6 — Deployment

26. **CI/CD.**
27. **Versioned DB migrations.**
28. **Proper health / readiness / liveness probes.**
29. **Production secrets management.**
30. **Staging environment + load tests.**

Only after all of the above: multi-provider, multi-tenant, large-scale distributed workers,
10k users, advanced security-audit workflows.

---

## Specific improvements folded into the stages above

**Critic structured evidence (Stage 1/4).** The Critic should output an audit, not an
opinion. Instead of `{approved: false, issues:["shared function changed"]}`, require:
contract-changed Y/N, signature-changed Y/N, return-behavior-changed Y/N, affected-callers N,
verified caller incompatibilities, tests covering affected behavior, unverified risks,
source evidence, confidence. `CriticVerdict` today doesn't require most of this.

---

## Code-intelligence — known limitations / future work (accuracy honesty)

The retrieval/indexing primitives are real and implemented (exact + semantic + hybrid
search, deterministic rerank, structural chunks, Python AST, SQL extraction, provenance,
snapshots, incremental indexing, index locking, atomic activation, persistent repo
intelligence). Three honest corrections before over-relying on it:

1. **Graph precision — don't call all edges "proven."** Binding is largely name-based; two
   modules with `def check()` can be confused, and a symbol's text appearing in source
   isn't proof the specific call edge is correct. Label edges `CONFIRMED / APPROXIMATE /
   UNRESOLVED` until binding is actually resolved. (Also feeds Critic caller quality.)
2. **Branch/ref enforcement.** repo+ref+commit are stored, but the ref isn't always enforced
   at checkout/query — tighten before relying on "I analyzed release/2026."
3. **Large-repo scaling hazards.** Serial embeddings, full inventories in prompts, full rg
   output capture, repeated clones, copied snapshot data, unbudgeted embedding work.

**Claim discipline (for portfolio/LinkedIn):** say *"designed for large repositories and
already implements the key retrieval/indexing primitives"* — NOT *"proven on 50,000-file
enterprise repositories."* The gap between "designed for" and "proven on" is honest and is
itself a strong signal.
