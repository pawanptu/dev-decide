---
name: dev-decide
description: Typed, calibrated decisions (a choice or a score, each with confidence) instead of prose. Classifies and routes requests, issues and errors; rates risk and effort; checks secrets, breaking changes, security, tests, migrations, prompt injection and ML leakage; gates PRs, deploys, AI changes and agent releases with pass, review or block. Decides locally in milliseconds when it can, else with one fast model call, and learns from use.
when_to_use: A machine-readable judgment to branch on - "which agent should handle this", "how risky is this diff", "can this PR merge", "what kind of error is this". Bundles - pr, deploy, triage, incident, plan, ai_change, agent_release, ml_pr, ai_incident - or presets such as route, risk, secret_leak, task_type. Input after "::" as DIFF, DIFF --staged, COMMIT <sha>, @path or text; never pass JSON with a brace next to a quote inline (write it to a file, pass @path). Also for "fix last <key>=<value>", "status", "improve", "rollback <preset>". Not for writing code or prose reviews.
argument-hint: "<presets or bundles> [+why] [+local|+llm|+deep] [+json] :: <DIFF | DIFF --staged | COMMIT sha | @path | text>   or   fix last <key>=<value> | status | improve | rollback <preset>"
allowed-tools: Bash(sh ${CLAUDE_SKILL_DIR}/scripts/run.sh decide.py *)
---

## dev-decide result

```!
sh ${CLAUDE_SKILL_DIR}/scripts/run.sh decide.py --skill <<'DD_REQUEST_7F3A'
$ARGUMENTS
DD_REQUEST_7F3A
```

The block above is the finished result from the dev-decide engine: local rules and, when needed, one model call already ran. Do not run the engine again and do not re-judge the answers yourself. Follow its "Reply:" line exactly.

When the user later says one of these answers was wrong, record the correction by invoking this skill again with `fix last <key>=<correct value>` (use the decision id instead of `last` if other decisions came in between). Corrections take effect at once for near-identical inputs and train the local model. Only record a correction the user stated; never correct answers on your own judgment.
