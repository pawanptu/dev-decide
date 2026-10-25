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
- **The web console** loads only its own scripts (`script-src 'self'`: no inline script, no `eval`, no CDN), and a
  key typed into it is sent with that one request only, never stored.

## Supported versions

Security fixes go to the latest release only.

## For people who deploy it

- Always set `DEV_DECIDE_TOKEN` (16 characters or more) before listening beyond localhost.
- Keep `.env` out of git. Use your host's secret manager in production.
- Keep the daily model-call cap on, so a leaked token cannot run up a bill.
