# pre_codex.md — Start Here (Before You Touch Codex)

> **Purpose.** This is the very first thing you read. It gets your machine and your
> accounts ready *before* you ask Codex to build anything. If you skip steps here,
> Codex will generate code that fails on the first run because a tool isn't installed
> or a credential is missing — and you won't know which of the six moving parts broke.
> Do everything here, in order, and check each box. Only when every box is checked do
> you move to `setup.md`.
>
> Estimated time: 60–90 minutes (mostly waiting on account/token screens).

---

## 0. What you are about to build (one paragraph, so the setup makes sense)

You are building an autonomous software-engineering system. A ticket comes in (from
Jira). A team of AI agents reads it, splits it into isolated pieces of work, plans
each piece, shows you the plan, waits for your approval, then reads your code on
GitHub, makes surgical edits, runs tests, and opens a real pull request — while you
watch each agent activate live in a web page. To make that real, your machine needs:
a container engine (Docker) to run the database, Python 3.11 to run the code, git to
talk to GitHub, and three API credentials (OpenAI, GitHub, Jira). This document
installs the tools and gets the credentials.

---

## 1. Install the tools

Four things must be installed **before** anything else. Each has a verification
command. Do not proceed past a step until its verification prints a version.

### 1.1 Docker Desktop (runs Postgres for you)

Why: your database (Postgres + the pgvector extension) runs inside a container so you
don't install and configure Postgres by hand. Docker runs that container.

- **macOS:** <https://www.docker.com/products/docker-desktop/> → download the build
  matching your chip (Apple menu → About This Mac tells you Apple Silicon vs Intel) →
  open the `.dmg` → drag Docker to Applications → launch it → wait until the menu-bar
  whale icon stops animating (engine running).
- **Windows:** download Docker Desktop → run installer → accept the WSL 2 prompt if
  shown → reboot if asked → launch → wait for "Engine running."

**Verify:**
```bash
docker --version
docker run hello-world
```
First prints `Docker version 27.x`. Second prints "Hello from Docker!". If
`hello-world` fails, the engine isn't running — open Docker Desktop and wait.

- [ ] `docker --version` prints a version
- [ ] `docker run hello-world` prints the hello message

### 1.2 Python 3.11 (runs the application)

Why: the system is Python. We pin **3.11** (not 3.12/3.13) because some agent/ML
libraries lag the newest Python; matching versions avoids install failures.

- **macOS:** install Homebrew from <https://brew.sh> if needed, then
  `brew install python@3.11`.
- **Windows:** get the 3.11 installer from
  <https://www.python.org/downloads/release/python-3119/> ("Windows installer
  64-bit"). On the first screen **check "Add python.exe to PATH"** before Install.

**Verify:**
```bash
python3.11 --version
```
Must print `Python 3.11.x`. We always call `python3.11` explicitly to be unambiguous.

- [ ] `python3.11 --version` prints `3.11.x`

### 1.3 git (talks to GitHub)

Why: the system clones your repo, makes branches, pushes commits.

- **macOS:** `brew install git` (may already be present — check first).
- **Windows:** <https://git-scm.com/download/win> → run installer → accept defaults.

**Verify:**
```bash
git --version
```

- [ ] `git --version` prints a version

### 1.4 A code editor

If you don't already use one, install **VS Code** (<https://code.visualstudio.com/>).
Codex runs inside it and you'll want proper markdown rendering for these docs.

- [ ] An editor is installed and can open this folder

---

## 2. Get your three credentials

The system talks to three services; each needs a secret. **Treat these like
passwords** — never paste them into a chat, never commit them to git, never put them
in a screenshot. In `setup.md` you'll place them in a `.env` file that git ignores.

### 2.1 OpenAI API key (the brains of the agents)

1. <https://platform.openai.com/> → sign in / create account.
2. **Add billing first** (API access is separate from ChatGPT Plus). Profile
   (top-right) → **Billing** → add a card. Set a low monthly usage limit (e.g. $20)
   while developing — a billing cap is a good second net behind the loop guards.
3. Left sidebar → **API keys** (<https://platform.openai.com/api-keys>).
4. **Create new secret key**, name it `agentic-sdlc-dev`.
5. **Copy it now** — you can't see it again. It starts with `sk-`.
6. Paste into a temporary safe note (you'll move it to `.env` in `setup.md`).

- [ ] I have an OpenAI key starting with `sk-`
- [ ] I set a billing limit

### 2.2 GitHub Personal Access Token (opens PRs on your repo)

**First, make a test repo:**
1. <https://github.com/new>.
2. Name `agentic-sdlc-sandbox`, set **Private**, check **Add a README file**.
   Create it.

**Now a fine-grained token (safer than classic):**
1. <https://github.com/settings/tokens?type=beta> (Settings → Developer settings →
   Personal access tokens → **Fine-grained tokens**).
2. **Generate new token**. Name `agentic-sdlc-dev`. Expiration 90 days.
3. **Resource owner:** your username.
4. **Repository access:** **Only select repositories** → pick `agentic-sdlc-sandbox`.
5. **Permissions → Repository permissions**, set to **Read and write**:
   - **Contents** (push branches/commits)
   - **Pull requests** (open PRs)
   - **Metadata** auto-selects read-only — fine.
6. Generate. **Copy now** (starts with `github_pat_`).

- [ ] Private repo `agentic-sdlc-sandbox` exists
- [ ] Fine-grained token with Contents + Pull requests write

### 2.3 Jira API token (receives tickets)

**First, a free Jira Cloud site (if you don't have one):**
1. <https://www.atlassian.com/software/jira/free> → start a free site. You'll get a
   URL like `https://your-name.atlassian.net`.
2. Create a simple software project. Note its **project key** (issue prefix, e.g.
   `SANDBOX` → issues look like `SANDBOX-1`).

**Now the API token:**
1. <https://id.atlassian.com/manage-profile/security/api-tokens>.
2. **Create API token**, label `agentic-sdlc-dev`. **Copy it now.**
3. Note **the email address** of your Atlassian account — Jira's API authenticates
   with *email + token together*, not the token alone.

- [ ] Jira site URL (`https://something.atlassian.net`)
- [ ] A project with a known project key
- [ ] Jira token + the email it pairs with

---

## 3. Pre-flight checklist (all true before `setup.md`)

Tools:
- [ ] Docker running (`docker run hello-world`)
- [ ] Python 3.11 (`python3.11 --version`)
- [ ] git (`git --version`)
- [ ] An editor installed

Credentials (in a temporary safe note, NOT in git yet):
- [ ] OpenAI key (`sk-...`)
- [ ] GitHub token (`github_pat_...`) + sandbox repo exists
- [ ] Jira token + paired email + site URL + project key

All checked? Open `setup.md`.

---

## 4. Ground rules that will save you (read once)

- **Never paste a real secret into Codex, a chat, or a commit.** Codex only needs to
  know a variable named e.g. `OPENAI_API_KEY` exists — never its value.
- **One integration at a time.** Even in "everything in Phase 1," each piece is
  verified alone before the next. If something breaks, you'll know which piece,
  because you saw the previous one work.
- **If a verification fails, stop.** Don't push on hoping it resolves — a missing
  tool or bad credential now becomes an un-debuggable mess three steps later.
- **You will see it work at every step.** That's the design. If a step doesn't end
  with something visible or testable, something is wrong with the step.
