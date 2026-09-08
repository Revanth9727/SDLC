# Agentic SDLC

An AI-powered software development lifecycle agent system.

## Setup

See `docs/setup.md` for environment setup instructions.

## Structure

```
agentic-sdlc/
├── .env                  # your secrets (git-ignored)
├── .env.example          # template showing which vars exist
├── .gitignore
├── docker-compose.yml    # Postgres+pgvector container
├── requirements.txt      # Python dependencies
├── app/
│   ├── agents/           # the 6 agents
│   ├── core/             # state object, orchestrator, guards
│   ├── integrations/     # openai, github, jira clients
│   ├── db/               # schema, models, migrations
│   └── web/              # FastAPI app + frontend
└── tests/
```

## Quick Start

```bash
source .venv/bin/activate
docker compose up -d
```
