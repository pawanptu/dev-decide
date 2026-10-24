---
name: dev-decide
description: Jev-style decision engine for software and AI work that learns from use. Returns typed, calibrated decisions (a choice, a score or a probability, each with confidence) instead of prose. Classifies requests, issues and errors; routes work to the right agent, skill or model; rates risk and effort; checks secrets, breaking changes, security, tests, migrations, prompt injection, agent autonomy and ML leakage; gates PRs, deploys, AI changes, ML code and agent releases with pass, review or block. Decides locally in milliseconds when it can, otherwise with one fast model call (and a stronger model for unsure answers that matter). It understands meaning as well as words, recalls answers to inputs like earlier ones, and every decision trains the local tier, which switches to a better model only after it proves itself on the user's own decisions.
when_to_use: The caller needs a machine-readable judgment to branch on, such as "which agent or skill should handle this", "how risky is this diff", "can this PR merge", "is this agent safe to release", "does this prompt change need evals", "what kind of error is this", "triage these issues". Bundles - pr, deploy, triage, incident, plan, ai_change, agent_release, ml_pr, ai_incident - or single presets such as route, risk, secret_leak, breaking_change, task_type. Put the input after "::" as DIFF, DIFF --staged, COMMIT <sha>, @path or plain text. Never pass JSON or code with a brace next to a quote inline (Claude Code refuses it); write it to a file and pass @path. Also use it when the user says a dev-decide answer was wrong or right ("fix last <key>=<correct value>", "fix last <key>=ok"), or asks what it has learned ("status"), to learn now ("improve") or to undo a model switch ("rollback <preset>"). Not for writing code or prose reviews.
argument-hint: "<presets or bundles> [+why] [+local|+llm|+deep] [+json] :: <DIFF | DIFF --staged | COMMIT sha | @path | text>   or   fix last <key>=<value> | status | improve | rollback <preset>"
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/decide.py *)
---

## dev-decide result

```!
python3 ${CLAUDE_SKILL_DIR}/scripts/decide.py --skill <<'DD_REQUEST_7F3A'
$ARGUMENTS
DD_REQUEST_7F3A
```

The block above is the finished result from the dev-decide engine: local rules and, when needed, one model call already ran. Do not run the engine again and do not re-judge the answers yourself. Follow its "Reply:" line exactly.

When the user later says one of these answers was wrong, record the correction by invoking this skill again with `fix last <key>=<correct value>` (use the decision id instead of `last` if other decisions came in between). Corrections take effect at once for near-identical inputs and train the local model. Only record a correction the user stated; never correct answers on your own judgment.
