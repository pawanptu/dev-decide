# Changelog

## Unreleased

### Added
- **The routing hint learns what Claude really runs.** When Claude runs an agent or skill after a prompt, the hint
  server keeps the prompt's meaning vector (never its text) and the agent's name, per project, outside the
  repository (`~/.claude/dev-decide/traces/usage/`). A later request that means much the same, with at least 3
  similar past requests agreeing, gets that agent as its hint, ahead of the generic models. The bar (similarity
  0.55, 3 votes) was chosen on the kit's labeled routing requests: 87.5-100% of its hints right, answering more
  often as the memory grows. `"router_hook": {"learn": false}` turns it off.
- **Hints Claude keeps ignoring get stricter:** once Claude has used another agent after at least 5 hints for the
  same one, and followed fewer than 1 in 5, that hint needs 0.9 confidence in that project.
- **Built-in agents can be suggested** (`Explore`, `Plan`, `general-purpose`): `"builtins": true` is now the default.
  On the kit's held-out requests, hints rose from 87.0% to 89.7% of requests, and 4.2% of hints now name a
  built-in agent where the label names a specialist; wrong hints on held-out chat stay at 1 of 42.
- **Real agent run times:** a `SubagentStop` hook records how long each agent ran (from its transcript's first
  timestamp; nothing else is read). Before, the graph showed the Agent tool call's time, which for an agent
  started in the background is only its launch.
- Decision-log rows are audit records: engine version, a hash of the input, which model version answered, the
  threshold and the band (`next`). The escalate band's width is a setting (`"escalate": {"band": 0.15}`).
- Options reach the model in a shuffled order (the same order for the same input), since models lean toward
  options listed first.
- `tools/jev_bench.py`: downloads Banking77, TweetEval, HelpSteer2 and RAGTruth and tests the local tier on them
  next to the published Jev results. First result, Banking77 (77 intents, trained on its 8,502-example training
  split): 93.3% right, calibration error 0.040, 3 ms per decision (Jev, untrained: 78.3%, 0.096, about 840 ms).

### Fixed
- Messages no person typed (background-task notices, messages between agents) were routed and hinted like
  prompts and counted in the graph; they are now skipped, also in events recorded before this fix. HTML comments,
  paste wrappers and quote marks are no longer taken for the request.
- A project that held an older copy of the shipped route model kept using it, because the hint never updates a
  project; it now uses the shipped one, read only, unless the project trained its own.
- The decision graph page says when Claude last ran an agent and offers a period that shows it, instead of an
  empty chart for a short period.

### Added (earlier in this release)
- **Other models.** Three new backends next to Claude, which stays the default: `openai` (any OpenAI-compatible
  Chat Completions endpoint: OpenAI, Azure OpenAI's v1 endpoint, Groq, Together, OpenRouter, Mistral, DeepSeek, or
  a local Ollama, LM Studio or vLLM server), `gemini` (Google's Gemini API) and `command` (any model CLI, such as
  Codex CLI, with the prompt on stdin). Each takes its model from `model.<backend>` or `DEV_DECIDE_MODEL_<BACKEND>`
  and escalates only when a stronger model is named (`escalate.<backend>` or `DEV_DECIDE_STRONG_MODEL_<BACKEND>`).
  Newer OpenAI models that refuse `max_tokens` or a fixed temperature are retried with what they accept.
- **MCP server** (`scripts/mcp_server.py`, standard library only): `decide`, `correct` and `catalog` tools over
  stdio, for Cursor, Codex CLI, Gemini CLI, Windsurf, Claude Desktop and other MCP clients. `@path` inputs are
  refused, so an agent's tool call cannot read arbitrary files.
- **Bring your own OpenAI or Gemini key** to the HTTP API (`X-OpenAI-Api-Key`, `X-Gemini-Api-Key`), alongside
  `X-Anthropic-Api-Key`. The key reaches only the endpoint and model the server is set up with; `/health` lists
  which providers' keys it takes (`own_keys`), and the web console has a provider picker.

### Changed
- Described as an open-source, Jev-style decision engine: the README compares it with Jev (TypeSafe AI's decision
  model), and the plugin and marketplace listings, the GitHub topics and the web console say so, each with a note
  that dev-decide is not affiliated with TypeSafe AI.
- Documentation brought up to date for the decision graph and the side panel (`setup_deploy.md`, the engine
  README, `SECURITY.md`, `CONTRIBUTING.md`) and the example settings file.

### Security
- Where a key is sent (`openai.base_url`, `openai.key_env`, `gemini.base_url`) and which program runs (`command`)
  are read only from the environment or the personal `~/.claude/dev-decide.json`, never from a repository's
  `.claude/dev-decide.json`. A backend that is named but not set up answers nothing rather than falling back to
  another provider.

## 5.2.0 (25 October 2026)

### Added
- **Decision graph.** `http://127.0.0.1:47321/graph` shows, per kind of task (type · area), what decided (the
  trained route model, the zero-shot match or a `/dev-decide` question), which agent or skill was suggested, which
  one Claude really used afterwards (from a new `PostToolUse` hook on the Agent and Skill tools) and how long each
  step took, with a filterable timeline, a project picker, 1 hour to 30 days, and live refresh. The hint server
  records it after answering, so the hint is no slower.
- **`dev-decide-graph` plugin**: the same graph as a side panel, opened with `/dev-decide-graph` (a text tree in the
  terminal, the graph in the desktop app and VS Code). Needs Claude Code 2.1.310 or later.
- Settings: `"decision_graph": {"enabled": true, "keep_days": 30, "prompt_chars": 40}`. Records stay on the machine
  in `~/.claude/dev-decide/traces/`, never in the repository; of a prompt only the first `prompt_chars` characters
  are kept, keys and tokens masked. A server listening beyond localhost (`serve.py`) records nothing.

