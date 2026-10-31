"""Token guard: a PreToolUse hook rule that speaks up before Claude spends tokens on a wasteful tool call.

It is local and instant (a file's size, a command's words; no model, no network) and by default only advises: the
note is added to Claude's context beside the tool call, which still goes ahead. Three rules:

  big file     Read of a large file with no offset or limit: say how big it is and point to Grep or offset/limit
  re-read      the same Read again, with the file unchanged: say it was already read (if it is still in context)
  unbounded    `git log` with no count, or `cat` of a large file: say how to bound it

Settings, in .claude/dev-decide.json:
  "token_guard": {"enabled": true, "mode": "advise", "read_bytes": 40000, "repeat": true, "bash": true}
mode "block_first" denies the first big read of a file with the same note (Claude sees the reason) and lets the same
read through when asked again within 10 minutes. "enabled": false turns the guard off.

Nothing about a file is stored except in memory while the hint server runs: the trace keeps the rule's name, the tool
and an estimate of the tokens at stake, never a path or a command.
"""
import os
import re
import shlex
import time

BYTES_PER_TOKEN = 4          # rough, for source code and prose
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ipynb", ".svg")
REPEAT_WINDOW_S = 30 * 60
ASKED_AGAIN_S = 10 * 60
MAX_STATE = 2000
MAX_LINE_SCAN = 5 << 20      # count lines only in files up to this size
_READS = {}                  # (session, agent, path, offset, limit) -> (time, mtime_ns, size)
_ASKED = {}                  # (session, agent, path) -> time of the denial in block_first mode

_GIT_LOG = re.compile(r"^git(\s+-[-\w=./]+)*\s+log\b")
_LOG_BOUND = re.compile(r"(^|\s)(-n\s*\d+|-\d+|--max-count[= ]\d+|-[a-zA-Z]*n\d+|--since\S*|--after\S*|-S\S*|-G\S*|--\S*=\S+|\|)")


def config(policy):
    c = (policy or {}).get("token_guard")
    c = c if isinstance(c, dict) else {}
    return {"enabled": c.get("enabled", True) is not False,
            "mode": "block_first" if c.get("mode") == "block_first" else "advise",
            "read_bytes": max(1000, int(c.get("read_bytes", 40000) or 40000)),
            "repeat": c.get("repeat", True) is not False, "bash": c.get("bash", True) is not False}


def _k(*parts):
    """The key under which a read is remembered. The lean plugin, which keeps this memory in a file between calls,
    swaps in a hash so that no path is ever written to disk."""
    return parts


def _trim(d):
    if len(d) > MAX_STATE:
        for k in sorted(d, key=lambda k: d[k][0] if isinstance(d[k], (tuple, list)) else d[k])[:MAX_STATE // 4]:
            del d[k]


def _resolve(path, cwd):
    if not isinstance(path, str) or not path.strip():
        return None
    p = path.strip().strip("'\"")
    return p if os.path.isabs(p) else os.path.join(cwd or os.getcwd(), p)


def _lines(path, size):
    if size > MAX_LINE_SCAN:
        return None
    try:
        with open(path, "rb") as fh:
            return fh.read().count(b"\n") + 1
    except OSError:
        return None


def _big_note(name, size, lines):
    tok = size // BYTES_PER_TOKEN
    where = "about %d lines" % lines if lines else "%dKB" % (size // 1024)
    return tok, ("dev-decide token guard: %s is %s (~%dk tokens). If you need only part of it, use Grep for the symbol, "
                 "or Read with offset and limit." % (name, where, max(1, tok // 1000)))


def check(data, cfg, now=None):
    """(the hook's JSON output or None, a trace event or None) for one PreToolUse input."""
    now = time.time() if now is None else now
    tool = data.get("tool_name")
    tin = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    cwd = data.get("cwd") or None
    sid, aid = data.get("session_id") or "", data.get("agent_id") or ""

    def out(note, rule, tokens, deny=False):
        hook = {"hookEventName": "PreToolUse"}
        if deny:
            hook.update(permissionDecision="deny", permissionDecisionReason=note + " Read it again if you need the whole file.")
        else:
            hook["additionalContext"] = note
        ev = {"kind": "guard", "rule": rule, "tool": tool, "tokens": int(tokens), "mode": "block_first" if deny else "advise",
              "session": sid or None, "prompt_id": data.get("prompt_id") or None, "agent_id": aid or None}
        return {"hookSpecificOutput": hook}, ev

    if tool == "Read":
        path = _resolve(tin.get("file_path"), cwd)
        if not path or path.lower().endswith(SKIP_SUFFIXES):
            return None, None
        try:
            st = os.stat(path)
        except OSError:
            return None, None
        offset, limit = tin.get("offset"), tin.get("limit")
        key = _k(sid, aid, path, offset, limit)
        if cfg["repeat"]:
            prev = _READS.get(key)
            _READS[key] = (now, st.st_mtime_ns, st.st_size)
            _trim(_READS)
            if prev and now - prev[0] <= REPEAT_WINDOW_S and tuple(prev[1:]) == (st.st_mtime_ns, st.st_size) and st.st_size > 2000:
                mins = max(1, int((now - prev[0]) / 60))
                return out("dev-decide token guard: %s was read %d min ago and has not changed. If that is still in your "
                           "context, use it instead of reading it again." % (os.path.basename(path), mins),
                           "re-read", st.st_size // BYTES_PER_TOKEN)
        if limit is None and not offset and st.st_size >= cfg["read_bytes"]:
            tok, note = _big_note(os.path.basename(path), st.st_size, _lines(path, st.st_size))
            if cfg["mode"] == "block_first":
                akey = _k(sid, aid, path)
                asked = _ASKED.get(akey)
                if asked and now - asked <= ASKED_AGAIN_S:
                    _ASKED.pop(akey, None)
                    return None, None
                _ASKED[akey] = now
                _trim(_ASKED)
                _READS.pop(key, None)  # a denied read did not happen: the retry is not a re-read
                return out(note, "big-file", tok, deny=True)
            return out(note, "big-file", tok)
        return None, None
    if tool == "Bash" and cfg["bash"]:
        cmd = tin.get("command")
        if not isinstance(cmd, str) or len(cmd) > 2000 or any(c in cmd for c in ";&\n`$("):
            return None, None  # only a single plain command is judged
        cmd = cmd.strip()
        if _GIT_LOG.match(cmd) and not _LOG_BOUND.search(cmd):
            return out("dev-decide token guard: `git log` with no count prints the whole history. Add -n 20 (and "
                       "--oneline if you only need subjects).", "unbounded-log", 8000)
        if cmd.startswith("cat ") and "|" not in cmd and ">" not in cmd:
            try:
                parts = shlex.split(cmd, posix=os.name != "nt")
            except ValueError:
                return None, None
            files = [p for p in parts[1:] if not p.startswith("-")]
            if len(files) == 1:
                path = _resolve(files[0], cwd)
                try:
                    size = os.stat(path).st_size if path else 0
                except OSError:
                    size = 0
                if size >= cfg["read_bytes"] and not path.lower().endswith(SKIP_SUFFIXES):
                    tok, note = _big_note(os.path.basename(path), size, _lines(path, size))
                    return out(note.replace("use Grep", "use Grep (or head -n 80)").replace(
                        "or Read with offset and limit", "or the Read tool with offset and limit"), "cat-big-file", tok)
    return None, None
