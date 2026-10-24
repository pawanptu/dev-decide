#!/usr/bin/env python3
"""Install dev-decide for Claude Code. Works on macOS, Linux and Windows.

  python3 install.py                    install for all your projects (~/.claude)
  python3 install.py --project          install into this repository (./.claude); commit it to share
  python3 install.py --router-hook      also add the routing-hint hook (opt-in; settings are backed up first)
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
HOOK_MARK = "hook_route.py"


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
    groups = hooks.get("UserPromptSubmit") or []
    kept = []
    removed = 0
    for g in groups:
        inner = [h for h in (g.get("hooks") or []) if HOOK_MARK not in json.dumps(h)]
        removed += len(g.get("hooks") or []) - len(inner)
        if inner:
            kept.append(dict(g, hooks=inner))
    if groups:
        if kept:
            hooks["UserPromptSubmit"] = kept
        else:
            hooks.pop("UserPromptSubmit", None)
        if not hooks:
            data.pop("hooks", None)
    return removed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", action="store_true")
    ap.add_argument("--router-hook", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    base = (Path.cwd() / ".claude") if args.project else (Path.home() / ".claude")
    dest = base / "skills" / "dev-decide"
    legacy = base / "agents" / "dev-decide-engine.md"
    sfile = settings_file(base, args.project)

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
        script = (dest / "scripts" / "hook_route.py").as_posix()
        entry = {"hooks": [{"type": "command", "command": '%s "%s"' % (py, script), "timeout": 5}]}
        data.setdefault("hooks", {}).setdefault("UserPromptSubmit", []).append(entry)
        save_settings(sfile, data)
        print("added the router hook to %s" % sfile)

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
