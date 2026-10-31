# Security policy

## Reporting a vulnerability

Please report security problems privately. Do not open a public issue.

Use GitHub's private reporting: go to the repository's **Security** tab and choose **Report a vulnerability**.

Include:
- What the problem is and which file or endpoint it affects.
- Steps to reproduce, or a short proof of concept.
- The version (see `CHANGELOG.md`) and your environment.

You will get a reply within 7 days. We will tell you when it is fixed and, if you want, credit you in the changelog.

## What is in scope

- The engine and CLI (`dev-decide-v5/skill/`).
- The HTTP API (`serve.py`): authentication, rate limits, the daily model-call cap, file access and request handling.
- The Claude Code plugin and its hooks (`plugin/`), including the local hint server (`hookd.py`).
- The decision graph (`traces.py`, the `/graph` page) and the `dev-decide-graph` side panel (`plugin-graph/`).
- The web console (`ui.html`, `ui.js`) and the MCP server (`mcp_server.py`).
- The deployment files (`Dockerfile`, the CI workflow).

## What is out of scope

- Findings that need a leaked token or key. Keep your `DEV_DECIDE_TOKEN` and `ANTHROPIC_API_KEY` secret and rotate them if exposed.
- Running the 5.0 HTTP API on the internet. Use 5.0.1 or later, which adds the token check and refuses to listen beyond localhost without one.
- Denial of service by sending a very large volume of requests to a server you host without the rate limit.

## What stays on your machine

- **The hint server** (`hookd.py`, port 47321) listens on `127.0.0.1` only. It refuses a request whose `Host` is not
  localhost, and one with an `Origin` header other than its own page's, so other websites cannot read it.
- **The decision graph** is kept in `~/.claude/dev-decide/traces/`, never in a repository, for 30 days. Of a prompt
  it keeps only the first 40 characters, with anything that looks like a key or token masked; it never keeps the
  prompt Claude gives a subagent, or any tool result. `"decision_graph": {"prompt_chars": 0}` keeps no prompt text,
  `"enabled": false` keeps nothing, and a server that listens beyond localhost records nothing.
- **The routing hint's usage memory** keeps, per project, the meaning vector (256 numbers) of each prompt after
  which Claude ran an agent or skill, with that agent's name, in `~/.claude/dev-decide/traces/usage/`, never the
  prompt's text. `"router_hook": {"learn": false}` turns it off. For an agent's run time it reads only the first
  line's timestamp of the agent's transcript; at the end of each reply it reads the transcript's tail and keeps only
  the reply's duration, the names and counts of the tools used and the four token counts Claude Code already writes
  there (new, cache write, cache read, output; numbers only), never their inputs or results. Plan steps
  (to-do items and tasks) keep their first 40 characters, like prompts. To judge a sub-task and teach the usage
  memory it reads the sub-task Claude gave an agent in memory only; it keeps the first 40 characters of its short
  description, never the prompt Claude wrote for the agent.
- **The token guard** (a `PreToolUse` hook on `Read` and `Bash`, local rules only, no model call) looks at a file's size
  and a command's words to add a note such as "this file is about 20,000 lines". It keeps in memory (not on disk, lost
  when the server stops) which files were read and when, to notice a re-read; the trace keeps only the rule's name, the
  tool and a token estimate, never a path or a command. `"token_guard": {"enabled": false}` turns it off.
- **Instant answers** (off unless `"instant": {"enabled": true}`): a prompt that starts with `/dev-decide ` is answered
  by the local engine instead of Claude. It runs the same engine with the same model backend as the skill, so the
  request goes where the skill's would; nothing else about the prompt is stored.
- **The files hint** (experimental, off unless `"files_hint": {"enabled": true}`) keeps, with the usage memory above, the
  path relative to the project of each file Claude edited after a prompt and that prompt's meaning vector. It never
  keeps a file outside the project, in `.git`, or with a name that suggests a secret (`.env`, keys, tokens,
  credentials), and no file contents.
- **The web console** loads only its own scripts (`script-src 'self'`: no inline script, no `eval`, no CDN), and a
  key typed into it is sent with that one request only, never stored.

## Supported versions

Security fixes go to the latest release only.

## For people who deploy it

- Always set `DEV_DECIDE_TOKEN` (16 characters or more) before listening beyond localhost.
- Keep `.env` out of git. Use your host's secret manager in production.
- Keep the daily model-call cap on, so a leaked token cannot run up a bill.
