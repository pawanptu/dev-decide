#!/usr/bin/env python3
"""The token guard as a plain command hook, for the lean plugin (dev-decide-lite): no server, no meaning model.

Claude Code runs this before each Read or Bash call (PreToolUse). It reads the hook input on stdin and, if the call is
likely to waste tokens (see guard.py), prints a note for Claude's context; otherwise it prints nothing. It never blocks
unless "token_guard": {"mode": "block_first"} is set, and any failure means "no opinion".

It remembers which files were read, between calls, in ~/.claude/dev-decide/guard/state.json under a hash of the path
(never the path itself), and logs each note's rule and token estimate (never a path or command) to
~/.claude/dev-decide/guard/<project>.jsonl, which savings.py counts. Settings are read from .claude/dev-decide.json in
the project (or a parent) and ~/.claude/dev-decide.json.
"""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import guard  # noqa: E402

HOME = Path(os.path.expanduser("~")) / ".claude"
FOLDER = HOME / "dev-decide" / "guard"
MAX_LOG_BYTES = 1 << 20


def _json(path):
    try:
        with open(str(path), encoding="utf-8") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def policy(cwd):
    """The token_guard setting: the project's over the personal one."""
    found = _json(HOME / "dev-decide.json").get("token_guard")
    p = Path(cwd or os.getcwd()).resolve()
    for d in [p] + list(p.parents):
        mine = _json(d / ".claude" / "dev-decide.json").get("token_guard")
        if mine is not None:
            found = mine
            break
        if (d / ".git").exists():
            break
    return {"token_guard": found}


def _hash(*parts):
    return hashlib.sha1("\0".join(str(x) for x in parts).encode("utf-8", "replace")).hexdigest()[:16]


def _slug(cwd):
    import savings
    return re.sub(r"[^A-Za-z0-9]", "-", str(savings._root(cwd)))


def _log(cwd, ev):
    try:
        FOLDER.mkdir(parents=True, exist_ok=True)
        p = FOLDER / ("%s.jsonl" % _slug(cwd))
        row = {"ts": int(time.time() * 1000), "rule": ev.get("rule"), "tool": ev.get("tool"), "tokens": ev.get("tokens")}
        with open(str(p), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        if p.stat().st_size > MAX_LOG_BYTES:
            lines = p.read_text(encoding="utf-8").splitlines()[-2000:]
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def main():
    if os.environ.get("DEV_DECIDE_CHILD"):
        return
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(data, dict) or data.get("hook_event_name") != "PreToolUse":
        return
    cwd = data.get("cwd") or None
    cfg = guard.config(policy(cwd))
    if not cfg["enabled"]:
        return
    state_file = FOLDER / "state.json"
    state = _json(state_file)
    reads, asked = state.get("reads") or {}, state.get("asked") or {}
    guard._k = _hash
    guard._READS.update({k: v for k, v in reads.items() if isinstance(v, list)})
    guard._ASKED.update({k: v for k, v in asked.items() if isinstance(v, (int, float))})
    out, ev = guard.check(data, cfg)
    try:
        FOLDER.mkdir(parents=True, exist_ok=True)
        tmp = state_file.with_name("state.%d.tmp" % os.getpid())
        tmp.write_text(json.dumps({"reads": guard._READS, "asked": guard._ASKED}), encoding="utf-8")
        os.replace(str(tmp), str(state_file))
    except OSError:
        pass
    if out:
        print(json.dumps(out))
        sys.stdout.flush()
    if ev:
        _log(cwd, ev)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a hook must never break the session
    sys.exit(0)
