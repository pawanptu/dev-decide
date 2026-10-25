# dev-decide

A decision engine for software and AI work. Instead of prose, it returns typed, calibrated decisions (a choice, a score or a probability, each with a confidence) that you or a script can branch on.

It decides locally in milliseconds when it can, asks Claude only when it can't, and learns from every answer.

## Install

```
/plugin marketplace add pawanptu/dev-decide
/plugin install dev-decide@dev-decide
```

Or from a terminal:

```
claude plugin marketplace add pawanptu/dev-decide
claude plugin install dev-decide@dev-decide
```

## Use

```
/dev-decide pr :: DIFF --staged
/dev-decide risk :: DIFF
/dev-decide triage :: <paste an issue>
/dev-decide route :: fix the login bug
```

Bundles: `pr`, `deploy`, `triage`, `incident`, `plan`, `ai_change`, `agent_release`, `ml_pr`, `ai_incident`. Single presets include `route`, `risk`, `secret_leak`, `breaking_change` and `task_type`.

Reading a result: `risk 3.0? | needs a human: risk` means a score of 3.0 on a scale of 1 to 5, `?` marks low confidence, and "needs a human" means it abstained so you decide.

## What the plugin installs

- **Skill** `dev-decide`, in `skills/dev-decide/`.
- **Two hooks**, in `hooks/hooks.json`:
  - `SessionStart` starts a small helper server bound to `127.0.0.1` only.
  - `UserPromptSubmit` asks that local server whether one of your agents or skills clearly matches the prompt. If so, it adds a one-line routing hint.

## Privacy and credentials

- The routing hook listens on `127.0.0.1` only, never calls a model, never blocks a prompt, and writes nothing.
- The plugin uses no one else's credentials. By default it asks Claude through your own Claude Code sign-in. To use the API instead, set `ANTHROPIC_API_KEY` in your shell. Never commit the key.
- Logs, labels and learned models are stored in your repository under `.claude/dev-decide/`. Nothing is sent anywhere else.

## Turn off the routing hint

Put this in `.claude/dev-decide.json`:

```json
{"router_hook": {"enabled": false}}
```

or disable the plugin's hooks.

## Requirements

Python 3.8+ (found as `python3`, `python` or `py -3`) and git. On Windows, Git Bash or WSL.

## More

Full documentation, the HTTP API and the training kit are in the [main repository](https://github.com/pawanptu/dev-decide).

MIT licensed.
