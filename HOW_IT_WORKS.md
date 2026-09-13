# How the SDLC Agent Works

This tool turns a Jira request into a reviewed code change. It reads the ticket, finds the relevant parts of the codebase, explains what it thinks is wrong, proposes a plan, waits for a person to approve that plan, makes and tests the change, checks its own work, and opens a GitHub pull request for human review.

## The Big Picture

Think of the tool as a small software team working from one shared job folder:

**Jira ticket arrives -> understand the request -> find the relevant code -> diagnose the problem -> propose a plan -> wait for approval -> edit and test -> review the result -> test the combined work -> open a pull request**

For example, a ticket might say, "Checkout crashes when the cart is empty." The tool can find the checkout code, identify the divide-by-zero problem, propose an error-handling change and a regression test, and wait for approval. After approval, it makes the change in a separate workspace, runs the tests, reviews the result, and opens a pull request. It does not merge that pull request into the main branch.

The web interface shows tickets, repositories, plans, approvals, progress, costs, failures, prior attempts, and live activity. Jira comments also provide an audit trail of meaningful steps.

## The Agent Team

The system uses six specialist agents, plus a coordinator that moves work between them:

- **Planner:** Reads the ticket, confirms the intended outcome with you, and splits larger requests into smaller pieces with clear dependencies.
- **Code-Intelligence:** Acts like a codebase investigator. It searches by words and meaning, follows relationships between functions, and verifies its findings against source files.
- **Diagnosis:** Uses the verified code evidence to explain the likely root cause and identify the affected files.
- **Step-Planner:** Converts the diagnosis into a precise sequence of file changes and tests for you to approve.
- **Executor:** Applies approved edits, creates tests, validates test syntax and imports, and runs the local test suite.
- **Critic:** Independently checks whether the result matches the ticket, whether tests support it, and whether callers or related code may have been missed.
- **Orchestrator:** This is the coordinator, not another thinking agent. It enforces dependencies, starts independent work in parallel where possible, manages approval gates, and brings completed pieces together.

Each specialist communicates through a structured shared record called the **blackboard**. In everyday terms, it is a form with named boxes for the diagnosis, plan, evidence, test result, review verdict, budget, and current status. This prevents one agent from quietly passing untracked information to another.

## What Happens to a Ticket

### 1. Intake and repository choice

The app polls Jira for eligible tickets and also refreshes tickets it already knows about. Jira status categories, rather than fixed English status names, tell it whether work is new, active, or done. A first-run settings page lets the operator declare the more specific meaning of project statuses.

The tool looks for a GitHub repository link in the ticket summary, description, environment, and comments. Jira's rich-text description is converted into searchable text. A proposed repository is checked before it is accepted. Public repositories are tried anonymously first; a private-repository token can be entered in the web interface and is stored encrypted.

### 2. Intent confirmation and work split

Before decomposition or code changes, the Planner states its understanding of the ticket and asks for confirmation. After confirmation, it creates one or more isolated sub-tasks. A dependency such as "update the database before changing the API" causes those pieces to run in order. Independent pieces can run in the same concurrent wave.

### 3. Investigation, diagnosis, and plan

For each sub-task, Code-Intelligence gathers a small, verified evidence set. Diagnosis explains the cause from that evidence. Step-Planner then produces concrete edit and test steps.

Each sub-task has its own approval gate. A person can approve, reject, ask a question, or describe a revision in the browser or in a Jira comment. Natural replies such as "approve," "looks good," or "also handle empty values" are classified by the cheaper model. Rejection with feedback causes bounded re-planning and returns to the gate; it is not treated as a dead end.

### 4. Change, test, and review

After approval, the app checks that the relevant code has not changed since it was investigated. If it has drifted, the work is rechecked instead of blindly editing stale code.

The Executor works in a fresh, isolated checkout. Existing files are changed with targeted search-and-replace edits; new files are written as complete files. Generated Python tests are parsed before pytest runs, and their imports are checked against the real repository. Invalid tests can be repaired only within a fixed retry limit.

