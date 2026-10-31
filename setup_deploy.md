# dev-decide: run it locally and deploy it live

This guide is for **dev-decide 5.2** (the GitHub repository). Part 1 sets it up on your own computer:
the Claude Code skill, the command line and the local HTTP API. Part 2 puts the HTTP API on the internet, on a
free platform.

> **Use 5.0.1 or later for everything in Part 2.** The HTTP API in 5.0 has no token check and lets callers read
> files on the machine it runs on, so it must never be reachable from the internet. 5.0.1 added both protections
> and otherwise decides exactly like 5.0.

Platform limits in Part 2 were checked against each provider's documentation on 19–23 October 2026. Free tiers
change often, so look at the provider's page again before you rely on a limit.

> **Quickest start: install the plugin.** In Claude Code run `/plugin marketplace add pawanptu/dev-decide`, then
> `/plugin install dev-decide@dev-decide`. That gives you the skill, starter models, a documented settings file
> (`.claude/dev-decide.json`, written on first use) and the routing hint. Part 1 below is the manual route, and is
> still the way to go when you want the HTTP API on your computer.

**Contents**
- [Part 1: Run it locally](#part-1-run-it-locally) (steps 1–12)
- [Part 2: Deploy it live](#part-2-deploy-it-live)
  - [Deploy straight from the GitHub repository](#deploy-straight-from-the-github-repository) (simplest: Render)
  - [Choose a platform](#choose-a-platform) (three free options; ★ marks the best one)
  - [Prepare the server folder](#prepare-the-server-folder) (needed for every option)
  - [Option A ★ Oracle Cloud Always Free](#option-a--oracle-cloud-always-free-best)
  - [Option B: Render](#option-b-render-free-web-service)
  - [Option C: Koyeb](#option-c-koyeb-free-instance)
  - [Use your live API](#use-your-live-api)
  - [Keep it safe and cheap](#keep-it-safe-and-cheap)
- [Troubleshooting](#troubleshooting)
- [Sources](#sources)

---

## Part 1: Run it locally

### Step 1. Check what you need

| You need | Check it with | Notes |
|---|---|---|
| Python 3.8 or later | `python3 --version` | dev-decide uses only the standard library, so there is nothing to `pip install`. On Windows, type `py -3` wherever this guide says `python3`. |
| git | `git --version` | Used to read diffs (`DIFF`, `COMMIT`). |
| Claude Code | `claude --version` | For the `/dev-decide` skill, and the default way dev-decide asks Claude. |
| *(optional)* An Anthropic API key | | Faster model calls, and needed on a server. Create one in the Claude Console. |
| *(optional)* numpy, scipy, scikit-learn | | Only to *train* your own starting models with the kit. |
| *(optional)* Docker | `docker --version` | Only to test the server image on your computer before you deploy it. |

On Windows, the skill needs Git Bash (Claude Code's default shell when Git for Windows is installed) or WSL. The
command line and the HTTP API run anywhere Python does.

### Step 2. Get the code

```bash
cd ~
git clone https://github.com/pawanptu/dev-decide
cd ~/dev-decide
ls
# CHANGELOG.md  README.md  dev-decide-kit  dev-decide-server  dev-decide-v5  plugin  plugin-graph  setup_deploy.md  tools ...
grep '^VERSION' dev-decide-v5/skill/scripts/ddcore.py
# VERSION = "5.3.0"
```

If you already have a clone, run `git pull` in it instead. The installed skill and everything dev-decide has
learned are stored elsewhere, so re-cloning loses nothing.

- `dev-decide-v5/` holds the engine, the Claude Code skill and the bundled meaning model (30 MB, MIT licence).
- `dev-decide-kit/` is the training kit, with pretrained starting models in `dev-decide-kit/pretrained/`.
- `plugin/` and `plugin-graph/` are the Claude Code plugins, `dev-decide-server/` is the deployed server's own
  project, and `tools/` holds the tests and benchmarks.

The rest of this guide assumes the folder is `~/dev-decide`. If you put it somewhere else, change that path
in the commands.

### Step 3. Install the skill

For all your projects (installs to `~/.claude/skills/dev-decide`):

```bash
python3 ~/dev-decide/dev-decide-v5/install.py
```

Or for one repository only. Run it from inside that repository, because it installs into `./.claude`:

```bash
cd /path/to/your/repo
python3 ~/dev-decide/dev-decide-v5/install.py --project
```

The installer replaces any earlier dev-decide (3.x, 4.x or 5.0) and keeps what each repository has learned. If
Claude Code is already open, run `/reload-skills` in it (or start a new session) so it loads the skill.

**To share it with your team**, use the project install and commit `.claude/skills/dev-decide/`. Everyone who
pulls gets the skill. What dev-decide learns stays in each clone's `.claude/dev-decide/` folder, which it marks as
ignored by git, so it never ends up in a commit.

### Step 4. Choose how dev-decide asks Claude

Most answers come from local tiers in milliseconds. The rest go to Claude, through one of these backends:

| Backend | When it is used | Setup |
|---|---|---|
| `api` | `ANTHROPIC_API_KEY` is set (the default then) | Add `export ANTHROPIC_API_KEY=...` to your shell profile. Never put the key in a file you commit. |
| `cli` | No API key, and Claude Code (`claude`) is installed | Nothing to do: it uses your Claude Code sign-in. |
| `none` | Neither is available, or you choose it | Local answers only. Questions the local tiers can't settle come back undecided or flagged for a human. |

To force one, set `DEV_DECIDE_BACKEND=api` (or `cli` or `none`), or set `"backend"` in the policy file (Step 10).

### Step 5. Check that it works

Run this in a repository you work on. `+local` means it makes no model call:

```bash
cd /path/to/your/repo
DD=~/.claude/skills/dev-decide/scripts/decide.py     # after a --project install: DD=.claude/skills/dev-decide/scripts/decide.py
python3 $DD --summary "triage +local :: Login returns 500 for users with a plus sign in their email"
```

You get one line, like this:

```
dev-decide | issue_type bug 0.44? | severity sev3_minor 0.40? | domain error | effort error | needs_clarification 0.15 | model_tier sonnet 0.60 | needs a human: issue_type, severity | 0 local + 4 hint | 38 ms
```

Each `key answer confidence` group is one decision. A `?` marks an answer below its confidence threshold, which
dev-decide passes to a human instead of acting on. `error` means "not decided": `+local` stopped it from asking
Claude. The end of the line says how the answers were made (a `hint` is a quick local estimate) and how long it
took.

Now let it ask Claude where it needs to:

```bash
python3 $DD --summary "issue_type,severity :: Payment page shows the wrong currency symbol for EUR"
python3 $DD --summary status        # what it has learned so far
```

With a backend from Step 4, the questions the local tiers can't settle are answered by Claude in a few seconds.
Without one you still get the local estimates, and the full JSON answer (the same command without `--summary`)
carries the warning "no model backend available".

### Step 6. Add starting models (optional, recommended)

The kit ships models trained on 821 examples, so triage and pull-request questions can be answered locally from
day one. Exporting them needs no extra packages. Run it in the same repository:

```bash
cd /path/to/your/repo
python3 ~/dev-decide/dev-decide-kit/kit.py export \
    --data ~/dev-decide/dev-decide-kit/pretrained --repo . \
    --only risk,breaking_change,issue_type,severity,domain
```

It prints one line per model, then `Installed 5 model(s) into …/.claude/dev-decide/models`. `issue_type` is
`enabled`. The others say "installed, not enabled": they keep asking Claude and switch themselves on once they have
learned enough from use. Run the first command from Step 5 again: `issue_type` now comes from the local model
(`1 local` at the end of the line).

Leave out `route` unless your agents and skills match `pretrained/routes.json`. To train models on your own
agents, skills and data, install `numpy scipy scikit-learn` and follow `dev-decide-kit/README.md`.

### Step 7. Use it in Claude Code

```
/dev-decide pr :: DIFF
/dev-decide triage :: <paste an issue>
/dev-decide route,model_tier :: add rate limiting to the login endpoint
/dev-decide choose web|api|worker :: <text>
/dev-decide fix last issue_type=bug
/dev-decide status
```

Inputs are `DIFF` (uncommitted changes), `DIFF --staged`, `COMMIT <sha>`, `@path/to/file`, or plain text. Flags go
before `::`: `+why`, `+local`, `+llm`, `+deep`, `+json` and `+nocache`. `dev-decide-v5/README.md` lists every
preset and bundle.

### Step 8. Use it from a terminal, a script or a pre-commit hook

```bash
python3 $DD "pr :: DIFF"                              # the full JSON
python3 $DD --summary "risk,breaking_change :: DIFF"  # one line
python3 $DD --fail-on block "pr :: DIFF --staged"     # exits with 1 when a gate says block
python3 $DD --file request.json                       # a JSON request: batches, inline diffs, custom questions
```

A JSON request can carry a diff inline. That is handy in CI, where the checkout has no uncommitted changes:

```bash
git diff origin/main...HEAD > pr.diff
python3 -c 'import json; print(json.dumps({"questions": ["pr"], "diff": open("pr.diff").read()}))' > request.json
python3 $DD --fail-on block --file request.json
```

**As a git pre-commit hook**, use local mode so every commit stays instant and free. It still blocks on exact
findings such as a live API key in the staged diff. Save this as `.git/hooks/pre-commit` and make it executable:

```sh
#!/bin/sh
out=$(sh "$HOME/.claude/skills/dev-decide/scripts/run.sh" decide.py --fail-on block --summary "+local pr :: DIFF --staged")
if [ $? -eq 1 ]; then echo "dev-decide blocked this commit: $out"; exit 1; fi
```

`run.sh` finds Python 3.8+ as `python3`, `python` or `py -3`. Skip the hook once with `git commit --no-verify`.

### Step 9. Run the HTTP API on your computer

The API lets your own apps and scripts call dev-decide the way they would call Jev. Start it and leave it running
in a terminal:

```bash
python3 ~/.claude/skills/dev-decide/scripts/serve.py --port 8765 --cwd /path/to/your/repo
# dev-decide 5.3.0 listening on http://127.0.0.1:8765 (repo: /path/to/your/repo; no authentication; file references on)
```

Open http://127.0.0.1:8765/ in a browser for a web page with a live console, or use curl from a second terminal:

```bash
curl -s localhost:8765/health
# {"ok": true, "version": "5.3.0", ...}

curl -s localhost:8765/v1/decide -H 'Content-Type: application/json' \
  -d '{"state": "Login returns 500 for users with a plus sign in their email", "questions": ["triage"]}'

curl -s localhost:8765/v1/decide -H 'Content-Type: application/json' \
  -d '{"input": "DIFF", "questions": ["pr"]}'                # reads the repository's uncommitted changes

curl -s localhost:8765/v1/feedback -H 'Content-Type: application/json' \
  -d '{"id": "<the id from a decision>", "answers": {"issue_type": "bug"}}'

curl -s localhost:8765/v1/learning                          # what it has learned
```

Press Ctrl-C to stop it. It listens on 127.0.0.1 only, so nothing outside your computer can reach it. To require a
token locally too, start it with `DEV_DECIDE_TOKEN` set and send `-H "Authorization: Bearer <token>"`.

| Endpoint | What it does |
|---|---|
| `GET /health` | Returns `{"ok": true, "version": "5.3.0", ...}` with Claude availability, model-call use and the demo token if set. Never needs a token. |
| `POST /v1/decide` | `{"state": "...", "questions": [...]}`. Optional: `"diff"`, `"items"` (a batch), `"mode": "local"` or `"llm"`, `"why": true`. |
| `POST /v1/feedback` | `{"id": "<decision id>", "answers": {"key": "correct value"}}`, plus `"item"` for a batch item. |
| `GET /v1/learning` | The state of the learning loop. |

### Step 10. Set project policy (optional)

Copy the example and edit it:

```bash
cp ~/.claude/skills/dev-decide/examples/dev-decide.json /path/to/your/repo/.claude/dev-decide.json
```

The main keys are:
- `notes`: facts about the project.
- `critical_paths`: a minimum risk per path, such as `"src/payments/**": 4`.
- `thresholds`: minimum confidence per decision.
- `routes`: `extra`, `exclude` and `builtins`.
- `presets`, `bundles` and `gates`: your own questions and gates.
- `backend` and `model`.
- `learning`: for example `{"enabled": false}`.
- `escalate`: for example `false`, so hard cases are never sent to Sonnet.
- `log` and `capture`.

Environment variables override the policy:
- `DEV_DECIDE_BACKEND` and `DEV_DECIDE_MODEL`.
- `DEV_DECIDE_LEARNING=0`: learning off.
- `DEV_DECIDE_ESCALATE=0`: escalation off.
- `DEV_DECIDE_TOKEN`: the API token.

### Step 11. The routing hook (optional)

```bash
python3 ~/dev-decide/dev-decide-v5/install.py --router-hook
# for a project install, run this from the repository instead:
python3 ~/dev-decide/dev-decide-v5/install.py --project --router-hook
```

On each prompt a small local server (`hookd.py`, started by a session-start hook and reached through Claude
Code's HTTP hook) scores your agents and skills in about 5 ms. When one clearly matches, it adds a one-line hint
for Claude. It never blocks a prompt and never calls a model. `--router-hook=command` runs the same check as a new
process per prompt instead (about 70 ms), for older Claude Code versions. The hooks go into
`~/.claude/settings.json`, or `.claude/settings.local.json` for a project install, and that file is backed up
first. Restart Claude Code after installing it.

The same server keeps the **decision graph**: for each prompt, what decided, which agent or skill it suggested, and
(from a `PostToolUse` hook on the Agent and Skill tools, a `SubagentStop` hook and a `Stop` hook) who did the work:
the agent or skill Claude ran, with its sub-task and run time, or Claude itself with the tools it used. It also **learns** from that: after Claude runs an agent following a prompt, similar requests in that
project get that agent as their hint (it keeps the prompt's meaning vector, never its text), and a hint Claude keeps
ignoring needs more confidence. Open
`http://127.0.0.1:47321/graph` while Claude Code runs, or install the side panel with
`/plugin install dev-decide-graph@dev-decide` and type `/dev-decide-graph` (Claude Code 2.1.310 or later). It is
kept on your computer only, in `~/.claude/dev-decide/traces/`, for 30 days, with the first 40 characters of each
prompt (keys and tokens masked). In `.claude/dev-decide.json`, `"decision_graph": {"prompt_chars": 0}` keeps no
prompt text and `"decision_graph": {"enabled": false}` keeps nothing.

### Step 12. Update or uninstall

To update, run `git pull` in your clone and run `install.py` the same way. Learned data is kept.

To uninstall:

```bash
python3 ~/dev-decide/dev-decide-v5/install.py --uninstall
# for a project install, run this from that repository:
python3 ~/dev-decide/dev-decide-v5/install.py --project --uninstall
```

This removes the skill and the hook. It leaves `<repo>/.claude/dev-decide/` (logs, labels, models) in place;
delete that folder yourself if you want it gone.

---

## Part 2: Deploy it live

### What gets deployed

Only the HTTP API (`serve.py`), in a small Docker image: python:3.12-slim, the engine and the meaning model. The
Claude Code skill and the routing hook stay on your computer. On the server:

- **A token protects it.** Every endpoint except `/health` needs `Authorization: Bearer <token>`. `serve.py`
  refuses to listen beyond localhost without one.
- **It never reads the server's files for a caller.** When it listens beyond localhost, `DIFF`, `COMMIT` and
  `@path` are not read. Callers send a pull request's diff in `"diff"` instead, which gives the same answers as
  `DIFF` does locally.
- **Claude needs an API key there.** A server can't use your Claude Code sign-in, so the model tier needs
  `ANTHROPIC_API_KEY`, stored as a platform secret. Without a key the server answers from its local tiers only.
- **Learning needs a disk that survives restarts.** Everything it learns lives in `.claude/dev-decide/`. Only
  Option A keeps that folder across restarts. On B and C it starts again from your image every time.

### Deploy straight from the GitHub repository

The repository is already set up to deploy: a `Dockerfile` at its root builds the server from the one copy of the
engine (`dev-decide-v5/skill`), and `dev-decide-server/repo/` holds the server's own project (its policy in
`.claude/dev-decide.json` and its models). On **Render**:

| Field | Value |
|---|---|
| Repository | your fork or copy of `dev-decide`, branch `main` |
| Language | **Docker** |
| Root Directory | leave **empty** (the Dockerfile is at the repository root) |
| Instance type | **Free** |
| Health Check Path | `/health` |
| Environment | `DEV_DECIDE_TOKEN` (required, 32 random characters), `ANTHROPIC_API_KEY` (optional), and the optional ones below |
| Auto-Deploy (Settings) | **After CI Checks Pass**, so a push deploys only when the GitHub Actions tests are green |

When it is live, open the service URL in a browser: `/` is a web page with a live console, and `/health` reports
the version, whether Claude is available (`"model": "api"` or `"none"`), today's model-call use and, if you set
one, the public demo token. Every other endpoint needs a token.

Optional environment variables:

| Variable | What it does |
|---|---|
| `DEV_DECIDE_DEMO_TOKEN` | A second, public token for a demo (16+ characters, different from `DEV_DECIDE_TOKEN`). The web page fills it in for visitors. It gets local answers only, unless the visitor adds their own Anthropic key; nothing about demo requests is stored or learned from, and feedback is refused. |
| `DEV_DECIDE_LOCAL_ONLY=1` | Never use the server's own Anthropic key: every request is answered locally (visitors' own keys still work). |
| `DEV_DECIDE_STATE_REPO`, `DEV_DECIDE_STATE_KEY_B64` | Keep what the server learns across restarts, in a private git repository (see below). |
| `DEV_DECIDE_LEARNING=0` | Turn learning off. Leave it unset to learn from your own requests and feedback. |

**Visitors' own keys.** A request may carry `X-Anthropic-Api-Key: sk-ant-...` (the web page has a field for it).
That key pays for that request's model calls only: it is never stored, logged or cached, and those calls do not
count against the server's daily cap.

**Keeping what it learns.** Free hosting wipes the disk on every restart, sleep and deploy. To keep the learning:
create a private repository (for example `you/dev-decide-state`), make an SSH key pair
(`ssh-keygen -t ed25519 -N "" -f dd_state_key`), add the public key to that repository as a deploy key with write
access (Settings → Deploy keys), and set `DEV_DECIDE_STATE_REPO=git@github.com:you/dev-decide-state.git` and
`DEV_DECIDE_STATE_KEY_B64` to the private key encoded on one line (`base64 -w0 dd_state_key`). The server restores
the saved state when it starts, saves every 10 minutes when something changed, and once more when it stops. It
saves models and hashed labels, never input text. The repository keeps a single commit.

**GitHub Actions.** `.github/workflows/dev-decide-triage.yml` labels each new issue and leaves one comment;
`.github/workflows/dev-decide-pr.yml` rates each pull request, keeps one comment up to date and fails the check
when the gate says block. Add two repository secrets: `DD_URL` (the service URL) and `DD_TOKEN` (your token).

The server limits itself so a leaked token cannot run up a bill. Change the limits with environment variables:

| Variable | Default | What it does |
|---|---|---|
| `DEV_DECIDE_RATE_PER_MIN` | `30` | requests per minute from one client address; `0` turns it off |
| `DEV_DECIDE_MODEL_CALLS_PER_DAY` | `300` | Claude calls per UTC day; past it, requests are answered locally; `0` = no cap |
| `DEV_DECIDE_TRUST_PROXY` | `1` in the image | read the client address from the platform proxy's `X-Forwarded-For` |

The limits are per server process, so they reset when the service restarts or wakes from sleep. Set a spend limit on
the API key too (see "Keep it safe and cheap").

The rest of Part 2 builds a separate server folder by hand. Use it for Oracle Cloud (Option A), or when you don't
want to deploy from this repository.

### Choose a platform

Three free platforms fit a small Python web service. ★ marks the best fit for dev-decide.

| | **A. Oracle Cloud Always Free ★ best** | **B. Render (free web service)** | **C. Koyeb (free instance)** |
|---|---|---|---|
| What you get | An Arm VM: up to 2 OCPUs and 12 GB RAM in total (Ampere A1), 200 GB block storage, 10 TB/month outbound | One web service with 512 MB RAM and 0.1 CPU; 750 free instance-hours a month per workspace | One web service: 512 MB RAM, 0.1 vCPU, 2 GB SSD, in Frankfurt or Washington, D.C. |
| Always on | Yes (but see the idle rule in A9) | No: sleeps after 15 minutes without traffic; waking takes about a minute | No: sleeps after 1 hour without traffic; waking takes 1–5 seconds |
| Keeps what it learns | **Yes** (a Docker volume on the VM's disk) | No: the disk is wiped on every restart, deploy and sleep | No: the free instance has no volumes |
| HTTPS | Caddy (open source) gets certificates automatically | Built in (`onrender.com`) | Built in (`koyeb.app`) |
| Card needed | Yes, for verification (no prepaid cards) | No | Yes (Koyeb places a temporary $29 hold) |
| Setup time | About 45–60 minutes | About 15 minutes | About 15 minutes |
| Best for | A team or CI server that should keep learning | Trying it live; occasional calls | Occasional calls that should wake fast |

**Why A is best:** it is the only option that is always on and keeps what dev-decide learns, which is the point of
5.0. It also has far more CPU than the 0.1-CPU free containers, and everything on it is open source (Ubuntu,
Docker, Caddy). The catch is a longer setup and a card for sign-up.

**Not included:**
- Hugging Face Docker Spaces: creating one needs a paid plan since July 2026.
- Fly.io: no free tier any more, only a short trial.
- Railway: its free plan is a small monthly credit, not a free service.

**Already have a Linux server with a public IP?** Option A from step A4 onwards works on any Ubuntu server.

### Prepare the server folder

Do this once, on your computer. Every option deploys this same folder.

**1. Create the folder and copy in the engine.**

```bash
mkdir -p ~/dev-decide-server/repo/.claude
cd ~/dev-decide-server
cp -R ~/dev-decide/dev-decide-v5/skill ./skill
```

`repo/` is the project the server decides for: its policy, its agents and skills (for `route`), and its models.

**2. Add your policy** (optional). Copy your project's `.claude/dev-decide.json` to `repo/.claude/dev-decide.json`,
or create one. For routing, the server must know your agents and skills. Either copy their files:

```bash
cp -R /path/to/your/repo/.claude/agents repo/.claude/agents    # agent files
cp -R /path/to/your/repo/.claude/skills repo/.claude/skills    # skill folders (their SKILL.md files are read)
rm -rf repo/.claude/skills/dev-decide                          # if it was there: the server has its own copy
```

or list them under `routes` in `repo/.claude/dev-decide.json`, which is often simpler on a server:

```json
{
  "routes": {
    "builtins": true,
    "extra": [
      {"name": "db-migrate", "kind": "skill", "description": "Database schema changes, migrations and rollbacks"},
      {"name": "frontend-dev", "kind": "agent", "description": "UI pages, components, styling and accessibility"}
    ]
  }
}
```

**3. Add starting models** (recommended). Use the kit's pretrained models:

```bash
python3 ~/dev-decide/dev-decide-kit/kit.py export \
    --data ~/dev-decide/dev-decide-kit/pretrained --repo ./repo \
    --only risk,breaking_change,issue_type,severity,domain
```

It may add a note that no dev-decide install was found. That's expected here: the server runs its own copy from
`skill/`. Or copy the models your own repository has learned:

```bash
mkdir -p repo/.claude/dev-decide
cp -R /path/to/your/repo/.claude/dev-decide/models repo/.claude/dev-decide/models
```

Then delete the folder's ignore file. dev-decide writes it so that learned data stays out of your code
repositories, but in this deploy folder you want the models committed (Options B and C build from git):

```bash
rm -f repo/.claude/dev-decide/.gitignore
```

**4. Create `Dockerfile`** in `~/dev-decide-server`:

```dockerfile
# dev-decide HTTP API: Python standard library only, plus the bundled 30 MB meaning model.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --chown=app:app skill/ skill/
COPY --chown=app:app repo/ repo/
# The state folder must exist and belong to the app user, so a volume mounted on it is writable.
RUN mkdir -p repo/.claude/dev-decide && chown -R app:app repo
USER app
RUN python3 -m compileall -q skill/scripts

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8765'), timeout=4)"
# Render and Koyeb set PORT; elsewhere it defaults to 8765. serve.py needs DEV_DECIDE_TOKEN to listen on 0.0.0.0.
CMD ["sh", "-c", "exec python3 skill/scripts/serve.py --host 0.0.0.0 --port \"${PORT:-8765}\" --cwd /app/repo"]
```

**5. Create `.dockerignore` and `.gitignore`.** They keep secrets, caches and runtime logs out of the image and out
of git:

```bash
cat > .dockerignore <<'EOF'
.env
.git
**/__pycache__
**/*.pyc
repo/.claude/dev-decide/cache
repo/.claude/dev-decide/learn
repo/.claude/dev-decide/*.jsonl
repo/.claude/dev-decide/last.json
EOF
cat > .gitignore <<'EOF'
.env
__pycache__/
*.pyc
repo/.claude/dev-decide/cache/
repo/.claude/dev-decide/learn/
repo/.claude/dev-decide/*.jsonl
repo/.claude/dev-decide/last.json
EOF
```

**6. Make a token.** Store it in a password manager. Anyone who has it can use your server and spend your API
credit:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

**7. Test the image on your computer** (optional, recommended). You need Docker. Docker Desktop is free for
personal use and small businesses; Podman is an open-source alternative that takes the same commands.

```bash
docker build -t dev-decide .
docker run --rm -p 127.0.0.1:8765:8765 -e DEV_DECIDE_TOKEN='<your token>' dev-decide
# dev-decide 5.3.0 listening on http://0.0.0.0:8765 (repo: /app/repo; token required; file references off)
```

In a second terminal:

```bash
export DD_URL=http://127.0.0.1:8765 DD_TOKEN='<your token>'
curl -s $DD_URL/health
curl -s $DD_URL/v1/decide -H "Authorization: Bearer $DD_TOKEN" -H 'Content-Type: application/json' \
  -d '{"state": "Login returns 500 for users with a plus sign in their email", "questions": ["issue_type"], "mode": "local"}'
curl -s -o /dev/null -w '%{http_code}\n' $DD_URL/v1/learning     # 401: no token, no access
```

Stop it with Ctrl-C. Your folder now looks like this:

```
dev-decide-server/
├── Dockerfile
├── .dockerignore
├── .gitignore
├── skill/              the engine and the meaning model
└── repo/.claude/       dev-decide.json (policy), agents/, skills/, dev-decide/models/
```

---

### Option A ★ Oracle Cloud Always Free (best)

You get an always-on Arm VM that runs dev-decide and Caddy with Docker Compose. Caddy handles HTTPS.

**A1. Create the account.** Go to [oracle.com/cloud/free](https://www.oracle.com/cloud/free/) and choose **Start for
free**. A credit or debit card is needed for verification; prepaid cards are not accepted. Choose your **home
region** carefully: you can't change it later, and Always Free resources exist only there. Pick the region closest
to where the API will be called from.

**A2. Create the VM.** In the Console, go to **Compute → Instances → Create instance**:
1. **Image:** Canonical Ubuntu 24.04 (or a newer LTS, if offered).
2. **Shape:** Ampere → `VM.Standard.A1.Flex`, with 1 OCPU and 6 GB of memory. That is plenty. Always Free covers
   up to 2 OCPUs and 12 GB in total (older tutorials say 4 and 24; Oracle halved it in June 2026). Look for the
   "Always Free-eligible" label.
3. **Networking:** create a new virtual cloud network with a public subnet, and assign a public IPv4 address.
4. **SSH keys:** let Oracle generate a key pair and download the private key, or paste your own public key.
5. Keep the default boot volume, which fits within the free 200 GB.
6. Click **Create** and note the instance's **public IP address**.

If you get "Out of host capacity", Always Free Arm capacity in your region is temporarily full. Try another
availability domain, try again later, or pick the AMD shape `VM.Standard.E2.1.Micro` (its 1 GB of RAM is enough
for dev-decide).

**A3. Open ports 80 and 443.** Two firewalls need to allow them.
1. **Security list (Console).** Go to **Networking → Virtual cloud networks →** your VCN → your public subnet →
   **Default Security List → Add Ingress Rules**. Set Source CIDR `0.0.0.0/0`, IP Protocol TCP and Destination
   Port Range `80`. Add a second rule the same way for `443`.
2. **The VM's own firewall.** Oracle's Ubuntu images only allow SSH. Connect and open the ports:

```bash
# On your computer: connect to the VM
chmod 600 ~/Downloads/ssh-key.key                      # the private key you downloaded
ssh -i ~/Downloads/ssh-key.key ubuntu@<public-ip>

# Now on the VM:
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

**A4. Install Docker** on the VM, from Docker's own repository:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
exit                                                    # log out, then ssh in again so the group applies
```

After you log back in, `docker compose version` should print a version.

**A5. Pick a hostname.** HTTPS certificates need a name that points at the VM.
- **Your own domain:** add an `A` record, for example `dd.example.com → <public-ip>`.
- **No domain:** use sslip.io, a free DNS service that needs no sign-up. Write the IP with dashes instead of dots:
  `140-238-12-34.sslip.io` for `140.238.12.34`. Let's Encrypt issues certificates for these names. They share
  rate limits with everyone who uses them, so a request is occasionally refused; then try again later, or use
  `nip.io` the same way.

**A6. Copy the server folder to the VM.** Run this on your computer:

```bash
scp -i ~/Downloads/ssh-key.key -r ~/dev-decide-server ubuntu@<public-ip>:~/
```

**A7. Add the Compose file, the Caddyfile and the secrets.** Run this on the VM:

```bash
cd ~/dev-decide-server
cat > docker-compose.yml <<'EOF'
services:
  dev-decide:
    build: .
    image: dev-decide:latest
    restart: unless-stopped
    env_file: .env
    volumes:
      - dd-state:/app/repo/.claude/dev-decide      # what it learns survives restarts and rebuilds
  caddy:
    image: caddy:2.11
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./caddy:/etc/caddy:ro
      - caddy-data:/data
      - caddy-config:/config
    depends_on:
      - dev-decide
volumes:
  dd-state:
  caddy-data:
  caddy-config:
EOF

DD_HOST=140-238-12-34.sslip.io        # your hostname from A5
mkdir -p caddy
printf '%s {\n\treverse_proxy dev-decide:8765\n}\n' "$DD_HOST" > caddy/Caddyfile
cat caddy/Caddyfile

nano .env    # type the two lines below, then save with Ctrl-O, Enter, Ctrl-X
chmod 600 .env
```

Put this in `.env` (leave the key empty for a server that answers locally only):

```
DEV_DECIDE_TOKEN=<your token>
ANTHROPIC_API_KEY=<your API key>
```

Type the secrets into the editor rather than with `echo`, so they don't end up in your shell history.

**A8. Start it.**

```bash
docker compose up -d --build
docker compose ps                  # both running; dev-decide turns "healthy" within about 30 s
docker compose logs caddy | grep -i "certificate obtained"
```

From your computer:

```bash
export DD_URL=https://<your hostname> DD_TOKEN='<your token>'
curl -s $DD_URL/health                                 # {"ok": true, "version": "5.3.0", ...}
```

Both containers start again by themselves after a crash or a reboot.

**A9. Know Oracle's idle rule.** Oracle may reclaim (stop) an Always Free instance that looks idle for 7 days:
CPU (95th percentile), network and, on Arm shapes, memory all under 20%. A quietly used dev-decide server will
look idle. There are two ways to handle it:
- **Upgrade the account to Pay As You Go.** Oracle has told free-tier users that this keeps idle instances from
  being stopped, and Always Free resources stay free. A card on file makes paid shapes possible, so first set a
  small budget alert under **Billing & Cost Management → Budgets**, and keep to the free limits (2 OCPUs and
  12 GB on Arm).
- **Accept the occasional stop.** The disk, and everything dev-decide has learned, is kept. Start the instance
  again from the Console and both containers come back by themselves.

**A10. Back up, update and restore.**

```bash
cd ~/dev-decide-server
# Back up what it has learned (models, labels, memory):
docker compose exec -T dev-decide tar czf - -C /app/repo/.claude dev-decide > dd-backup-$(date +%F).tgz

# Update: copy a newer skill/ (or an edited policy) into ~/dev-decide-server, then rebuild. Learned state is kept.
docker compose up -d --build

# Restore a backup:
docker compose exec -T dev-decide tar xzf - -C /app/repo/.claude < dd-backup-YYYY-MM-DD.tgz
docker compose restart dev-decide

# Push new starting models from repo/ into the running server (replaces those model files):
tar czf - -C repo/.claude/dev-decide models | docker compose exec -T dev-decide tar xzf - -C /app/repo/.claude/dev-decide
docker compose restart dev-decide
```

Oracle also gives you five free volume backups, under the instance's **Boot volume → Backups**.

To stop the service, run `docker compose down`; the volumes are kept. To remove everything, terminate the instance.

---

### Option B: Render (free web service)

Render builds the same Dockerfile from a GitHub repository and serves it over HTTPS. It needs no card. Free web
services are meant for testing and hobby projects.

**B1. Put the server folder on GitHub,** in a **private** repository. Create an empty one at
[github.com/new](https://github.com/new), then run:

```bash
cd ~/dev-decide-server
git init -b main
git add .
git status            # check: the models under repo/.claude/dev-decide/models are listed; no .env, no API keys
git commit -m "dev-decide server"
git remote add origin https://github.com/<you>/dev-decide-server.git
git push -u origin main
```

If `git status` shows no models, you skipped the `rm -f repo/.claude/dev-decide/.gitignore` at the end of step 3
of "Prepare the server folder". The 30 MB model file is within GitHub's 100 MB per-file limit.

**B2. Create the service.** Sign up at [render.com](https://render.com) with GitHub. Then choose **New → Web
Service**, connect your repository, and set these fields:

| Field | Value |
|---|---|
| Language | **Docker** |
| Branch | `main` |
| Region | The one closest to your callers |
| Instance type | **Free** |
| Environment variables | `DEV_DECIDE_TOKEN` = your token; `ANTHROPIC_API_KEY` = your key (optional); `DEV_DECIDE_LEARNING` = `0` |
| Advanced → Health Check Path | `/health` |

`DEV_DECIDE_LEARNING=0` is recommended here. The free disk is wiped whenever the service restarts or sleeps, so
anything it learned would be lost anyway, along with the spot checks that paid for it. Render sets `PORT` (10000 by
default), and the image listens on it.

**B3. Deploy and test.** Click **Create Web Service**. When the log says the service is live, copy the
`onrender.com` URL from the top of the page:

```bash
export DD_URL=https://<your-service>.onrender.com DD_TOKEN='<your token>'
curl -s $DD_URL/health
```

**Good to know**
- **Sleeping.** After 15 minutes without traffic the service sleeps. The next request waits about a minute while it
  wakes, so give clients a timeout of at least 90 seconds, or call `/health` first.
- **Free hours.** You get 750 free instance-hours a month per workspace. A service awake all month uses about 744,
  and sleeping time doesn't count.
- **Bandwidth.** The free (Hobby) workspace includes 5 GB of outbound bandwidth a month. dev-decide's answers are
  1–3 KB each, so that covers more than a million decisions. Without a payment method, going over suspends the
  service until the next month.
- **Updates.** Push to `main` and Render redeploys. To bring what you learned locally to the server, copy your
  repository's `.claude/dev-decide/models/` into `repo/.claude/dev-decide/models/`, then commit and push.

---

### Option C: Koyeb (free instance)

Koyeb also builds your Dockerfile from GitHub. It wakes from sleep in seconds instead of a minute, but it needs a
card.

**C1.** Put the server folder in a private GitHub repository, as in **B1**.

**C2. Sign up** at [koyeb.com](https://www.koyeb.com) and add a card. Koyeb requires one to prevent abuse, and
places a temporary $29 hold to verify it.

**C3. Store the secrets.** Open **Secrets** in the top bar. Create `DEV_DECIDE_TOKEN`, and optionally
`ANTHROPIC_API_KEY`.

**C4. Create the service.** Go to **Create Web Service → GitHub**, install Koyeb's GitHub app for your repository,
and set these fields:

| Field | Value |
|---|---|
| Repository and branch | Your repository, `main` |
| Builder | **Dockerfile** |
| Instance | **Free** (Frankfurt or Washington, D.C.) |
| Exposed ports | `8765`, HTTP, route `/` |
| Health checks | HTTP, path `/health`, port `8765` |
| Environment variables | `DEV_DECIDE_TOKEN` = `{{ secret.DEV_DECIDE_TOKEN }}`; `ANTHROPIC_API_KEY` = `{{ secret.ANTHROPIC_API_KEY }}`; `DEV_DECIDE_LEARNING` = `0` |

Click **Deploy**. Koyeb sets `PORT` to the exposed port, and the image listens on it.

**C5. Test it.** Copy the public URL (ending in `koyeb.app`) from the service page:

```bash
export DD_URL=https://<your-app>.koyeb.app DD_TOKEN='<your token>'
curl -s $DD_URL/health
```

**Good to know**
- **Sleeping.** After 1 hour without traffic the free instance scales to zero; you can't turn this off. The next
  request wakes it in 1–5 seconds. Koyeb says HTTP/2 requests can't wake a sleeping service, so if a wake-up
  request fails, retry it over HTTP/1.1 (`curl --http1.1`).
- **Updates.** Push to `main`; Koyeb rebuilds when autodeploy is on.

---

### Use your live API

The same requests work on every option. Set `DD_URL` and `DD_TOKEN` as in the test steps above.

```bash
# Triage an issue
curl -s $DD_URL/v1/decide -H "Authorization: Bearer $DD_TOKEN" -H 'Content-Type: application/json' \
  -d '{"state": "Payment page shows the wrong currency symbol for EUR", "questions": ["triage"]}'

# Check a pull request: send the diff itself
git diff origin/main...HEAD \
  | python3 -c 'import json, sys; print(json.dumps({"questions": ["pr"], "diff": sys.stdin.read()}))' \
  | curl -s $DD_URL/v1/decide -H "Authorization: Bearer $DD_TOKEN" -H 'Content-Type: application/json' --data-binary @-

# Correct an answer. Use the decision's "id": on a shared server, "last" may be someone else's decision.
curl -s $DD_URL/v1/feedback -H "Authorization: Bearer $DD_TOKEN" -H 'Content-Type: application/json' \
  -d '{"id": "<decision id>", "answers": {"issue_type": "bug"}}'

# What it has learned
curl -s $DD_URL/v1/learning -H "Authorization: Bearer $DD_TOKEN"
```

From Python, with no packages needed:

```python
import json, os, urllib.request

def decide(spec):
    req = urllib.request.Request(os.environ["DD_URL"] + "/v1/decide", data=json.dumps(spec).encode(),
                                 headers={"Authorization": "Bearer " + os.environ["DD_TOKEN"],
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)

out = decide({"state": "Login returns 500 for users with a plus sign in their email", "questions": ["issue_type"]})
print(out["summary"])
```

**A pull-request gate in GitHub Actions.** Add `DD_URL` and `DD_TOKEN` as repository secrets (**Settings → Secrets
and variables → Actions**), then commit this as `.github/workflows/dev-decide.yml`:

```yaml
name: dev-decide
on: pull_request
permissions:
  contents: read
jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
      - name: Ask dev-decide about this pull request
        env:
          DD_URL: ${{ secrets.DD_URL }}
          DD_TOKEN: ${{ secrets.DD_TOKEN }}
          BASE: ${{ github.base_ref }}
        run: |
          git diff "origin/$BASE...HEAD" > pr.diff
          python3 - <<'EOF'
          import json, os, sys, urllib.request
          if not os.environ.get("DD_URL"):
              print("DD_URL is not set (pull requests from forks get no secrets); skipping")
              sys.exit(0)
          body = json.dumps({"questions": ["pr"], "diff": open("pr.diff").read()}).encode()
          req = urllib.request.Request(os.environ["DD_URL"] + "/v1/decide", data=body, headers={
              "Authorization": "Bearer " + os.environ["DD_TOKEN"], "Content-Type": "application/json"})
          out = json.load(urllib.request.urlopen(req, timeout=180))
          print(out.get("summary"))
          verdicts = [g.get("verdict") for g in (out.get("gates") or {}).values()]
          sys.exit(1 if "block" in verdicts else 0)
          EOF
```

The job fails only when a gate says `block`. The timeout of 180 seconds leaves room for a sleeping Render service
to wake up.

### Keep it safe and cheap

**Security checklist**
- **The token.** Use 32 random characters (16 is the minimum `serve.py` accepts). Keep it only in platform
  secrets, `.env` (mode 600) and your password manager, never in git. To rotate it, change the secret and restart
  or redeploy.
- **The API key.** Give the server its own key with its own spending cap. In the Claude Console, go to
  **Workspaces** (`platform.claude.com/settings/workspaces`), create a workspace (for example `dev-decide-server`),
  create the server's API key in that workspace, then select the workspace and set a monthly cap under **Spend
  limits**. The Default workspace can't have a spend limit. An organization-wide cap is on the **Billing** page.
  Anyone with your token can make the server call Claude on your bill, and the cap limits what that can cost.
- **Keep the safeguards on.** Don't pass `--allow-no-auth` unless your own proxy authenticates every request, and
  don't pass `--allow-file-refs` on a public server.
- **Privacy.** Inputs that reach the model tier are sent to Anthropic. The local tiers send nothing anywhere. The
  server's request log records only the method, the path and the status.

**Cost controls**
- **Spot checks.** With learning on, they are capped at 20 a day.
- **Escalations to Sonnet** are capped at 30 a day. Set `DEV_DECIDE_ESCALATE=0` to turn them off.
- **On Render and Koyeb those caps reset** whenever the container restarts. That's another reason to run them with
  learning off.
- **Per client and per day.** `DEV_DECIDE_RATE_PER_MIN` (default 30) and `DEV_DECIDE_MODEL_CALLS_PER_DAY` (default
  300) limit requests and Claude calls; see "Deploy straight from the GitHub repository".
- **Per request.** Send `"mode": "local"` and it never calls a model.
- **A server with no model calls at all.** Set `DEV_DECIDE_BACKEND=none`, or leave the key out.

**Disk use (Option A).** Two logs grow by a few KB per decision: `log.jsonl` and `features.jsonl`, in the state
volume. Only the older batch tools read them, not the learning loop, so you can archive or delete them whenever
you like. Your backups (A10) include them.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| The container exits with "refusing to listen on 0.0.0.0 without a token" | `DEV_DECIDE_TOKEN` is not set where the container runs. Set it as a platform secret or in `.env`. |
| "DEV_DECIDE_TOKEN is too short" | Make a new one with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `401 unauthorized` | Send `Authorization: Bearer <token>` (or `X-Dev-Decide-Token`). On Koyeb, check that the variable's value is `{{ secret.DEV_DECIDE_TOKEN }}`. |
| Warning "no model backend available" | No `ANTHROPIC_API_KEY` on the server (or no backend on your computer, Step 4). That's fine for local-only answers; add a key to get model answers. |
| Warning with "API error 401" | The API key is wrong or revoked. |
| Warning with "API error 400" and "reached your specified … API usage limits" | The spend limit you set in the Claude Console was reached. Raise it, or wait for the next month. |
| "DIFF and COMMIT are not read on this server" | Send the diff itself in `"diff"` (see "Use your live API"). |
| `route` answers don't match your agents | The server doesn't know them. Copy `.claude/agents` and `.claude/skills`, or list them under `routes.extra` (step 2 of "Prepare the server folder"), then redeploy. |
| `/v1/feedback` says "learning is turned off" | Expected with `DEV_DECIDE_LEARNING=0` (B and C). Corrections only stick on a server that keeps its state (A). |
| First request takes a minute (Render) or a few seconds (Koyeb) | The service was asleep. Call `/health` first, or use longer client timeouts. |
| Render or Koyeb answers less locally than your Docker test did | The models didn't reach git. Check `git ls-files repo/.claude/dev-decide/models`; see B1. |
| Render deploy fails with no open port detected | Check the log for the "listening on" line. The CMD must keep `--port "${PORT:-8765}"`. |
| Caddy has no certificate (Option A) | Ports 80 and 443 must be open in both firewalls (A3), and the hostname must resolve to the VM's IP. Run `docker compose logs caddy`. |
| "Out of host capacity" (Oracle) | Try another availability domain, try again later, or use `VM.Standard.E2.1.Micro`. |
| `docker build` fails with "429 Too Many Requests" | Docker Hub is limiting anonymous pulls. Wait, run `docker login`, or change the first line to `FROM mirror.gcr.io/library/python:3.12-slim` (the same image from Google's mirror). |
| The server answers but learns nothing (Option A) | `docker compose exec dev-decide ls /app/repo/.claude/dev-decide` should list `learn/` after some decisions. Check that `DEV_DECIDE_LEARNING` isn't set to `0` in `.env`. |

---

## Sources

Platform limits and steps:
- Oracle Cloud, [Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm)
- InfoQ, [Oracle's 2026 change to the Ampere A1 free allowance](https://infoq.com/news/2026/07/oracle-cloud-free-tier-limits/)
- Oracle tutorial, [Install Apache and PHP on an Ubuntu instance](https://docs.oracle.com/en-us/iaas/Content/developer/apache-on-ubuntu/01oci-ubuntu-apache-summary.htm) (security list and iptables steps)
- NetSec, [Oracle's idle-instance notice and the Pay As You Go exemption](https://blog.51sec.org/2023/02/oracle-cloud-cleaning-up-idle-compute.html)
- Render, [Deploy for Free](https://render.com/docs/free), [Web Services](https://render.com/docs/web-services) (port binding), [Docker on Render](https://render.com/docs/docker) and [Pricing](https://render.com/pricing)
- J. Watte, [Render's 2026 plan change](https://jwatte.com/blog/render-com-platform-review.md)
- Koyeb, [Pricing FAQ](https://www.koyeb.com/docs/faq/pricing), [Scale-to-zero](https://www.koyeb.com/docs/run-and-scale/scale-to-zero), [Deploy with Git](https://koyeb.com/docs/quickstart/deploy-with-git), [Environment variables](https://www.koyeb.com/docs/build-and-deploy/environment-variables) and [Secrets](https://www.koyeb.com/docs/secrets)
- Hugging Face, [Spaces overview](https://huggingface.co/docs/hub/spaces-overview) (Docker Spaces need a paid plan), and [twikoo PR #1204](https://github.com/twikoojs/twikoo/pull/1204) (when that changed)
- SnapDeploy, [State of free hosting](https://snapdeploy.dev/state-of-free-hosting): a dated comparison, published by one of the hosts it compares
- [nip.io and sslip.io](https://nip.io/) (hostnames for IP addresses, with Let's Encrypt)

Tools:
- Docker, [Install Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- Caddy, [Running Caddy with Docker Compose](https://caddyserver.com/docs/running)
- GitHub, [actions/checkout releases](https://github.com/actions/checkout/releases)
- Claude docs, [Rate limits and spend limits](https://platform.claude.com/docs/en/api/rate-limits) and [Workspaces](https://platform.claude.com/docs/en/manage-claude/workspaces)
