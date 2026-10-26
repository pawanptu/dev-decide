#!/usr/bin/env python3
"""Install dev-decide for Claude Code. Works on macOS, Linux and Windows.

  python3 install.py                    install for all your projects (~/.claude)
  python3 install.py --project          install into this repository (./.claude); commit it to share
  python3 install.py --router-hook      also add the routing hint (opt-in; settings are backed up first): a small local
                                        server answers it in milliseconds (Claude Code's HTTP hooks), notes which agent
                                        or skill Claude then used, and draws it at http://127.0.0.1:47321/graph
  python3 install.py --router-hook=command   the same hint as a command run on every prompt (older Claude Code)
  python3 install.py --uninstall        remove the skill, the hook, and the old v2 engine agent
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "skill"
HOOK_MARKS = ("hook_route.py", "hookd.py", ":47321/route", ":47321/tool", ":47321/agent", ":47321/turn")  # every form of the routing hint, old and new
HOOK_EVENTS = ("UserPromptSubmit", "SessionStart", "PostToolUse", "SubagentStop", "Stop")
USED_TOOLS = "Agent|Skill"  # what Claude really used, for the decision graph
HOOK_PORT = 47321


def python_command():
    for cand in ("python3", "python"):
        if shutil.which(cand):
            return cand
    if shutil.which("py"):
        return "py -3"
    return "python3"


def settings_file(base, project):
    return base / ("settings.local.json" if project else "settings.json")


def load_settings(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError:
        raise SystemExit("%s is not valid JSON; fix it first (nothing was changed)." % path)
    if not isinstance(data, dict):
        raise SystemExit("%s does not contain a JSON object; nothing was changed." % path)
    return data


def save_settings(path, data):
    if path.exists():
        stem = path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        backup, n = path.with_name(stem), 1
        while backup.exists():  # never overwrite an earlier backup
            n += 1
            backup = path.with_name("%s-%d" % (stem, n))
        shutil.copy2(str(path), str(backup))
        print("  backed up %s -> %s" % (path.name, backup.name))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def strip_hook(data):
    hooks = data.get("hooks") or {}
    removed = 0
    for event in HOOK_EVENTS:
        groups = hooks.get(event) or []
        kept = []
        for g in groups:
            inner = [h for h in (g.get("hooks") or []) if not any(m in json.dumps(h) for m in HOOK_MARKS)]
            removed += len(g.get("hooks") or []) - len(inner)
            if inner:
                kept.append(dict(g, hooks=inner))
        if groups:
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)
    if "hooks" in data and not hooks:
        data.pop("hooks", None)
    return removed


def stop_hint_server(dest=None):
    """Stop a running routing-hint server (hookd.py) before its files are replaced; the next session restarts it.

    Asked over HTTP, so it works even when the server's own files are already gone."""
    import urllib.request

    def call(path, method):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (HOOK_PORT, path), method=method,
                                     data=b"" if method == "POST" else None)
        with urllib.request.urlopen(req, timeout=0.5) as r:
            return json.loads(r.read().decode("utf-8") or "{}")

    try:
        if call("/health", "GET").get("service") != "dev-decide-hook":
            return
        call("/shutdown", "POST")
    except Exception:
        return  # not running
    for _ in range(40):  # wait until the process has exited and released its files
        try:
            call("/health", "GET")
        except Exception:
            break
        time.sleep(0.1)
    time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", action="store_true")
    ap.add_argument("--router-hook", nargs="?", const="http", choices=["http", "command"])
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    base = (Path.cwd() / ".claude") if args.project else (Path.home() / ".claude")
    dest = base / "skills" / "dev-decide"
    legacy = base / "agents" / "dev-decide-engine.md"
    sfile = settings_file(base, args.project)

    stop_hint_server(dest)  # it maps the meaning model's file, which Windows will not delete while mapped
    if args.uninstall:
        if dest.exists():
            shutil.rmtree(str(dest))
            print("removed %s" % dest)
        if legacy.exists():
            legacy.unlink()
            print("removed %s" % legacy)
        if sfile.exists():
            data = load_settings(sfile)
            if strip_hook(data):
                save_settings(sfile, data)
                print("removed the router hook from %s" % sfile)
        print("Per-repo data in <repo>/.claude/dev-decide/ (logs, labels, models) was left in place.")
        return 0

    if sys.version_info < (3, 8):
        raise SystemExit("Python 3.8 or later is required.")
    py = python_command()
    if dest.exists():
        shutil.rmtree(str(dest))
    shutil.copytree(str(SRC), str(dest), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print("installed the skill: %s" % dest)
    if legacy.exists():
        legacy.unlink()
        print("removed the v2 engine agent (no longer needed): %s" % legacy)

    if args.router_hook:
        data = load_settings(sfile)
        strip_hook(data)
        hooks = data.setdefault("hooks", {})
        scripts = dest / "scripts"
        if args.router_hook == "command":
            cmd = '%s "%s"' % (py, (scripts / "hook_route.py").as_posix())
            hooks.setdefault("UserPromptSubmit", []).append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
            hooks.setdefault("PostToolUse", []).append(
                {"matcher": USED_TOOLS, "hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
            hooks.setdefault("SubagentStop", []).append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
            hooks.setdefault("Stop", []).append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
        else:  # a local server answers in milliseconds; each session start makes sure it runs
            cmd = '%s "%s" --ensure' % (py, (scripts / "hookd.py").as_posix())
            hooks.setdefault("SessionStart", []).append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
            hooks.setdefault("UserPromptSubmit", []).append(
                {"hooks": [{"type": "http", "url": "http://127.0.0.1:%d/route" % HOOK_PORT, "timeout": 2}]})
            hooks.setdefault("PostToolUse", []).append(
                {"matcher": USED_TOOLS, "hooks": [{"type": "http", "url": "http://127.0.0.1:%d/tool" % HOOK_PORT,
                                                   "timeout": 2}]})
            hooks.setdefault("SubagentStop", []).append(  # how long each agent really ran, for the decision graph
                {"hooks": [{"type": "http", "url": "http://127.0.0.1:%d/agent" % HOOK_PORT, "timeout": 2}]})
            hooks.setdefault("Stop", []).append(  # the end of each reply: who did the work, and how long it took
                {"hooks": [{"type": "http", "url": "http://127.0.0.1:%d/turn" % HOOK_PORT, "timeout": 2}]})
        save_settings(sfile, data)
        print("added the router hook (%s) to %s" % (args.router_hook, sfile))
        if args.router_hook == "http":
            print("Decision graph (which agent or skill for which task, and timings): http://127.0.0.1:%d/graph"
                  % HOOK_PORT)

    print("")
    print("Python command: %s" % py)
    print("Try it in Claude Code:   /dev-decide pr :: DIFF")
    print("Or from a terminal:      %s %s \"risk :: DIFF\"" % (py, (dest / "scripts" / "decide.py").as_posix()))
    print("Optional project policy: copy %s to <repo>/.claude/dev-decide.json"
          % (dest / "examples" / "dev-decide.json").as_posix())
    print("If Claude Code is already running, run /reload-skills (and restart it for the hook).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