The Critic then compares the ticket, diagnosis, plan, changes, test result, and known code relationships. It can approve, reject and send the work back to the Executor, or escalate when the retry limit is reached. Its verdict and confidence are visible on the ticket page.

### 5. Integration and pull request

When all runnable sub-tasks have settled, the integration stage intentionally looks across them. It combines their changes on fresh checkouts, runs the full pytest suite for each affected repository, and checks whether one sub-task changed something another relies on. If combined work breaks, no pull request is opened; the ticket is marked for human attention with a specific report.

If integration passes, one shared publishing path records the result, updates Jira, writes memory, cleans the workspace, and opens a pull request. Before doing that work, it checks whether the ticket already has an open pull request. If one exists, it stops and asks whether to keep or replace it. A deliberate "redo PR" action closes the current pull request and creates a fresh one through the same path.

## Safety Rails

### A person controls the important decisions

There are two meaningful human gates: confirmation of the Planner's interpretation and approval of each sub-task's change plan. Both the browser and Jira-comment actions use the same decision path. Approvers are limited to the ticket assignee or configured Jira account IDs.

### The main branch is not edited

Work happens in isolated, shallow Git checkouts and on a working branch. The finished result is proposed as a pull request. The tool does not automatically merge or deploy it.

### A guard checks every agent stage

After the Planner, Code-Intelligence, Diagnosis, Step-Planner, freshness check, Executor, and Critic, a deterministic guard checks required output, retry limits, elapsed time, model-call usage, and estimated cost. A deterministic check means ordinary rules perform this job; another AI opinion is not used as the safety mechanism.

When the system cannot support an answer, a repository cannot be reached, tests fail past their retry cap, a budget is exhausted, or integration finds a conflict, it records the real failure reason and moves the work to `needs_human`. It also posts a Jira message and emits a live event instead of silently continuing.

### Attempts, gates, and pull requests do not pile up

There can be only one active sub-task attempt and one active approval gate for the same work. Older attempts are summarized on Jira and marked as superseded. There can also be only one open pull request per ticket; keeping or replacing an existing one requires a human choice.

### Credentials are kept out of the work

Private-repository tokens are encrypted at rest and are never returned by the API after entry. Provider credentials are removed from the environment given to repository tests. Tokens should still be treated as sensitive and limited to the permissions the tool needs.

## How It Finds Its Way Around a Large Codebase

The tool does not start by sending an entire repository to a model. It works more like a researcher using a catalog:

1. It inventories useful files and ignores Git metadata, dependencies, generated output, binaries, lock files, minified files, and oversized files.
2. It records symbols, imports, references, callers, callees, file relationships, and detectable SQL reads and writes, with the source file and line range for each fact.
3. It embeds searchable code chunks so it can find code by meaning even when names are poor. A request about "preventing duplicate charges," for example, need not contain the exact function name.
4. It combines exact-word results, meaning-based results, symbol matches, file relevance, and relationship proximity with a fixed scoring formula. No model chooses the search ranking.
5. It opens the shortlisted source lines and verifies the evidence before Diagnosis relies on it.

This repository intelligence is saved per repository, branch, and commit. The first visit performs a cold index. Later visits use Git differences to update changed files instead of rebuilding everything. A lock prevents two tickets from writing the same repository index at once, and a new snapshot becomes active only after it is complete and validated.

The Executor and Critic use the same relationship information to notice affected callers and references. Integration uses it to identify possible conflicts between sub-tasks. The relationship map is a navigation aid; the source code remains the evidence.

## What It Remembers

The system has two different kinds of memory:

- **Resolution memory:** When a sub-task succeeds, fails, or is escalated, the app stores a short, sanitized problem-and-outcome summary. On a similar future ticket, successful summaries can be recalled as advice. They do not bypass approval, freshness checks, or the Critic.
- **Repository intelligence:** The app remembers the structure of a repository at a particular branch and commit: files, symbols, relationships, hashes, and searchable embeddings. This makes later investigations faster and allows incremental updates.

