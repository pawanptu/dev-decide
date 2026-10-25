# dev-decide-graph

A side panel for [dev-decide](../plugin/README.md) that shows its **decision graph**: for each kind of task, what
decided (the trained route model, the zero-shot match, or a `/dev-decide` question), which agent or skill it
suggested, what Claude really used afterwards, and how long each step took.

```
Task (type · area)     Decided by          Suggested        Claude really used
bugfix · auth     ──▶  trained model  ──▶  debugger    ──▶  used debugger   (green: followed the hint)
feature · database ─▶  trained model  ──▶  db-migrate  ──▶  used Explore    (red: something else)
question          ──▶  trained model  ──▶  no hint
```

Below the graph it lists the latest requests with their timings, and a link opens the full page
(`http://127.0.0.1:47321/graph`) with a project picker, 1 hour to 30 days, live refresh and a filterable timeline.

## Install

Needs the `dev-decide` plugin (its local hint server records and serves the graph) and Claude Code 2.1.310 or later.

```
/plugin marketplace add pawanptu/dev-decide
/plugin install dev-decide@dev-decide
/plugin install dev-decide-graph@dev-decide
```

Then type `/dev-decide-graph`. The panel opens only when you ask, and refreshes every 5 seconds while it is open.
In the terminal it draws a text tree; in the desktop app and VS Code, the graph itself.

## What is recorded

Everything stays on your machine, in `~/.claude/dev-decide/traces/` (one file per project, outside the repository):
task type and area, agent and skill names, timings, and the **first 40 characters of each prompt** (keys and tokens
masked) so you can tell requests apart. Events older than 30 days are deleted. Change it in `.claude/dev-decide.json`:

```json
"decision_graph": {"enabled": true, "keep_days": 30, "prompt_chars": 40}
```

`"prompt_chars": 0` keeps no prompt text at all; `"enabled": false` stops recording.

## Develop

```
claude plugin validate plugin-graph
claude plugin test plugin-graph
```
