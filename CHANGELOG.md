# Changelog

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
  neither run trained on). Repositories holding an older starter model get the new ones on their next decision;
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
- Gates passed on answers that were unsure; `.pyc` files were reviewed as part of a change; a `COMMIT -x` argument
  could be read as a git option; and the local API answered other websites when no token was set.