Isolation is enforced at the sub-task level. Each sub-task has its own state and workspace, and its agents receive only that state. The Planner may coordinate the split, and integration is the deliberate point where completed pieces are viewed together. Recalled resolutions are bounded summaries, not another ticket's raw conversation or code context. Private-repository intelligence is access-scoped, and repository records do not store credentials.

The LLM client also has exact and meaning-based response caches. Ticket-private prompts are not cached unless explicitly allowed, reducing the chance that private ticket context is reused elsewhere.

## Cost and Time

There is no honest fixed price or completion time. Repository size, cold indexing, number of sub-tasks, test duration, model retries, and time waiting for approval all matter.

A straightforward, single-sub-task change with a two-step plan commonly needs roughly **8 to 10 chat-model calls**, plus embedding calls used for repository search and memory. A larger ticket repeats much of that work for each sub-task. A rejected Critic verdict, invalid generated test, or requested plan revision adds bounded retry calls. Exact or semantic cache hits can reduce calls.

The default model split is deliberate:

- The strong model, currently configured as `gpt-4o`, handles decomposition, investigation, diagnosis, and critical review.
- The cheaper model, currently configured as `gpt-4o-mini`, handles simpler structured work such as execution steps and comment classification.
- `text-embedding-3-small` is used for similarity search.

For a small repository with quick tests, active processing is usually measured in minutes, not seconds. The first ticket for a repository is slower because all searchable code chunks are indexed. Independent sub-tasks can overlap, but tests, Git operations, synchronous database work, and some synchronous network calls limit the speedup. Time waiting for a person at a gate is separate and can dominate the total.

By default, a ticket is limited to 40 model calls, 100,000 tokens, an estimated $2.00 in model cost, and two hours of processing time. These values are configurable. The web interface shows calls, tokens, estimated cost, cache statistics, and budget state. Cost is an estimate calculated by the app, not the provider's final invoice.

## What It Does Not Do Yet

The current build is substantial, but it is not a replacement for engineering judgment:

- **It does not merge or deploy.** It opens a pull request for a person to review. Automated merge and deployment are not built.
- **Testing is Python-focused.** The local test runner always invokes pytest. Repository indexing recognizes build and dependency files, but first-class test execution for JavaScript, Java, Go, and other ecosystems is planned, not yet built.
- **Code understanding has blind spots.** The graph is built from deterministic parsing and lexical evidence. Reflection, runtime dependency injection, generated code, metaprogramming, and indirect database behavior can be missed. Deeper language-server or tree-sitter resolution and AI-inferred graph edges are planned, not yet built.
- **Very large repositories remain expensive to index.** Vendor and generated folders, binaries, lock files, minified files, oversized files, and ignored paths are excluded. Cold indexing still embeds every accepted searchable chunk, so very large monorepos need further scaling work.
- **Parallelism is partial.** Independent sub-tasks use concurrent scheduling, but the database layer and some network operations are synchronous. Fully asynchronous processing is planned, not yet built.
- **Ticket throughput is still conservative.** Work inside one ticket can branch into concurrent independent sub-tasks, but high-volume multi-ticket scheduling is not yet a mature distributed job system.
- **Authentication is for one operator.** The web UI has basic username/password protection. Full multi-user accounts, roles, and organization-level permissions are planned, not yet built.
- **Email alerts are not built.** Escalations appear in Jira and the web interface.
- **The system cannot guarantee a correct fix.** It verifies syntax, imports, tests, code relationships, freshness, and integration as far as the available repository and tests allow. Missing or weak tests reduce confidence and are surfaced to the human.
- **External test environments may still need people.** Tests that require unavailable databases, services, hardware, secrets, or complex setup can fail or time out. The tool reports the reason and stops for assistance.

The practical promise is narrower and safer: it gathers evidence, proposes traceable work, waits for permission, checks what it can prove, and makes uncertainty visible before asking a person to review the pull request.
