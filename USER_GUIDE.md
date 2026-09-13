# Agentic SDLC User Guide

This is the complete guide, starting with an empty computer and ending with a reviewed GitHub pull request. Follow it in order the first time. Later, use **Start Again Tomorrow** and the day-to-day sections.

You do not need to understand the source code. A few commands must be typed exactly as shown. Lines beginning with `#` are explanations and do not need to be typed.

# Part One: Set Up the App

## 1. Understand What You Are Setting Up

Agentic SDLC connects four things:

1. **Jira** holds the request.
2. **GitHub** holds the code and the final pull request.
3. **OpenAI** helps the agents understand, plan, edit, and review the work.
4. **Agentic SDLC** coordinates the process and stores its local records in PostgreSQL.

The normal path is:

**Jira ticket -> claim -> confirm repository -> confirm intent -> approve plan -> edit and test -> review -> integration -> GitHub pull request**

The app never merges the pull request. A person reviews and merges it in GitHub.

## 2. Create the Accounts and Access Keys

Allow 60 to 90 minutes for first-time setup. Keep every key private. Never paste a real key into Jira, a screenshot, a chat, or a Git commit.

### OpenAI

1. Go to [OpenAI Platform](https://platform.openai.com/), create an account, and enable API billing.

   A ChatGPT subscription does not include OpenAI API usage.

   **You should see:** Your OpenAI Platform dashboard.

2. Open [OpenAI API keys](https://platform.openai.com/api-keys) and create a secret key.

   **You should see:** A key that normally starts with `sk-`. Store it somewhere safe because the complete value may be shown only once.

### GitHub

1. Create or sign in to a [GitHub account](https://github.com/).

   **You should see:** The repository you want the app to work on.

2. Open [GitHub fine-grained personal access tokens](https://github.com/settings/tokens?type=beta) and create a token.

3. Give the token access to the repository. Set **Contents** to **Read and write** and **Pull requests** to **Read and write**.

   **You should see:** A token that normally starts with `github_pat_`. Save it securely when GitHub displays it.

The app can read a public repository anonymously, but it still needs a valid GitHub token to push a branch and open a pull request.

### Jira Cloud

1. Create or sign in to a [Jira Cloud account](https://www.atlassian.com/software/jira) and open the project you want the app to watch.

   **You should see:** A project key such as `SCRUM` in issue names like `SCRUM-7`.

2. Open [Atlassian API tokens](https://id.atlassian.com/manage-profile/security/api-tokens) and create a token.

   **You should see:** A token value. Save it securely.

3. Write down these four values:

   - Jira site address, such as `https://your-company.atlassian.net`
   - Jira account email
   - Jira API token
   - Jira project key, such as `SCRUM`

## 3. Install the Required Tools

You need Docker Desktop, Git, and Python 3.11. Python is useful for creating the encryption key and is required if you choose the local-development launch method.

### Mac

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/). Choose the Apple Silicon or Intel download that matches your Mac.

2. Open Docker Desktop and wait until it says Docker is running.

3. Open **Terminal** and run:

   ```bash
   docker --version
   docker run hello-world
   ```

   **You should see:** A Docker version and the words `Hello from Docker!`.

4. Install [Homebrew](https://brew.sh/) if it is not already installed. Then run:

   ```bash
   brew install python@3.11 git
   python3.11 --version
   git --version
   ```

   **You should see:** `Python 3.11.x` and a Git version number.

### Windows

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/). Accept WSL 2 when asked and restart Windows if the installer requests it.

2. Open Docker Desktop and wait until the engine is running.

3. Open **PowerShell** and run:

   ```powershell
   docker --version
   docker run hello-world
   ```

   **You should see:** A Docker version and the words `Hello from Docker!`.

4. Install 64-bit [Python 3.11](https://www.python.org/downloads/release/python-3119/). During installation, check **Add python.exe to PATH**.

5. Install [Git for Windows](https://git-scm.com/download/win) using its default choices.

6. Return to PowerShell and run:

   ```powershell
   py -3.11 --version
   git --version
   ```

   **You should see:** `Python 3.11.x` and a Git version number.

## 4. Clone the Project

1. Choose a place for the project. Open Terminal on Mac or PowerShell on Windows.

2. Run these commands exactly:

   ```bash
   git clone https://github.com/Revanth9727/SDLC.git
   cd SDLC
   ```

   **You should see:** Git download the repository. Your command line should now be inside the `SDLC` folder.

3. Check the files.

   **Mac:**

   ```bash
   ls
   ```

   **Windows PowerShell:**

   ```powershell
   Get-ChildItem
   ```

   **You should see:** `app`, `docs`, `Dockerfile`, `docker-compose.yml`, `requirements.txt`, and `README.md`.

## 5. Create and Fill In `.env`

The `.env` file is the app's private settings file. Git is configured to ignore it.

1. Create `.env` from the supplied example.

   **Mac:**

   ```bash
   cp .env.example .env
   ```

   **Windows PowerShell:**

   ```powershell
   Copy-Item .env.example .env
   ```

   **You should see:** No output. That is normal.

2. Open `.env` in a plain-text editor.

   If Visual Studio Code is installed, run:

   ```bash
   code .env
   ```

   Otherwise, open the `SDLC` folder in your normal editor and select `.env`.

3. Replace the required placeholders:

   ```text
   OPENAI_API_KEY=your-real-openai-key
   GITHUB_TOKEN=your-real-github-token
   GITHUB_OWNER=your-github-user-or-organization
   GITHUB_REPO=a-safe-sandbox-repository-name
   JIRA_BASE_URL=https://your-company.atlassian.net
   JIRA_EMAIL=your-jira-email@example.com
   JIRA_API_TOKEN=your-real-jira-token
   JIRA_PROJECT_KEY=SCRUM
   ```

   `GITHUB_REPO` is a fallback used by older tests. Real ticket work uses the repository that you confirm on that ticket.

   **You should see:** No `REPLACE_ME`, `your-site`, or `your-github-username` placeholder left in those required lines.

4. Keep this database setting unchanged for the normal local setup:

   ```text
   DATABASE_URL=postgresql://agentic:localdevpassword@localhost:5432/agentic_sdlc
   ```

   Docker Compose automatically changes the hostname to `db` inside the app container.

5. Change the browser login password:

   ```text
   UI_BASIC_AUTH_USERNAME=operator
   UI_BASIC_AUTH_PASSWORD=choose-a-long-private-password
   ```

   Do not leave `change-this-before-deploying` as the password.

6. Generate a unique encryption key using the app's Docker image.

   Save `.env`, make sure Docker Desktop is running, and run:

   ```bash
   docker compose build app
   docker compose run --rm --no-deps app python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   The first command can take several minutes because it installs the app inside a Docker image. The second command prints the key and exits.

   Copy the printed value into `.env`:

   ```text
   APP_ENCRYPTION_KEY=the-new-value-you-generated
   ```

   **You should see:** A long key ending in `=`. Do not share it. The app uses it to encrypt private-repository tokens.

7. For Jira comment approvals, decide who may direct the app.

   The Jira ticket assignee is automatically allowed. Additional users can be listed by Jira account ID:

   ```text
   JIRA_APPROVAL_ACCOUNT_IDS=["712020:example-account-id"]
   ```

   Keep `[]` if the assignee alone should approve. An unassigned ticket with an empty list has no authorized approver.

8. Save `.env`, then make sure Git will not upload it:

   ```bash
   git status --short
   ```

   **You should see:** `.env` must not appear. Stop and fix `.gitignore` before continuing if it does.

The remaining values in `.env.example` already have usable defaults for models, retries, budgets, polling, indexing, memory, logging, and timeouts. Change them later only when you understand the effect.

## 6. Start Everything with Docker

This is the recommended first launch. Docker starts both PostgreSQL and the app.

1. Confirm Docker Desktop is running.

2. From inside the `SDLC` folder, run:

   ```bash
   docker compose up --build -d
   ```

   The first build downloads software and installs packages, so it can take several minutes.

   **You should see:** Docker create `agentic_sdlc_db` and `agentic_sdlc_app`, then return to the command prompt.

3. Check both services:

   ```bash
   docker compose ps
   ```

   **You should see:** Both `agentic_sdlc_db` and `agentic_sdlc_app`. The database should become `healthy`; the app may say `starting` briefly before it becomes healthy.

4. If the app is still starting, watch its log:

   ```bash
   docker compose logs -f app
   ```

   **You should see:** Uvicorn listening on port `8000`. Press `Ctrl+C` to stop watching the log; the containers continue running.

5. Open [http://localhost:8000](http://localhost:8000) in your browser.

6. Enter the `UI_BASIC_AUTH_USERNAME` and `UI_BASIC_AUTH_PASSWORD` from `.env` when the browser asks.

   **You should see:** The Jira status setup page on the first visit.

7. Check app health by opening [http://localhost:8000/health](http://localhost:8000/health).

   **You should see:** `status` as `ok`, `database` as `ok`, and `poller` as `running`.

### If Docker startup fails

Run:

```bash
docker compose logs app
docker compose logs db
```

Read the last error. The most common causes are an unchanged `.env` placeholder, an invalid Jira/OpenAI/GitHub key, Docker Desktop not running, or ports `5432`/`8000` already being used.

## 7. Alternative: Run Python Locally

Skip this section if the Docker app is working. Use this path when developing or when you want Uvicorn to reload after source-code changes.

1. Start only PostgreSQL:

   ```bash
   docker compose up -d db
   docker compose ps
   ```

   **You should see:** `agentic_sdlc_db` as healthy.

2. Create and activate a Python environment.

   **Mac:**

   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate
   ```

   **Windows PowerShell:**

   ```powershell
   py -3.11 -m venv .venv
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   .venv\Scripts\Activate.ps1
   ```

   **You should see:** `(.venv)` at the beginning of the command line.

3. Install the project's pinned packages:

   ```bash
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   ```

   **You should see:** Installation finish without an error.

4. Create or update the database tables:

   ```bash
   python -m app.db.init_db
   ```

   **You should see:** The command return to the prompt without an error.

5. Start the app:

   ```bash
   uvicorn app.main:app --reload --port 8000
   ```

   **You should see:** `Uvicorn running on http://127.0.0.1:8000`. Leave this terminal open.

6. Open [http://localhost:8000](http://localhost:8000).

Do not run the Docker app and local Uvicorn at the same time on port `8000`.

## 8. Optional: Enable Jira Comments and Fast GitHub Updates

Polling works without webhooks. However, Jira cannot send a comment event to `localhost`. To approve, reject, revise, and ask questions through Jira comments, you need a public HTTPS address that forwards to this app.

The project does not include or automatically configure a tunnel. Use a tunnel service approved by your organization, such as ngrok, or deploy the app at a public HTTPS address. Automatic tunnel setup is **planned, not built**.

### Jira webhook

1. Create a long random value and place it in `.env`:

   ```text
   JIRA_WEBHOOK_SECRET=your-long-random-secret
   ```

2. Restart the app after changing `.env`.

3. In Jira's webhook settings, use this exact shape for the callback URL:

   ```text
   https://YOUR-PUBLIC-HOST/webhooks/jira?secret=your-long-random-secret
   ```

4. Enable issue-created, issue-updated, and comment-created events for the configured project.

   **You should see:** Jira webhook deliveries accepted by the app. New comments can now reach the comment handler.

Jira Cloud UI webhooks do not sign their body. This app authenticates Jira using the secret in the URL. Do not publish that URL.

### GitHub webhook

1. Create another secret and place it in `.env`:

   ```text
   GITHUB_WEBHOOK_SECRET=a-different-long-random-secret
   ```

2. Restart the app.

3. In the GitHub repository, open **Settings -> Webhooks -> Add webhook**.

4. Use:

   - Payload URL: `https://YOUR-PUBLIC-HOST/webhooks/github`
   - Content type: `application/json`
   - Secret: the exact `GITHUB_WEBHOOK_SECRET`
   - Events: pull request and push/delete events used by your repository workflow

   **You should see:** GitHub report a successful delivery. The app verifies GitHub's signature before accepting the event.

After any `.env` change, restart the app. Docker users run `docker compose up -d --force-recreate app`; local Uvicorn users press `Ctrl+C` and run the Uvicorn command again.

# Part Two: Run Tickets Day to Day

## 9. Open the App

1. Open the app's home page.

   **You should see:** The **Tickets** page. On a first visit, the app may send you to **Jira workflow statuses** instead. Complete Section 2, then return to Tickets.

2. Look at the navigation at the top.

   - **Tickets** returns to the main queue.
   - **Needs human** opens the escalation queue.
   - **Jira statuses** opens the Jira workflow setup page.

   **You should see:** The same three links on the ticket list, ticket detail, status setup, and escalation pages.

3. Read the **Ticket queue** table.

   Each row shows the Jira key, ticket title, current app status, whether the ticket is claimed, and its confirmed repository. Select the title or **Open** to see the full ticket.

   Common app statuses include:

   - `new`: recorded locally but not yet running.
   - `processing`: agents are working.
   - `awaiting_approval`: a person must make a decision.
   - `in_review`: a pull request is ready.
   - `needs_human`: the app stopped and needs help.
   - `mixed`: some sub-tasks succeeded and others need help.
   - `done`: the tracked work is complete.

   **You should see:** A colored status label, a **claimed** label when Jira intake has claimed the ticket, and either **Resolve repos** or the repository name with **Change repo**.

4. Check **LLM cache** in the page heading.

   This shows how often the app reused a previous model response instead of making another model call. A hit can save time and cost. It is not the ticket's spending total.

   **You should see:** A hit-rate percentage and a count such as “3 hits / 8 misses.” It refreshes automatically.

5. Open a ticket to check its budget.

   The ticket page shows the total model calls, tokens, and estimated cost near the top. The **Current run** area also shows the active sub-task's usage.

   **You should see:** A line like “Ticket total: 7 calls · 12,450 tokens · $0.1245 estimated.” This is an app estimate, not the final OpenAI invoice.

6. Notice the two activity areas.

   The home page's **Jira poller** area shows newly claimed Jira tickets. A ticket's **Live activity** area shows the detailed agent and tool events for that ticket.

   **You should see:** **Connected** on a ticket's live feed. New events appear without a page reload.

The **Submit a ticket** form creates a local ticket using a title and description. It is useful for manual intake, but a local-only ticket has no Jira issue to comment on or update.

## 10. Set Up Jira Statuses Once

1. Select **Jira statuses** in the top navigation.

   **You should see:** **Jira workflow statuses**, the configured Jira project key, and a table fetched from that Jira project.

2. Review each row in **Status mapping**.

   The **Status** column shows the real Jira status, its Jira category, and its ID. The **Meaning** column tells the app how that status is used. Available meanings are:

   - `ready-to-pick-up`
   - `work-started`
   - `awaiting-approval`
   - `in-review`
   - `blocked/needs-human`
   - `done`

   **You should see:** A suggested meaning for every fetched status. Jira's broad category remains a fallback when a meaning is left out of the saved map.

3. Confirm uncertain suggestions.

   Some Jira statuses have an obvious category. Others, especially custom active statuses, can mean different things. Those rows show **Confirm suggestion**.

   Check the box after reviewing the selected meaning. If you leave a required confirmation unchecked, the browser blocks Save and points to that checkbox.

   **You should see:** The checkbox only on low-confidence suggestions. It is a review step; the checkbox itself is not stored after Save.

4. Use **Add status** or **Remove** only when needed.

   Rows with the same meaning are tried in the order shown. **Reload from Jira** discards the unsaved display and fetches the project statuses again.

   **You should see:** Added or removed rows immediately in the table. No change reaches the saved map until you select **Save status map**.

5. Select **Save status map**.

   The app verifies that the status IDs and names still belong to the Jira project before saving.

   **You should see:** The browser returns to **Tickets**. If Jira changed while the page was open, the page asks you to reload instead of saving stale data.

## 11. Write a Good Jira Ticket

1. Use a short summary that names one clear result.

   Good: “Return a clear error when checkout receives an empty cart.”

   Weak: “Checkout is broken.”

   **You should see:** The summary becomes the ticket title in the app.

2. In the description, include four things:

   - What happens now.
   - How to reproduce it, including important inputs.
   - What should happen instead.
   - Any boundary that must not change.

   **You should see:** The current Jira description on the ticket page after the next poll refresh.

3. Include a complete GitHub link such as `https://github.com/acme/orders-service`.

   The link may be in the summary, description, environment field, or a Jira comment. The app refreshes those Jira fields and scans them for repository links.

   **You should see:** **Resolve repos** offer `acme/orders-service` as a candidate. The app still asks you to confirm it.

4. Separate distinct requests clearly.

   A ticket may contain several requests. Use a numbered list and describe the expected result for each one. The Planner can split them into isolated sub-tasks and preserve dependencies.

   A ticket is too ambiguous when it asks for “better,” “faster,” or “cleaner” without a measurable outcome. A very large redesign with no boundaries should be discussed and divided before starting.

### Example: focused bug

**Summary:** Empty cart crashes checkout

**Description:**

> Repository: https://github.com/acme/orders-service  
> Calling checkout with an empty cart divides by zero while calculating the average item price. Return a clear empty-cart error instead. Checkout behavior for non-empty carts must remain unchanged. Add a regression test for the empty cart.

### Example: several clear requests

**Summary:** Improve order totals and order API behavior

**Description:**

> Repository: https://github.com/acme/orders-service  
> 1. Apply discounts before currency conversion in calculate_total.  
> 2. Add limit and offset pagination to GET /orders, preserving the current defaults.  
> 3. Make GET /healthz verify the database connection and report an unavailable state when it fails.  
> Each request needs a test. These requests do not depend on one another.

## 12. Get a Jira Ticket Picked Up

1. Move the Jira ticket into a status mapped as `ready-to-pick-up`.

   The app uses the Jira status category `new` as its broad ready signal and the saved project mapping to understand your workflow.

   **You should see:** The issue in the ready column on your Jira board.

2. Wait for automatic polling, or select **Run poll now** on the Tickets page.

   Automatic polling runs at the configured interval. The default is every 30 minutes. **Run poll now** performs the same poll immediately.

   **You should see:** `running...`, followed by either `claimed 1 ticket(s)` or `no ready-category tickets found`.

3. Confirm the claim.

   Claiming records the Jira issue locally, creates a fresh pending attempt, and asks Jira to move the issue to the configured work-started status.

   **You should see:** The ticket in **Ticket queue**, a **claimed** label, and a claim event in the Jira poller feed. Jira normally moves to your In Progress or work-started status.

Claiming does **not** automatically run diagnosis. Open the ticket, confirm its repository, then select **Run diagnosis & plan**.

## 13. Confirm the Repository

1. Select **Resolve repos** on the ticket row or ticket page.

   If a repository is already confirmed, select **Change repo** to run resolution again.

   **You should see:** A **Repository** or **Repo resolution** panel. It either lists candidates with checkboxes or asks you to paste a GitHub repository URL.

2. Review the candidate carefully.

   Keep the correct repositories checked. If no candidate was found, paste a URL in the form `https://github.com/owner/repo`.

   **You should see:** **Confirm repos** for detected candidates, or **Confirm** beside the pasted URL.

3. Confirm a public repository without entering a token.

   The app tries public access first.

   **You should see:** `Saved: owner/repo`, followed by a refreshed page showing that repository.

4. If the repository is private, enter its token in **Private token if needed**.

   This is a password-style masked field. Select **Confirm repos** or **Confirm** after entering it. On the ticket page, **Private access** also offers **Add token if needed** or **Replace saved token**, followed by **Save token**.

   **You should see:** `Encrypted token saved · reused automatically` after a successful save. The token itself is never displayed again.

5. If validation fails, correct the repository or token.

   The app does not accept a repository it cannot reach. It records `needs_human` and comments on the Jira issue when appropriate.

   **You should see:** A specific message such as the repository not being found, not being accessible, or requiring a private token.

## 14. Use the Two Approval Gates

Start the flow by opening the ticket and selecting **Run diagnosis & plan**.

**You should see:** `Running diagnosis and planning...`, then the first approval gate. The app posts the same meaningful gate to Jira when this is a Jira ticket.

### Gate 1: confirm the intent

1. Read **Decomposition**.

   The Planner shows how it understood the ticket, the proposed sub-tasks, repositories, and its reasoning.

   **You should see:** **Awaiting confirmation**, a numbered split, a **Decision note**, **Confirm**, and **Reject**.

2. In the browser, select **Confirm** when the reading is correct.

   **You should see:** `Confirmed. Diagnosing...` and the sub-tasks begin their investigation.

3. If the reading is wrong, explain why in **Decision note**, then select **Reject**.

   Intent rejection stops the current attempt for human attention. It does not silently rewrite the ticket.

   **You should see:** The rejection reason recorded and the run move to `needs_human`.

4. In Jira, reply to the app's intent comment with a natural decision.

   Examples:

   - `approve`
   - `yes`
   - `looks good`
   - `go ahead`
   - `reject - these should be two separate tickets`

   **You should see:** A Jira acknowledgement and the same flow transition as the browser button. No gate ID is required when there is one current gate.

### Gate 2: approve each sub-task plan

1. Read each card under **Sub-task workflow**.

   Each card shows its description, repository, dependencies, investigation result, diagnosis, plan, status, and later its Critic verdict and pull-request link.

   **You should see:** **Approve** and **Reject** only on a sub-task currently waiting for approval.

2. Select **Approve** for a plan you accept.

   **You should see:** The sub-task resume, run freshness checks, edit files, and run tests.

3. To change a plan, enter specific feedback and select **Reject**.

   Example: `Also handle None values and add a test for them.`

   **You should see:** `Feedback received. Generating a revised plan.` A new plan returns to the approval gate. Re-planning has a retry limit.

4. If you select **Reject** with no note, the app asks what should change and does not discard the gate.

   **You should see:** `What should change? Add feedback and reject again.`

5. In Jira, use natural comments on the current plan:

   - Approve: `approve`, `yes`, `lgtm`, `go ahead`, `ship it`, or `looks good`.
   - Reject: `reject - use the existing validation helper instead`.
   - Revise: `also handle None values` or `change step 2 to use try/except`.
   - Ask: `Which files will this change?` or `Why is a new test file needed?`

   **You should see:** Approval resumes the same path as the browser. A rejection or revision with feedback generates a new plan. A question receives a Jira answer while the gate remains open.

6. Approve concurrent sub-tasks individually.

   More than one independent sub-task can wait at the same time. A bare Jira `approve` is accepted only when the ticket has one pending gate. With several pending gates, the app asks you to open the ticket and choose the intended sub-task.

   **You should see:** A separate approval area on each waiting sub-task card. Use those browser buttons to avoid ambiguity.

Only the Jira ticket's assignee or an account listed in `JIRA_APPROVAL_ACCOUNT_IDS` may approve or direct work through comments. If the ticket is unassigned and no allowlist is configured, the app says `No authorized approver configured` instead of silently accepting a decision. Jira-comment interaction also requires the Jira webhook to be configured; otherwise use the browser.

## 15. Watch the Work

1. Keep the ticket page open and read **Live activity** from oldest work toward the newest event.

   **You should see:** Events arrive without refreshing. **Connected** changes to **Reconnecting...** if the live connection briefly drops.

2. Look for repository intelligence events.

   On the first visit to a repository, the app inventories and indexes accepted files. On later commits it updates changed files. It also reports exact search, meaning-based search, symbol lookup, and relationship checks.

   **You should see:** Indexing and retrieval events, followed by a **Code intelligence** result with confidence, relevant files, commit, snapshot, and verified source evidence.

3. Read the diagnosis and plan.

   **You should see:** A root cause, affected files, reasoning, and concrete plan steps. With several sub-tasks, these appear inside each sub-task card rather than in the single-task panels.

4. Watch execution and tests after approval.

   **You should see:** Step-started, edit, test, retry, and completion events. For a single-subtask view, **Execution and pull request** lets you expand each completed step and see its pytest result and output.

5. Read the **Critic verdict**.

   **You should see:** Approved or Rejected, verifiability (`ok`, `no_tests`, or `uncovered_change`), feedback, and retry count. A rejection loops back to the Executor within the retry cap. Missing or uncovered tests use warning styling.

6. Wait for integration.

   The integration stage combines completed sub-task changes on fresh checkouts, runs the full pytest suite for each affected repository, and checks cross-sub-task relationships.

   **You should see:** Integration started, repository passed, or a clear cross-breakage event. A conflict blocks pull-request publication and moves the ticket to `needs_human`.

7. Watch publishing.

   **You should see:** A `pr_opened` event and an **Open pull request** link when publication succeeds. Jira receives a “PR ready for review” comment and moves toward your in-review status.

## 16. When a Pull Request Already Exists

The app allows only one open pull request per Jira ticket. This prevents a new attempt from quietly competing with work already under review.

1. Open a ticket that already has an open pull request.

   **You should see:** **Open pull request**, **Decision needed**, a summary such as `PR #24 "Fix empty checkout" is open. Keep it, or replace it with a fresh PR.`, plus **Open PR** and **Replace PR**.

2. Select **Open PR** to inspect the current pull request in GitHub.

   **You should see:** GitHub open in a new tab.

3. Keep the current pull request by taking no replacement action in the browser.

   In Jira, you can explicitly comment `keep`, `keep PR`, or `keep the PR`.

   **You should see:** A Jira confirmation that the existing pull request is being kept.

4. Replace it deliberately by selecting **Replace PR**.

   In Jira, comment `redo PR`, `replace PR`, `replace the PR`, or `redo the PR`.

   **You should see:** `Replacing pull request...`, then `Replacement PR created`. The app closes the old GitHub pull request and publishes the saved, tested state through the same publishing path.

The replace action is not a fresh diagnosis. It republishes the tested state associated with the open pull request. Start new work separately after the previous result is closed or merged.

## 17. Review the Pull Request

1. Select **Open pull request** on the sub-task card or in **Execution and pull request**.

   **You should see:** The real GitHub pull request with the proposed branch, files, commits, and test information.

2. Review the code and tests in GitHub as you would for a teammate.

   The app performs checks, but your review remains the final engineering decision.

   **You should see:** GitHub's normal review controls. The app does not supply an automatic Merge action.

3. Optionally select **Check CI status** in the app after a pull request exists.

   **You should see:** `CI: running`, `CI: passed`, `CI: failed`, or `CI: not configured for this repo`. When available, select the CI label to open the run.

4. Merge or close the pull request in GitHub yourself.

   The tool never merges or deploys.

   **You should see:** GitHub mark the pull request merged or closed. The app's next poll/webhook reconciliation updates its pull-request record.

5. For later work on the same ticket after a merge, start a new run from the updated repository.

   **You should see:** A fresh checkout and freshness information based on the newer commit. The repository index updates only changed files, so new work builds incrementally instead of stacking on stale code.

## 18. When the App Needs a Human

1. Look for a ticket status of `needs_human` or `mixed`.

   `mixed` means at least one sub-task succeeded and at least one stopped for help.

   **You should see:** The exact failure reason on the ticket or affected sub-task card, not only a generic “failed” message.

2. Read the Jira comment.

   Failures such as inaccessible repositories, exhausted retries, failed integration, expired gates, and blocked publishing are posted to Jira when possible. Integration failures mention the owner and explain what broke.

   **You should see:** A comment beginning with a clear blocked or integration message and containing the underlying reason.

3. Open **Needs human** from the top navigation.

   **You should see:** A queue of escalated sub-tasks with status, failure reason, calls, tokens, estimated cost, **Retry / Continue budget**, and **Reject and close**.

4. Select **Retry / Continue budget** after fixing the underlying problem.

   Examples include saving a valid private token, correcting repository access, restoring a required service, or intentionally allowing work to continue after a budget pause.

   **You should see:** The saved workflow resume from its human-resolution point rather than silently losing its state.

5. Select **Reject and close** when the attempt should not continue.

   **You should see:** The escalation close and the sub-task end without publishing a pull request.

The ticket page also shows a **Retry** button for a failed or `needs_human` attempt when no other attempt is active and a repository is confirmed. Use it to start a fresh diagnosis-and-plan run.

## 19. Everyday Habits

1. Keep ticket requests specific and keep the GitHub link current.

   **You should see:** Faster repository resolution and plans that are easier to approve.

2. Treat Jira as a two-way conversation when the webhook is configured.

   Use short approvals, specific revision requests, and direct questions. The app reads recent ticket conversation for continuity. It ignores comments that it posted itself.

   **You should see:** Meaningful replies and decisions recorded on the same Jira ticket. Casual chatter is ignored.

3. Remember that instructions outside a current gate are proposals.

   A command-like Jira comment does not immediately change code. The app creates a proposed action. Open the ticket's **Requested changes awaiting approval** panel and use **Approve proposal** or **Reject proposal**. A proposal rejection requires a note.

   **You should see:** The proposal description and decision buttons before any command is applied.

4. Check spending before approving more retries.

   Use the ticket total, current-run budget, and the Needs human queue.

   **You should see:** Calls, tokens, and estimated cost. A budget stop is visible and requires a human decision.

5. If a ticket seems stuck, check Jira and **Live activity** before restarting it.

   The poller sends one reminder for an unanswered approval and later expires it according to the configured policy. It also comments on non-terminal Jira tickets that remain unchanged longer than the stuck threshold.

   **You should see:** A history-aware Jira reminder or stuck notice, not repeated comments every poll.

6. Use **Run poll now** after changing Jira status, ticket content, or pull-request state when you do not want to wait for the automatic interval.

   **You should see:** Reconciliation refresh the known Jira ticket and then scan for ready work.

## 20. Common Situations

### My ticket was not picked up

1. Confirm that the issue belongs to the configured Jira project.
2. Confirm its Jira status is mapped as `ready-to-pick-up` and has Jira category `new`.
3. Select **Run poll now**.
4. If the page says `no ready-category tickets found`, open **Jira statuses** and review the map.

**You should see:** A claim event and a **claimed** label once the issue is eligible. If it is already claimed, the poller does not claim it again.

### The wrong repository was selected

1. Select **Change repo** on the ticket row or ticket page.
2. Choose the correct candidate, or paste its GitHub URL.
3. Select **Confirm repos** or **Confirm**.

**You should see:** The corrected repository on the ticket. Do this before starting or retrying the agent flow.

### The plan looks wrong

1. Do not approve it.
2. Enter a concrete change in **Decision note** and select **Reject**, or comment in Jira: `also handle duplicate order IDs and add a test`.

**You should see:** A revised plan return to the same sub-task gate. If the retry cap is reached, the ticket moves to **Needs human**.

### Tests are failing

1. Expand the completed step under **Execution and pull request** when available.
2. Read the pytest outcome and the end of its output.
3. Let bounded automatic repair run. If the app escalates, correct the repository setup or clarify the requirement before selecting **Retry / Continue budget**.

**You should see:** The actual test failure in the event or failure reason. No pull request opens when integration fails.

### The budget is exhausted

1. Open **Needs human** and review calls, tokens, estimated cost, and the failure reason.
2. Decide whether the ticket is still scoped well enough to continue.
3. Select **Retry / Continue budget** only when you accept the additional work, or **Reject and close** to stop.

**You should see:** A visible budget escalation. Jira comment classification also pauses when the model budget is exhausted, so use the browser for the decision.

### Jira comments are not getting a response

1. Check that the Jira comment webhook is configured for this app.
2. Confirm you are the ticket assignee or your Jira account ID is in the configured approval allowlist.
3. If several sub-tasks are waiting, approve the intended one on the ticket page.
4. Use the browser when the ticket budget is exhausted.

**You should see:** An authorization or clarification comment when the webhook reaches the app. Webhook setup and troubleshooting are advanced administration tasks; automatic setup from this operator screen is **planned, not built**.

### Port 8000 is already in use

Only one app can listen on the same port.

**Mac:**

```bash
lsof -tiTCP:8000 -sTCP:LISTEN
```

Use the printed number in place of `PID`:

```bash
kill PID
```

**Windows PowerShell:**

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen
Stop-Process -Id PID
```

Replace `PID` with the displayed `OwningProcess` number. Then start the app again.

**You should see:** Port `8000` become available and the app start normally.

### Docker cannot connect to the daemon

1. Open Docker Desktop.
2. Wait until Docker reports that its engine is running.
3. Run `docker compose up -d` again.

**You should see:** The database and app containers start.

### The health page says the database is unavailable

Run:

```bash
docker compose ps
docker compose logs db
docker compose up -d db
```

For the local Python launch, also run:

```bash
python -m app.db.init_db
```

**You should see:** The database become healthy and `/health` return `database` as `ok`.

### OpenAI, Jira, or GitHub rejects a key

1. Open `.env` and replace the rejected key with a current one.
2. Check OpenAI API billing, Jira site/email/token values, or GitHub repository permissions as appropriate.
3. Restart the app so it reloads `.env`.

GitHub push and pull-request access requires **Contents: Read and write** and **Pull requests: Read and write** for the target repository.

**You should see:** The previous `401`, `403`, or access error disappear on the next operation.

### I need multi-user roles, automatic merging, or non-Python test runners

These are **planned, not built**. The current UI uses single-operator Basic Auth, opens pull requests without merging them, and runs pytest for local and integration tests.

# Part Three: Stop, Restart, and Update

## 21. Stop the App Safely

### Docker launch

1. From the `SDLC` folder, run:

   ```bash
   docker compose down
   ```

   **You should see:** The app and database containers stop. Your database remains saved in its Docker volume.

2. Do **not** add `-v` unless you intentionally want to erase the local database, status map, ticket history, memories, repository index, and encrypted repository tokens.

### Local Python launch

1. In the terminal running Uvicorn, press `Ctrl+C`.

2. Stop PostgreSQL when you are finished:

   ```bash
   docker compose stop db
   ```

   **You should see:** Uvicorn stop and the database container stop without deleting its saved data.

## 22. Start Again Tomorrow

### Docker launch

1. Open Docker Desktop.
2. Open Terminal or PowerShell in the `SDLC` folder.
3. Run:

   ```bash
   docker compose up -d
   docker compose ps
   ```

4. Open [http://localhost:8000](http://localhost:8000).

   **You should see:** Both containers running, followed by your existing Tickets page and saved history.

### Local Python launch

1. Open Docker Desktop and a terminal in the `SDLC` folder.

2. Start PostgreSQL:

   ```bash
   docker compose up -d db
   ```

3. Activate the environment.

   **Mac:**

   ```bash
   source .venv/bin/activate
   ```

   **Windows PowerShell:**

   ```powershell
   .venv\Scripts\Activate.ps1
   ```

4. Start Uvicorn:

   ```bash
   uvicorn app.main:app --reload --port 8000
   ```

5. Open [http://localhost:8000](http://localhost:8000).

   **You should see:** Your saved tickets and settings.

## 23. Update to the Latest Version

Do not update while an agent is editing a repository or publishing a pull request.

1. Stop the app using Section 21.

2. From the `SDLC` folder, run:

   ```bash
   git status
   ```

   **You should see:** `nothing to commit, working tree clean`. If not, preserve your work before continuing.

3. Download the newest committed version:

   ```bash
   git pull origin main
   ```

   **You should see:** Git list updated files or say `Already up to date.`

4. Docker users rebuild and restart:

   ```bash
   docker compose up --build -d
   ```

5. Local Python users update packages and tables, then restart.

   **Mac:**

   ```bash
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   python -m app.db.init_db
   uvicorn app.main:app --reload --port 8000
   ```

   **Windows PowerShell:**

   ```powershell
   .venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   python -m app.db.init_db
   uvicorn app.main:app --reload --port 8000
   ```

6. Open [http://localhost:8000/health](http://localhost:8000/health).

   **You should see:** `status` as `ok`, `database` as `ok`, and `poller` as `running`.
