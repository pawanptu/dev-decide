# dev-decide-lite

Waste fewer Claude Code tokens. Two things, both local, with no server, no models and no network:

- **A token guard.** Before Claude reads a file over 40 KB whole, reads an unchanged file again, or runs `git log` with
  no count or `cat` on a big file, it gets a one-line note: use Grep, or read a range. It only advises.
- **A savings report.** `/dev-decide-lite:savings` counts, from the transcripts Claude Code already keeps, where the last
  7 days went (new tokens, cache, output) and how many tokens came in through reads the guard targets, against the 7
  days before.

```
/plugin marketplace add pawanptu/dev-decide
/plugin install dev-decide-lite@dev-decide
```

## Honest numbers

The report shows an **upper bound** on what the guard could save, not a saving: some of those reads were needed, and the
guard only advises. On the author's own busiest days the targeted reads were 2-3% of new tokens. Check yours first,
without installing anything: `python3 plugin-lite/scripts/savings.py --days 30`. If the share is low, this plugin will
not change much. If it is high, install it, work as usual, and run the report again: a falling share is the evidence.

## Settings

In `.claude/dev-decide.json` (or `~/.claude/dev-decide.json`):

```json
{"token_guard": {"enabled": true, "mode": "advise", "read_bytes": 40000, "repeat": true, "bash": true}}
```

`"mode": "block_first"` refuses the first whole read of a big file once (Claude sees why and may read again).
`"enabled": false` turns the guard off.

## What it keeps

Nothing from your prompts or files. In `~/.claude/dev-decide/guard/` it keeps a hash of each read file's path with its
time, size and modification time (to notice a re-read), and a log of each note's rule and a token estimate. The report
reads your transcripts and prints totals; it stores nothing from them.

## Requirements

Python 3.8+ (`python3`, `python` or `py -3`). On Windows, Git Bash or WSL for the `sh` that starts the hook. Each Read or
Bash call starts one short Python process (tens of milliseconds); if Python is missing the hook does nothing.

Part of [dev-decide](https://github.com/pawanptu/dev-decide); the full plugin adds typed decisions, routing hints and gates.