### Changed
- **Decision graph page redesigned:** a plain-English summary at the top, a "read it left to right" guide, stat
  tiles that count up with sparklines and meters, an animated step-by-step replay of the latest request, a flow graph
  with moving particles, hover tracing, tooltips, keyboard focus and a table view, leaderboards (agents and skills
  used, kinds of tasks, who decided and how fast), and a timeline with per-step timing bars, day groups and filters.
  Live refresh keeps the previous frame on screen and toasts each new request. Colors are the validated categorical
  slots (checked for color-blind separation in light and dark); reduced motion turns every animation off.
- **Side panel:** the SVG now carries the totals, the latest request, check and cross badges, and moving dots on
  the busiest links; the terminal shows a text tree with bars and check marks.
- **Web console rebuilt with Alpine.js** (its CSP build, vendored in `skill/vendor/`, so no eval and no CDN): an
  animated example decision in the hero, a three-step console (what to decide, your input, how to answer) with
  tiles, a staged progress view while it decides, a verdict banner, a "where the answers came from" bar, answers
  ordered by trust with growing confidence meters, history cards, "Try it" on every bundle, and install tabs.
  The page's script now comes from `/ui.js`, so its CSP is `script-src 'self'` (no inline script at all).

### Fixed
- The console offered Right/Wrong on rough guesses, which the server never logged, so saving one failed.
- On Windows a second hint server could bind the same port (Python's default `SO_REUSEADDR`), so two sessions
  starting together could split prompts between two servers. It now binds exclusively.

## 5.1.0 (25 October 2026)

### Added
- **Instant routing hint.** A small local server (`hookd.py`) answers Claude Code's HTTP hook in about 5 ms instead of
  starting Python on every prompt (about 300 ms on Windows). A SessionStart hook starts it when needed. The old
  per-prompt command hook remains available (`install.py --router-hook=command`).
- **Routing by meaning and area.** Without a trained route model, matching blends words, the bundled embedding model
  and technical areas (postgres, column, schema count as database). On held-out requests hint precision went from
  80.5% to 88.1%, and false hints with one unrelated skill from 19.5% to 3.2% (`tools/eval_router.py`).
- **Example agents and their route model.** Eight subagents in `dev-decide-kit/example-agents/` (backend-reviewer,
  db-migrate, frontend-dev, debugger, security-auditor, test-writer, devops-deploy, docs-writer). When they are
  installed, the shipped route model (trained on them) answers routing locally; it is never used for other agents.
- **Web page:** public demo token filled in for visitors, bring-your-own Anthropic key, one example per decision type,
  right/wrong feedback that the learning loop uses, batch mode (one input per line), history kept in the browser,
  and a warning when the daily Claude budget is nearly used.
- **Server:** `DEV_DECIDE_DEMO_TOKEN` (local answers only, nothing about demo requests is stored or learned),
  `X-Anthropic-Api-Key` (the caller's own key for that request only: never stored, logged or counted),
  `DEV_DECIDE_LOCAL_ONLY`, a per-client rate limit, a daily model-call cap with an 80% warning, security headers,
  `/v1/presets`, and richer `/health`.
- **Learning that survives restarts.** `statesync.py` saves what a server learned (models and hashed labels, never
  input text) to a private git repository with a deploy key, and restores it on start.
- **GitHub Actions.** `dev-decide-triage.yml` labels new issues and comments; `dev-decide-pr.yml` rates each pull
  request, keeps one comment up to date and fails the check on block.
- **Per-class confidence thresholds.** Training measures, per answer class, the confidence its answers need to reach
  the target; weak classes need more before they are answered locally.
- `tools/benchmark.py` (label, train with calibration, evaluate, write the results table), `tools/smoke.py`
  end-to-end checks, CI on Linux and Windows with Python 3.8 and 3.12.

### Changed
- Triage models retrained with 149 hand-labeled short reports (checked blind by Sonnet) and 772 new generated issues.
  `issue_type`: short reports never trained on went from 35% to 88% right (96% of its local answers right), 95.3% on
  the held-out test split. `severity` and `domain` are now enabled (local answers 94% and 100% right on examples
  neither run trained on). `breaking_change` is retrained on 647 diffs and now enabled (local answers 86% right on
  diffs neither run trained on); the retrained `risk` measured worse and was not shipped.
  The route model is retrained with chat messages: wrong hints on held-out chat fell from 24 of 42 to 1, at the cost
  of fewer hints on real requests (71% instead of 82%, 94% right).
  Repositories holding an older starter model get the new ones on their next decision;
  models you trained or the learning loop promoted are never replaced.
- Rule-based guesses no longer call every short report noise.
- The project root is found without starting git (faster on Windows).
- Starter models and a documented settings file (`.claude/dev-decide.json`) are installed on first use.

### Fixed
- Rolling back to the oldest archived model crashed.
- Secrets written as `KEY=value` without quotes (as in `.env` files) were not detected; a diff too large to scan
  fully was reported as having no secrets.
- A Claude Code plan or usage limit message was taken for a model answer (and cached by the kit).
- A gate could block on rough local guesses alone; such rules now ask for review, while rules and models still block.
- Reinstalling while the routing-hint server ran failed on Windows (a memory-mapped file); the installer now stops it first.
- The routing hint named an agent even when the route model said none fits (it took the best of the rest).
- The routing hint arrived twice when the plugin and a global install were both active (Claude Code runs a plugin's
  hooks separately); the hint server now answers each prompt once, and identical copies no longer restart each other.
- Gates passed on answers that were unsure; `.pyc` files were reviewed as part of a change; a `COMMIT -x` argument
  could be read as a git option; and the local API answered other websites when no token was set.
