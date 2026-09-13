# Agentic SDLC

Agentic SDLC turns a Jira ticket into a proposed code change. AI agents read the ticket and linked GitHub code, split larger requests into smaller jobs, diagnose the problem, and prepare a plan. You approve the plan before code is changed. The app then edits a separate branch, runs the repository's tests, checks the result, and opens a GitHub pull request for review.

## Before You Start

Allow about 60 to 90 minutes for the first setup. You need:

1. An [OpenAI API account](https://platform.openai.com/) with API billing enabled.
2. A [GitHub account](https://github.com/) and a repository you can test with.
3. A [Jira Cloud account](https://www.atlassian.com/software/jira) and a Jira project.
4. [Docker Desktop](https://www.docker.com/products/docker-desktop/) for the PostgreSQL 16 database and pgvector.
5. [Python 3.11](https://www.python.org/downloads/release/python-3119/) for the app.
6. [Git](https://git-scm.com/downloads) for downloading and changing repositories.

The Python package versions are pinned in [`requirements.txt`](requirements.txt). You do not install them one at a time.

## Get Your Access Keys

Access keys are passwords for software. Never put them in Jira, screenshots, chats, or Git commits.

### OpenAI

1. Sign in at [platform.openai.com](https://platform.openai.com/).
2. Add a payment method under Billing. ChatGPT subscriptions do not include API usage.
3. Open [API keys](https://platform.openai.com/api-keys).
4. Create and save a secret key. It normally starts with `sk-`.

### GitHub

1. Open [GitHub fine-grained tokens](https://github.com/settings/tokens?type=beta).
2. Create a token for the repository the app will change.
3. Give it **Read and write** access to **Contents** and **Pull requests**.
4. Copy the token when GitHub shows it.

The app tries anonymous access first for public repositories. Current settings still require `GITHUB_TOKEN` for GitHub API, push, and pull-request operations. A private repository can also receive its own token through the app. That token is encrypted in the database and is never shown again.

### Jira

1. Open [Atlassian API tokens](https://id.atlassian.com/manage-profile/security/api-tokens).
2. Create and copy a token.
3. Note your Jira site address, such as `https://example.atlassian.net`.
4. Note your short project key, such as `SCRUM`.

## Install The Tools

### Mac

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/). Choose Apple Silicon or Intel to match your Mac. Open Docker Desktop and wait for it to start.

   ```bash
   docker --version
   docker run hello-world
   ```

   You should see a Docker version and `Hello from Docker!`.

2. Install [Homebrew](https://brew.sh/) if needed. Then install Python 3.11.

   ```bash
   brew install python@3.11
   python3.11 --version
   ```

   You should see `Python 3.11.x`.

3. Install Git.

   ```bash
   brew install git
   git --version
   ```

   You should see a Git version number.

### Windows

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/). Accept WSL 2 if asked. Restart Windows if requested. Open Docker Desktop and wait until it says the engine is running.

   ```powershell
   docker --version
   docker run hello-world
   ```

   You should see a Docker version and `Hello from Docker!`.

2. Download the 64-bit installer for [Python 3.11](https://www.python.org/downloads/release/python-3119/). Check **Add python.exe to PATH** during installation.

   ```powershell
   py -3.11 --version
   ```

   You should see `Python 3.11.x`.

3. Install [Git for Windows](https://git-scm.com/download/win). Keep the default choices.

   ```powershell
   git --version
   ```

   You should see a Git version number.

## Download The Project

Use Terminal on Mac or PowerShell on Windows:

```bash
git clone https://github.com/Revanth9727/SDLC.git
cd SDLC
```

The folder should contain `app`, `docs`, `docker-compose.yml`, and `requirements.txt`.

## Create Your Settings File

The `.env` file holds settings and secrets. Git ignores it.

**Mac:**

```bash
cp .env.example .env
```

**Windows:**

```powershell
Copy-Item .env.example .env
```

Open `.env` in a text editor. Replace every placeholder. Do not add quotes unless the example uses JSON.

You will generate a unique `APP_ENCRYPTION_KEY` after installing the Python packages below. Do not reuse the example value.

### Required settings

| Variable | Plain-language meaning |
| --- | --- |
| `DATABASE_URL` | Database connection. Keep `postgresql://agentic:localdevpassword@localhost:5432/agentic_sdlc` for this local setup. |
| `OPENAI_API_KEY` | Your OpenAI API key. |
| `GITHUB_TOKEN` | Your GitHub token for API, push, and pull-request access. |
| `GITHUB_OWNER` | Your GitHub username or organization. |
| `GITHUB_REPO` | Sandbox repository used by older local tests. Real tickets use their confirmed repository. |
| `JIRA_BASE_URL` | Your Jira site, such as `https://example.atlassian.net`. |
| `JIRA_EMAIL` | Your Jira account email. |
| `JIRA_API_TOKEN` | Your Atlassian API token. |
| `JIRA_PROJECT_KEY` | Your short Jira project key, such as `SCRUM`. |

### Models, limits, and storage

These variables all have defaults in `app/config.py`. The `.env.example` values are suitable for a first run.

| Variable | Plain-language meaning |
| --- | --- |
| `OPENAI_MODEL` | Default model when a specific tier is not selected. Default: `gpt-4o`. |
| `MODEL_STRONG` | Model for harder reasoning. Default: `gpt-4o`. |
| `MODEL_CHEAP` | Model for simpler work. Default: `gpt-4o-mini`. |
| `OPENAI_EMBED_MODEL` | Model used to compare code and past solutions by meaning. Default: `text-embedding-3-small`. |
| `TICKET_CALL_BUDGET` | Maximum model calls for one ticket. Default: `40`. |
| `TICKET_TOKEN_BUDGET` | Maximum model tokens for one ticket. Default: `100000`. |
| `TICKET_COST_BUDGET_USD` | Maximum estimated model cost for one ticket. Default: `2.00`. |
| `TICKET_TIME_BUDGET_SECONDS` | Maximum time from the ticket's first model call. Default: `7200`. |
| `LLM_EST_COST_PER_1K_TOKENS` | Fallback cost estimate for unlisted models. Default: `0.01`. |
| `MAX_AGENT_RETRIES` | Maximum retries in bounded agent loops. Default: `2`. |
| `TEST_TIMEOUT_SECONDS` | Maximum seconds for a local test run. Default: `120`. |
| `MAX_EDIT_FILE_CHARS` | Largest source file handled by the normal edit flow. Default: `60000`. |
| `WORKSPACE_MIN_FREE_MB` | Minimum free disk space for repository workspaces. Default: `100`. |
| `WORKSPACE_ROOT` | Optional location for isolated repository copies. Blank uses `/tmp/agentic-workspaces`. |
| `REPO_CACHE_DIR` | Older optional repository cache location. It can remain blank. |
| `APP_ENCRYPTION_KEY` | Key used to encrypt private-repository tokens in the database. |
| `REPO_INDEX_MAX_FILE_BYTES` | Largest file included in repository indexing. Default: `1000000`. |
| `REPO_INDEX_EXCLUDED_DIRS` | JSON list of generated or vendor folders excluded from indexing. |
| `CODE_INTELLIGENCE_MAX_STEPS` | Maximum code-investigation steps. Default: `18`. |
| `MEMORY_TOP_K` | Maximum similar past solutions recalled. Default: `3`. |
| `MEMORY_SEARCH_THRESHOLD` | Minimum similarity for recalling a past solution. Default: `0.75`. |
| `MEMORY_REUSE_SIMILARITY_THRESHOLD` | Similarity required to reuse a past plan. Default: `0.9`. |
| `LLM_CACHE_ENABLED` | Turns the model-response cache on or off. Default: `true`. |
| `LLM_CACHE_SEMANTIC_THRESHOLD` | Similarity required for a semantic cache hit. Default: `0.92`. |
| `LLM_CACHE_TICKET_PROMPTS` | Allows ticket-specific prompts in the shared cache. Safe default: `false`. |

### Jira workflow settings

| Variable | Plain-language meaning |
| --- | --- |
| `JIRA_POLL_INTERVAL_MINUTES` | Minutes between automatic Jira checks. Default: `30`. |
| `STUCK_THRESHOLD_MINUTES` | Minutes before a non-finished ticket may receive a reminder. Default: `120`. |
| `HUMAN_GATE_REMINDER_MINUTES` | Minutes before one approval reminder. Default: `60`. |
| `HUMAN_GATE_EXPIRY_MINUTES` | Minutes before unanswered approval expires. Default: `1440`. |
| `JIRA_STATUS_CACHE_TTL_SECONDS` | Seconds project status information stays cached. Default: `300`. |
| `JIRA_STATUS_IN_PROGRESS` | Optional preferred status name for started work. Usually blank. |
| `JIRA_STATUS_AWAITING_APPROVAL` | Optional preferred status name while waiting for approval. Usually blank. |
| `JIRA_STATUS_IN_REVIEW` | Optional preferred status name after a pull request opens. Usually blank. |
| `JIRA_STATUS_BLOCKED` | Optional preferred status name for blocked work. Usually blank. |
| `JIRA_STATUS_DONE` | Optional preferred status name for finished work. Usually blank. |
| `JIRA_STATUS_FALLBACKS` | Optional JSON object of fallback status names. Default: `{}`. |
| `JIRA_APPROVAL_ACCOUNT_IDS` | JSON list of Jira account IDs allowed to approve, besides the assignee. Default: `[]`. |
| `JIRA_BOT_ACCOUNT_ID` | Optional account ID used by the app. Blank makes the app discover it through Jira. |

### Runtime and advanced settings

| Variable | Plain-language meaning |
| --- | --- |
| `GIT_AUTHOR_NAME` | Name written on commits. Default: `SDLC Agent`. |
| `GIT_AUTHOR_EMAIL` | Email written on commits. Default: `sdlc-agent@users.noreply.github.com`. |
| `UI_BASIC_AUTH_USERNAME` | Username protecting the operator pages. Blank disables protection locally. |
| `UI_BASIC_AUTH_PASSWORD` | Password used with the operator username. |
| `LOG_LEVEL` | Amount of server log detail. Default: `INFO`. |
| `EXTERNAL_RETRY_ATTEMPTS` | Retries for temporary OpenAI, GitHub, or Jira errors. Default: `3`. |
| `EXTERNAL_RETRY_BASE_SECONDS` | Initial delay between external retries. Default: `0.5`. |
| `GITHUB_WEBHOOK_SECRET` | Optional secret for checking GitHub webhook signatures. |
| `JIRA_WEBHOOK_SECRET` | Optional secret placed in the Jira webhook URL. |

Webhooks are an advanced, faster notification option. They are not required for polling. GitHub sends signed events to `/webhooks/github`. Jira sends events to `/webhooks/jira?secret=YOUR_JIRA_WEBHOOK_SECRET`.

## Start The App

This method runs the database in Docker and the Python app on your computer.

### 1. Start the database

Make sure Docker Desktop is open.

```bash
docker compose up -d db
docker compose ps
```

You should see `agentic_sdlc_db` with a healthy status. It uses port `5432`.

### 2. Create a private Python environment

**Mac:**

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

**Windows PowerShell:**

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

You should see `(.venv)` at the start of the terminal line. Installation should finish without an error.

If PowerShell blocks activation, run this once in that window and activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Now generate the encryption key with the active Python environment:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the printed value after `APP_ENCRYPTION_KEY=` in `.env`. Do not share it.

### 3. Create the database tables

```bash
python -m app.db.init_db
```

It should return to the prompt without an error.

### 4. Start the web app

```bash
uvicorn app.main:app --reload --port 8000
```

You should see `Uvicorn running on http://127.0.0.1:8000`. Leave this terminal open.

Open [http://localhost:8000](http://localhost:8000). If both Basic Auth values are set, use that username and password.

Check [http://localhost:8000/health](http://localhost:8000/health). A healthy response contains:

```json
{"status":"ok","database":"ok","poller":"running"}
```

## First Run: Match Your Jira Statuses

Your first visit redirects to [http://localhost:8000/settings/statuses](http://localhost:8000/settings/statuses).

1. Wait for the real statuses from your Jira project.
2. Give each status its meaning: ready, work started, in review, blocked, or done.
3. Review any low-confidence suggestion.
4. Save the status map.

You should return to the ticket list. The map is saved in PostgreSQL. Change it later through **Settings · Jira statuses**.

If Jira statuses do not load, check `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, and `JIRA_PROJECT_KEY`. Restart Uvicorn after changing `.env`.

## Send Your First Ticket

Start with a small test repository and a safe issue.

1. Create a Jira issue in the project named by `JIRA_PROJECT_KEY`.
2. Put it in a Jira status mapped as **ready**.
3. Describe the current problem and expected result.
4. Include `https://github.com/OWNER/REPOSITORY` in the summary, description, environment, or a comment.
5. In Agentic SDLC, click **Run poll now**. You can instead wait for the automatic poll.
6. Open the new ticket in Agentic SDLC.
7. Click **Resolve repos** and confirm the repository. For a private repository, enter a token in the masked field if asked.
8. Click **Run diagnosis & plan**.
9. Read and confirm the Planner's understanding.
10. Review each sub-task plan. Click **Approve** or **Reject**.
11. Watch **Live activity** for repository indexing, code investigation, diagnosis, edits, tests, Critic review, integration, and publication.

When work succeeds, the page shows the Critic verdict and a GitHub pull-request link. The app never pushes directly to the default branch. Review and merge the pull request in GitHub.

## If Something Goes Wrong

### Docker is not running

**What you see:** `Cannot connect to the Docker daemon`.

Open Docker Desktop and wait. Then run:

```bash
docker compose up -d db
docker compose ps
```

### The database is unavailable

**What you see:** `/health` says `"database":"unavailable"`, or Python mentions port `5432`.

```bash
docker compose ps
docker compose logs db
docker compose up -d db
python -m app.db.init_db
```

### Port 8000 is already in use

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

Replace `PID` with the `OwningProcess` number.

### An access key is rejected

- OpenAI `401`: create a current [OpenAI API key](https://platform.openai.com/api-keys), check API billing, update `.env`, and restart Uvicorn.
- Jira cannot load: check the Jira site URL, email, token, and project key. The base URL must look like `https://example.atlassian.net`.
- GitHub `403` or repository not found: check the repository link and token permissions. The token needs read/write **Contents** and **Pull requests** access.

### The browser keeps asking for a password

Use the exact `UI_BASIC_AUTH_USERNAME` and `UI_BASIC_AUTH_PASSWORD` from `.env`. Restart Uvicorn after changing them.

## Stop And Restart

### Stop for the day

1. Press `Ctrl+C` in the Uvicorn terminal.
2. Stop the database:

   ```bash
   docker compose stop db
   ```

The database remains saved.

### Start again

1. Open Docker Desktop.
2. Open a terminal in the `SDLC` folder.
3. Start the database:

   ```bash
   docker compose up -d db
   ```

4. Activate Python.

   **Mac:**

   ```bash
   source .venv/bin/activate
   ```

   **Windows:**

   ```powershell
   .venv\Scripts\Activate.ps1
   ```

5. Start the app:

   ```bash
   uvicorn app.main:app --reload --port 8000
   ```

6. Open [http://localhost:8000](http://localhost:8000).

To stop containers without erasing the database:

```bash
docker compose down
```

Do not use `docker compose down -v` unless you want to erase the local database.

## Optional: Run Everything In Docker

After `.env` is complete, Docker can run both services:

```bash
docker compose up --build -d
```

Open [http://localhost:8000](http://localhost:8000). The app container runs `python -m app.db.init_db` before Uvicorn.

```bash
docker compose ps
docker compose logs -f app
```

Stop both services with:

```bash
docker compose down
```
