# dev-decide 5.2 + dev-decide-kit

dev-decide is a Jev-style decision engine for software and AI work: a Claude Code skill, a command-line tool,
an HTTP API and an opt-in routing hook. It answers typed questions (route this request, triage this issue, rate
this diff) locally in milliseconds when it can, asks Claude when it can't, and learns from every answer.

## A Jev-style decision engine, open source and on your machine

[Jev](https://www.edenai.co/post/jev-a-new-kind-of-ai-model-built-for-decisions-not-conversation) (TypeSafe AI,
early access since 15 September 2026) made a simple idea popular: an agent's decisions should come from a **decision
model** that returns a typed answer with a confidence, not from a chatbot's prose, and your own code decides what to
do with that confidence. dev-decide applies the same idea to software work, as a free, open-source engine you run
yourself:

| | Jev | dev-decide |
|---|---|---|
| Question types | Choice, Score, yes/no | `choose`, `score`, `verify` (yes/no with a probability) |
| Answer | the typed value, probabilities, a confidence | the same, plus where it came from (rule, learned model, memory, Claude) |
| Thresholds | in your code | in your code and in `.claude/dev-decide.json` gates (pass / review / block) |
| Where it runs | TypeSafe's hosted service | your machine (Python standard library); Claude only for what stays unsure |
| Speed | 70-500 ms reported, over the network | about 1 ms for a local answer ([measured](dev-decide-kit/README.md)) |
| Learns from you | not described | yes: every answer Claude gives trains the local models, which switch only after proving better |
| Built for | any closed question | software work: routing to agents and skills, triage, pull-request and deploy gates, AI-change checks |
| Licence | proprietary | MIT |

Jev is one general model; dev-decide's local tier is many narrow models, one per decision, so compare accuracy only
on your own decisions. Jev is a decision model made by TypeSafe AI. dev-decide is an independent open-source project, not affiliated with or endorsed by TypeSafe AI.

## Install (as a Claude Code plugin)

In Claude Code:

```
/plugin marketplace add pawanptu/dev-decide
/plugin install dev-decide@dev-decide
```

Then use it in any repository, for example `/dev-decide pr :: DIFF` or `/dev-decide triage :: <paste an issue>`.

- **Your own key.** Nothing here uses anyone else's credentials. By default it asks Claude through your own Claude
  Code sign-in. To use the API instead, set `ANTHROPIC_API_KEY` in your shell. Never commit the key.
- **Other models.** Claude is the default, but the model tier also works with any OpenAI-compatible endpoint
  (OpenAI, Azure, Groq, Together, OpenRouter, Mistral, DeepSeek, or a local Ollama, LM Studio or vLLM), Gemini, or
  any model CLI such as Codex. See "Other models" in `dev-decide-v5/README.md`.
- **Other tools.** `mcp_server.py` serves dev-decide over MCP, so Cursor, Codex CLI, Gemini CLI, Windsurf and other
  MCP clients can call it. See "Use it from other tools (MCP)" in `dev-decide-v5/README.md`.
- **Needs** Python 3.8+ (found as `python3`, `python` or `py -3`) and git, and Git Bash or WSL on Windows.
- **Starter models are built in.** The first time it runs in a repository it copies in pretrained models
  (`skill/starter-models/`, 2.6 MB), so `issue_type` is answered locally from day one. The other models switch on by
  themselves once they have learned enough from your use. A repository that already has models is never overwritten.
- **One settings file you can edit.** The first time it runs in a git repository it writes `.claude/dev-decide.json`
  with every setting at its default and a `_help` section that explains each one (what to ask Claude with, how
  risky paths are rated, learning, the routing hook, your own questions and gates). Edit it and save; changes apply
  on the next run. Commit it to share settings with your team. It never overwrites a file you already have. Its
  values override your personal `~/.claude/dev-decide.json`, so delete a setting from the project file if you want
  your personal one to win.
