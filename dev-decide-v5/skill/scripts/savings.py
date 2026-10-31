#!/usr/bin/env python3
"""Where your Claude Code tokens went, and how much of it the token guard targets, from your own transcripts.

  python3 savings.py                 this project, the last 7 days, against the 7 days before
  python3 savings.py --days 30       a longer period
  python3 savings.py --all           every project on this machine
  python3 savings.py --json          the same numbers as JSON

It reads the transcripts Claude Code already keeps in ~/.claude/projects (no model call, no network, nothing written)
and counts, over the period:

  spent      new tokens (input + cache writes + output), tokens read back from the cache, and model calls
  flagged    tokens that entered the conversation through reads the guard speaks up about: a whole read of a file of
             40 KB or more, a read of the same file again with no edit in between, `git log` with no count, and `cat`
             of a big file. Each read's size is the size of its result in the transcript (about 4 characters a token)

"Flagged" is an upper bound on what the guard could save, not a saving: some of those reads were needed, and the
guard only advises. The real test is the share falling from the earlier period to the later one once it is installed.
Only counts and sizes are kept in memory while it runs; no path or text is printed or stored.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import guard  # noqa: E402
import tokens  # noqa: E402

READ_BYTES = 40000
CHARS_PER_TOKEN = 4
DAY_MS = 86400000


def _root(cwd):
    p = Path(cwd or os.getcwd()).resolve()
    for d in [p] + list(p.parents):
        if (d / ".git").exists():
            return d
    return p


def project_dirs(cwd, everything=False):
    base = Path(os.path.expanduser("~")) / ".claude" / "projects"
    if everything:
        return [d for d in base.iterdir() if d.is_dir()] if base.is_dir() else []
    d = base / re.sub(r"[^A-Za-z0-9]", "-", str(_root(cwd)))
    return [d] if d.is_dir() else []


def _ms(stamp):
    try:
        import datetime
        return datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp() * 1000
    except (ValueError, TypeError):
        return None


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _rows(path):
    try:
        with open(str(path), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    yield r
    except OSError:
        return


def scan(dirs, start_ms, end_ms):
    """Totals over [start_ms, end_ms) from the transcripts under `dirs`."""
    rows_in_window, flagged = [], {"big-file": [0, 0], "repeat": [0, 0], "bash": [0, 0]}
    sessions = set()
    files = [f for d in dirs for f in d.rglob("*.jsonl")]
    for f in files:
        try:
            if f.stat().st_mtime * 1000 < start_ms:
                continue
        except OSError:
            continue
        uses, read_at, edited = {}, {}, {}
        for r in _rows(f):
            ts = _ms(r.get("timestamp"))
            inside = ts is not None and start_ms <= ts < end_ms
            m = r.get("message") if isinstance(r.get("message"), dict) else {}
            content = m.get("content")
            if r.get("type") == "assistant":
                if inside:
                    rows_in_window.append(r)
                    sessions.add(r.get("sessionId") or f.stem)
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        uses[b.get("id")] = (b.get("name"), b.get("input") if isinstance(b.get("input"), dict) else {})
                        if b.get("name") in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                            p = (b.get("input") or {}).get("file_path") or (b.get("input") or {}).get("notebook_path")
                            if p:
                                edited[p] = edited.get(p, 0) + 1
            elif r.get("type") == "user" and isinstance(content, list):
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_result") or b.get("tool_use_id") not in uses:
                        continue
                    name, tin = uses[b["tool_use_id"]]
                    size = len(_text(b.get("content")))
                    if name == "Read":
                        p = tin.get("file_path")
                        key = (p, tin.get("offset"), tin.get("limit"))
                        again = key in read_at and edited.get(p, 0) == read_at[key]
                        read_at[key] = edited.get(p, 0)
                        if not inside or not p or p.lower().endswith(guard.SKIP_SUFFIXES):
                            continue
                        if again and size > 2000:
                            flagged["repeat"][0] += 1
                            flagged["repeat"][1] += size // CHARS_PER_TOKEN
                        elif tin.get("limit") is None and not tin.get("offset") and size >= READ_BYTES:
                            flagged["big-file"][0] += 1
                            flagged["big-file"][1] += size // CHARS_PER_TOKEN
                    elif name == "Bash" and inside:
                        cmd = tin.get("command")
                        if not isinstance(cmd, str) or len(cmd) > 2000 or any(c in cmd for c in ";&\n`$("):
                            continue
                        cmd = cmd.strip()
                        unbounded_log = guard._GIT_LOG.match(cmd) and not guard._LOG_BOUND.search(cmd)
                        big_cat = cmd.startswith("cat ") and "|" not in cmd and ">" not in cmd and size >= READ_BYTES
                        if (unbounded_log and size >= 4000) or big_cat:
                            flagged["bash"][0] += 1
                            flagged["bash"][1] += size // CHARS_PER_TOKEN
    spent = tokens.count(rows_in_window)
    fl = {k: {"count": v[0], "tokens": v[1]} for k, v in flagged.items()}
    total = sum(v["tokens"] for v in fl.values())
    fresh = tokens.fresh(spent)
    return {"sessions": len(sessions), "spent": spent, "flagged": fl, "flagged_tokens": total,
            "flagged_share": round(total / float(fresh), 3) if fresh else None}


def guard_notes(cwd, days, everything=False):
    """Notes the guard gave in the period (from the decision graph's records, when the full plugin is installed)."""
    try:
        import traces
        pid = None if everything else traces.slug(_root(cwd))
        n = 0
        for p in ([x["id"] for x in traces.projects()] if everything else [pid]):
            n += sum(1 for e in traces.load(p, days) if e.get("kind") == "guard")
        return n
    except Exception:
        pass
    try:  # the lean plugin's own log
        base = Path(os.path.expanduser("~")) / ".claude" / "dev-decide" / "guard"
        files = list(base.glob("*.jsonl")) if everything else [base / ("%s.jsonl" % re.sub(r"[^A-Za-z0-9]", "-", str(_root(cwd))))]
        since = time.time() * 1000 - days * DAY_MS
        n = 0
        for f in files:
            for r in _rows(f):
                n += 1 if r.get("ts", 0) >= since else 0
        return n
    except Exception:
        return None


def report(days=7, cwd=None, everything=False):
    now = time.time() * 1000
    dirs = project_dirs(cwd, everything)
    return {"days": days, "all_projects": everything,
            "now": scan(dirs, now - days * DAY_MS, now + 1),
            "before": scan(dirs, now - 2 * days * DAY_MS, now - days * DAY_MS),
            "guard_notes": guard_notes(cwd, days, everything)}


def _pct(x):
    return "n/a" if x is None else "%d%%" % round(100 * x)


def text(rep):
    n, b = rep["now"], rep["before"]
    out = ["dev-decide savings: last %d days, %s (%d sessions)" % (rep["days"], "all projects" if rep["all_projects"]
                                                                  else "this project", n["sessions"])]
    s = n["spent"]
    if not s:
        out.append("No Claude Code transcripts found for this period, so there is nothing to count yet.")
        return "\n".join(out)
    out.append("Spent: %s new tokens (%s written to the cache as context grew, %s output, %s other input), %s read back "
               "from the cache, %d model calls" % (
                   tokens.short(tokens.fresh(s)), tokens.short(s["cache_write"]), tokens.short(s["output"]),
                   tokens.short(s["input"]), tokens.short(s["cache_read"]), s["calls"]))
    out.append("Flagged: reads the token guard speaks up about put about %s tokens into the conversation (%s of the new tokens)"
               % (tokens.short(n["flagged_tokens"]), _pct(n["flagged_share"])))
    labels = (("big-file", "whole reads of files over 40 KB"), ("repeat", "the same file read again, unedited"),
              ("bash", "git log with no count, cat of a big file"))
    for key, label in labels:
        v = n["flagged"][key]
        out.append("    %-42s %4d   about %s tokens" % (label, v["count"], tokens.short(v["tokens"])))
    if b["spent"]:
        out.append("Before (the %d days earlier): %s of the new tokens were flagged%s" % (
            rep["days"], _pct(b["flagged_share"]),
            "; the later share is lower" if (b["flagged_share"] or 0) > (n["flagged_share"] or 0) else ""))
    if rep["guard_notes"] is not None:
        out.append("Guard notes given in this period: %d" % rep["guard_notes"])
    out.append("Flagged is an upper bound on what the guard could save, not a saving: some reads were needed, and the "
               "guard only advises. Install it, then run this again later: a falling share is the evidence.")
    return "\n".join(out)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--all", action="store_true", help="every project")
    ap.add_argument("--cwd")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rep = report(max(1, min(a.days, 365)), a.cwd, a.all)
    print(json.dumps(rep) if a.json else text(rep))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # a report must not look like a crash in a skill
        print("dev-decide savings: could not read the transcripts (%s)" % type(exc).__name__)
        sys.exit(0)
