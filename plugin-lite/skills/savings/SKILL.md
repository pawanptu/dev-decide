---
name: savings
description: Show where this project's Claude Code tokens went in the last 7 days and how much of it the token guard targets (whole reads of big files, repeat reads, unbounded git log). Use when the user asks about token use, cost or savings.
disable-model-invocation: true
allowed-tools: Bash(sh ${CLAUDE_SKILL_DIR}/../../scripts/run.sh savings.py)
---

```!
sh ${CLAUDE_SKILL_DIR}/../../scripts/run.sh savings.py
```

Print the report above to the user exactly as it is, then add nothing. If it says no transcripts were found, say so in one line.
