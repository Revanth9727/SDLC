# setup.md — Environment Setup (Do This Before Building)

> **Purpose.** `pre_codex.md` installed your tools and got your credentials. This
> document turns a bare folder into a running development environment: project
> structure, a Postgres+pgvector database in Docker, a Python 3.11 virtual
> environment, and a `.env` file holding your secrets. Every section ends with a
> **verification** you can see. When the final checklist passes, you have a working
> skeleton and you move to `codex_prompts.md` to build features.
>
> You will run these steps **yourself, by hand** — not through Codex. This is
> foundation; you want to understand it and know it works before Codex builds on top.

---

## 1. Create the project folder and structure

Pick where the project lives and create the top-level layout.

```bash
mkdir -p ~/projects/agentic-sdlc
cd ~/projects/agentic-sdlc
```

Create this structure (Codex will fill most files later; you're making the skeleton):

```
agentic-sdlc/
├── .env                  # your secrets (git-ignored) — created in section 4
├── .env.example          # template showing which vars exist (safe to commit)
├── .gitignore            # keeps secrets and junk out of git
├── docker-compose.yml    # defines the Postgres+pgvector container
├── requirements.txt      # Python dependencies
├── README.md
├── app/                  # the application code (Codex builds this)
│   ├── __init__.py
│   ├── agents/           # the 6 agents
│   ├── core/             # state object, orchestrator, guards
│   ├── integrations/     # openai, github, jira clients
│   ├── db/               # schema, models, migrations
│   └── web/              # FastAPI app + frontend
└── tests/                # automated tests (one folder per phase)
```

Make the folders now:
```bash
mkdir -p app/agents app/core app/integrations app/db app/web tests
touch app/__init__.py
```

**Verify:**
```bash
find . -type d | sort
```
You should see all the folders above.

- [ ] Folder structure exists

---

## 2. Initialise git (and protect your secrets FIRST)

Create `.gitignore` **before** you create `.env`, so a secret can never be committed
even by accident.

```bash
cat > .gitignore << 'EOF'
# secrets
.env
*.env
!.env.example

# python
__pycache__/
*.pyc
.venv/
venv/
.pytest_cache/

# os / editor
.DS_Store
.vscode/
.idea/

# runtime
*.log
EOF
```

Now initialise git:
```bash
git init
git add .gitignore
git commit -m "chore: add gitignore before any secrets exist"
```

**Verify:** `.env` will be ignored (test it once it exists in section 4). For now:
```bash
git status
```
Only `.gitignore` should be tracked.

- [ ] git initialised, `.gitignore` committed first

---

## 3. Start Postgres with pgvector (in Docker)

You will NOT install Postgres directly. This `docker-compose.yml` runs a Postgres
image that already includes the `pgvector` extension (needed later for the memory
layer). Using the pgvector image now means you never have to migrate the DB later.

```bash
cat > docker-compose.yml << 'EOF'
services:
  db:
    image: pgvector/pgvector:pg16
    container_name: agentic_sdlc_db
    environment:
      POSTGRES_USER: agentic
      POSTGRES_PASSWORD: localdevpassword
      POSTGRES_DB: agentic_sdlc
    ports:
      - "5432:5432"
    volumes:
      - agentic_pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U agentic -d agentic_sdlc"]
      interval: 5s
      timeout: 5s
      retries: 5

volumes:
  agentic_pgdata:
EOF
```

Start it:
```bash
docker compose up -d
```

**Verify (this is the "see it work" moment for the DB):**
```bash
# 1. container is healthy
docker compose ps

# 2. you can actually connect and the pgvector extension is available
docker exec -it agentic_sdlc_db psql -U agentic -d agentic_sdlc -c "SELECT 1 AS ok;"
docker exec -it agentic_sdlc_db psql -U agentic -d agentic_sdlc -c "CREATE EXTENSION IF NOT EXISTS vector; SELECT extname FROM pg_extension WHERE extname='vector';"
```
The first shows the container `running`/`healthy`. The second prints `ok = 1`. The
third prints `vector` — proving pgvector is installed and enabled. If you see
`vector`, your database is fully ready for every phase including memory.

- [ ] `docker compose ps` shows the DB healthy
- [ ] `SELECT 1` returns 1
- [ ] `vector` extension enables and lists

> **Note the connection string** you'll use everywhere:
> `postgresql://agentic:localdevpassword@localhost:5432/agentic_sdlc`

---

## 4. Create your `.env` (secrets) and `.env.example` (template)

First the **template** (safe to commit — no real values):
```bash
cat > .env.example << 'EOF'
# --- Database ---
DATABASE_URL=postgresql://agentic:localdevpassword@localhost:5432/agentic_sdlc

# --- OpenAI ---
OPENAI_API_KEY=sk-REPLACE_ME
OPENAI_MODEL=gpt-4o                 # swappable; see agent_context.md

# --- GitHub ---
GITHUB_TOKEN=github_pat_REPLACE_ME
GITHUB_OWNER=your-github-username
# GITHUB_REPO is the SANDBOX repo for local testing only. In real runs the target
# repo(s) are resolved from the ticket (web links/description) and confirmed by you —
# never hardcoded. See architecture.md §5b and ai_rules.md R-26.
GITHUB_REPO=agentic-sdlc-sandbox

# --- Jira ---
JIRA_BASE_URL=https://your-site.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=REPLACE_ME
JIRA_PROJECT_KEY=SANDBOX

# --- Jira status mapping (dynamic; match YOUR project's workflow names) ---
# Internal stage -> the Jira status NAME to transition to. Leave blank to skip a stage.
# The system discovers allowed transitions at runtime and matches these names
# case-insensitively; a name not in your workflow is skipped with a warning (never crashes).
JIRA_STATUS_IN_PROGRESS=In Progress
JIRA_STATUS_AWAITING_APPROVAL=Awaiting Approval
JIRA_STATUS_IN_REVIEW=In Review
JIRA_STATUS_BLOCKED=Blocked
JIRA_STATUS_DONE=Done

# --- Jira polling (scheduled intake) ---
# How often to poll Jira for new "To Do" tickets to pick up. Default 30.
JIRA_POLL_INTERVAL_MINUTES=30
EOF

git add .env.example docker-compose.yml .gitignore
git commit -m "chore: add env template and docker compose"
```

Now the **real** `.env` (copy the template, then paste your actual secrets from your
safe note):
```bash
cp .env.example .env
```
Open `.env` in your editor and replace every `REPLACE_ME` / placeholder with the real
values from `pre_codex.md`:
- `OPENAI_API_KEY` → your `sk-...`
- `GITHUB_TOKEN` → your `github_pat_...`, and set `GITHUB_OWNER` to your username
- `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY` → your Jira values

**Verify secrets are protected (critical):**
```bash
git status
```
`.env` must **NOT** appear in the list. If it does, your `.gitignore` is wrong — fix
it before continuing. This check is what stops you leaking keys.

- [ ] `.env` filled with real values
- [ ] `git status` does NOT show `.env`

---

## 5. Python virtual environment + core dependencies

Create an isolated environment so project packages don't collide with your system.

```bash
python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python --version                 # should now print 3.11.x
```

Create `requirements.txt` with the core stack (Codex adds more per phase):
```bash
cat > requirements.txt << 'EOF'
# web
fastapi==0.115.*
uvicorn[standard]==0.32.*
jinja2==3.1.*
sse-starlette==2.1.*

# agents / orchestration
langgraph==0.2.*
langchain-core==0.3.*
openai==1.54.*

# db
psycopg[binary]==3.2.*
sqlalchemy==2.0.*
pgvector==0.3.*

# validation / config
pydantic==2.9.*
pydantic-settings==2.6.*
python-dotenv==1.0.*

# integrations
PyGithub==2.5.*
httpx==0.27.*

# tests
pytest==8.3.*
pytest-asyncio==0.24.*
EOF

pip install --upgrade pip
pip install -r requirements.txt
```

**Verify:**
```bash
python -c "import fastapi, langgraph, openai, psycopg, pydantic, github; print('all core imports OK')"
```
Prints `all core imports OK`. If any import fails, that one line tells you exactly
which package didn't install.

- [ ] venv active, Python 3.11
- [ ] `all core imports OK`

---

## 6. One tiny end-to-end connectivity test (prove all 3 credentials work)

Before Codex builds anything, confirm your three external services actually answer.
Create this throwaway script, run it, then delete it.

```bash
cat > _connectivity_check.py << 'EOF'
import os
from dotenv import load_dotenv
load_dotenv()

# 1. OpenAI
from openai import OpenAI
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
r = client.chat.completions.create(
    model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
    messages=[{"role": "user", "content": "reply with the single word: pong"}],
    max_tokens=5,
)
print("OpenAI:", r.choices[0].message.content.strip())

# 2. GitHub
from github import Github, Auth
gh = Github(auth=Auth.Token(os.environ["GITHUB_TOKEN"]))
repo = gh.get_repo(f'{os.environ["GITHUB_OWNER"]}/{os.environ["GITHUB_REPO"]}')
print("GitHub: reached repo", repo.full_name)

# 3. Jira
import httpx
jira = httpx.get(
    f'{os.environ["JIRA_BASE_URL"]}/rest/api/3/myself',
    auth=(os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"]),
)
jira.raise_for_status()
print("Jira: authenticated as", jira.json()["emailAddress"])

print("\nALL THREE INTEGRATIONS RESPONDED ✅")
EOF

python _connectivity_check.py
```

**Verify — you want to literally see all three answer:**
- `OpenAI: pong`
- `GitHub: reached repo your-username/agentic-sdlc-sandbox`
- `Jira: authenticated as you@example.com`
- `ALL THREE INTEGRATIONS RESPONDED ✅`

If any line errors, you've isolated exactly which credential is wrong — fix that one,
rerun. When all three pass, clean up:
```bash
rm _connectivity_check.py
```

- [ ] OpenAI responded
- [ ] GitHub responded
- [ ] Jira responded

> This is the single most valuable check in the whole setup: it proves the three
> hardest-to-debug pieces work *in isolation*, before any agent code exists. When a
> phase later fails, you'll know it's your logic — not your credentials.

---

## 7. Final readiness checklist (all true before `codex_prompts.md`)

- [ ] Project structure created
- [ ] git initialised, `.gitignore` committed first, `.env` NOT tracked
- [ ] Postgres+pgvector running and healthy in Docker
- [ ] `vector` extension enabled
- [ ] `.env` filled with real secrets
- [ ] venv active on Python 3.11, all core imports OK
- [ ] All three integrations responded in the connectivity check

When every box is checked, your environment is real and verified. Open
`codex_prompts.md` and start Phase 1.

---

## 8. Everyday commands (bookmark these)

```bash
# start your day
cd ~/projects/agentic-sdlc
source .venv/bin/activate
docker compose up -d

# check the DB
docker exec -it agentic_sdlc_db psql -U agentic -d agentic_sdlc

# stop the DB at end of day (data persists in the volume)
docker compose down

# nuke the DB entirely and start fresh (destroys data)
docker compose down -v
```