- **Routing hint, in milliseconds.** On each prompt a small local server (started by a session-start hook, reached
  through Claude Code's HTTP hook) checks whether one of your agents or skills clearly matches, and only then adds a
  one-line hint: about 5 ms per prompt. It listens on 127.0.0.1 only, never calls a model, never blocks a prompt and
  writes nothing to your repository. To turn it off, put `{"router_hook": {"enabled": false}}` in
  `.claude/dev-decide.json`, or disable the plugin's hooks.
- **Decision graph.** Open `http://127.0.0.1:47321/graph` to see, per kind of task (bugfix · database, docs, ...),
  what decided (the trained route model, the zero-shot match or a `/dev-decide` question), which agent or skill was
  suggested, which one Claude really used afterwards, and how long each step took, with a timeline you can filter by
  clicking a node. `/plugin install dev-decide-graph@dev-decide` adds the same graph as a side panel
  (`/dev-decide-graph`, Claude Code 2.1.310+). It is kept on your machine only (`~/.claude/dev-decide/traces/`, 30
  days) with the first 40 characters of each prompt, keys and tokens masked; set `"decision_graph"` in
  `.claude/dev-decide.json` to keep less (`"prompt_chars": 0`) or nothing (`"enabled": false`).
- **Example agents.** `dev-decide-kit/example-agents/` holds eight Claude Code subagents (backend-reviewer,
  db-migrate, frontend-dev, debugger, security-auditor, test-writer, devops-deploy, docs-writer). Copy them into
  `~/.claude/agents/` and the shipped route model, trained on exactly these, answers routing locally (about 94%
  accurate on held-out requests). It is never used for other agents.

**Install from a clone instead** (for the HTTP API on your computer, or to choose whether to add the routing hook):

```
git clone https://github.com/pawanptu/dev-decide
python3 dev-decide/dev-decide-v5/install.py              # add --router-hook for the routing hint
```

**Web page and server.** `serve.py` also serves a web page with a live console at `/`: a public demo token for
visitors (local answers only), an optional field for your own Anthropic key (used for that request only, never
stored), examples, batch mode, history kept in the browser, and right/wrong buttons that teach the model. The root
`Dockerfile` builds the server; `setup_deploy.md` covers deploying it (Render takes about 10 minutes), keeping what it
learns across restarts in a private repository, and the GitHub Actions that label issues and gate pull requests
(`.github/workflows/dev-decide-*.yml`).

**Benchmark.** `python3 tools/benchmark.py` trains with calibration, evaluates and writes the results table (add
`--label-issues N --label-diffs M` to write and label new examples first). See `CHANGELOG.md` for what changed.

**Maintainers:** the engine lives in `dev-decide-v5/skill/`. After changing it, run `python3 tools/sync.py` to copy it
into `plugin/`, then `python3 tools/smoke.py` (CI runs the same checks on Linux and Windows, Python 3.8 and 3.12).

## What is in this repo

- **`dev-decide-v5/`**: the engine, with its bundled meaning model (30 MB, MIT). Install it in place of 3.x or
  4.x; your logs, models and labels are kept.
- **`plugin/`** and **`plugin-graph/`**: the Claude Code plugin (a copy of the engine made by `tools/sync.py`) and
  the decision graph side panel.
- **`dev-decide-kit/`**: optional. It bootstraps labeled data with Claude and trains starting models. It ships
  with pretrained models from a real run (821 examples, 89 Claude calls, $1.22), retrained for 5.0.
- **`setup_deploy.md`**: step-by-step setup on your computer, and deployment of the HTTP API on free hosting.

**New in 5.0.1: the HTTP API can be deployed.** It checks a token (`DEV_DECIDE_TOKEN`), refuses to listen beyond
localhost without one, never reads the server's own files for a remote caller, and takes a pull request's diff
inline (`"diff"`). Decisions are otherwise the same as 5.0. Don't put the 5.0 API on the internet: it has none of
these checks.

## What 5.0 adds

| | What it does | Measured |
|---|---|---|
| Understands meaning | 256 meaning features per input from a small embedding model, used where they measure better | routing 84.9% → 90.6% held out; domain 57.9% → 68.4% |
| Learns from each answer | Recalls inputs like ones already labeled, trusted only where its record reaches 90% | steady usage: 80% answered locally at 100% (4.0: 60%) |
| Tunes itself | Per-class thresholds follow spot checks; checks target borderline, unfamiliar and recalled answers; a class breaker; unfamiliar inputs go to Claude | with a convention change: 79% local at 96.6% (4.0: 57% at 97.8%) |
| Stronger model for hard cases | Unsure answers to decisions that matter are re-asked to Sonnet, at most 30 a day | live: Haiku 0.60 → Sonnet agreed at 0.62, $0.016, 7.6 s |

From 4.0, it still learns from Claude's answers, spot checks and your corrections, switches models only on
evidence, and can roll back. Nothing runs in the background and no code changes itself. With learning and
escalation off it answers exactly like 3.1 (99 of 99 test decisions).

**Speed.** Routing through the local HTTP API takes 4.1 ms and triage 3.2 ms: 17–150× faster than Jev's reported
70–500 ms. The command line and routing hook take 72–74 ms, mostly Python start-up.

**Honest limits.** Local answers agree with Claude plus your corrections, no more. After an abrupt change in how
a class should be labeled, 5.0 dips further than 4.0 for a while (87% vs 92% right over the next 180 decisions in
simulation), because more local answers mean fewer labels to notice the change from; one `fix` from you speeds
that up. Meaning features are English-first. The simulations used synthetic issues.

## Quick start

```bash
cd dev-decide-v5 && python3 install.py && cd ..          # 1. install (or --project for one repository)
pip install numpy scipy scikit-learn                     # 2. optional, for the kit: pretrained starting models
python3 dev-decide-kit/kit.py export --data dev-decide-kit/pretrained --repo /path/to/repo
```

`setup_deploy.md` has the full steps, including deployment. Then use it. In Claude Code: `/dev-decide route :: <task>`, `/dev-decide triage :: <issue>`,
`/dev-decide fix last issue_type=bug`, `/dev-decide status`. `dev-decide-v5/README.md` has the details
("5.0 measurements", "The learning loop"); `dev-decide-kit/README.md` covers training your own.

## Contributing

dev-decide is open source and contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md), the
[Code of Conduct](CODE_OF_CONDUCT.md) and [SECURITY.md](SECURITY.md) (report vulnerabilities privately).

## Licence

MIT. See `LICENSE`. The starter models and the example data in `dev-decide-kit/pretrained/` are released under
the same licence, so results can be reproduced with `python3 tools/benchmark.py`. The bundled meaning model
(potion-base-8M) is MIT licensed too; see its `NOTICE`. Each user's own learned state stays private to them.
