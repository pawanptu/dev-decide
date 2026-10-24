#!/usr/bin/env python3
"""Record the correct answer for a past dev-decide decision.

Usage:
  label.py <decision-id | last> key=value [key=value ...] [--item ITEM_ID]

Examples:
  label.py last risk=4 breaking_change=true
  label.py 20261008-231500-ab12 task_type=bugfix
  label.py last severity=sev3_minor --item ISSUE-42

Values: true/false for verify presets, an integer for score presets,
the option name for choose presets. Labels go to
.claude/dev-decide/labels.jsonl; run calibrate.py once you have 20+ per preset.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ddcore as dd  # noqa: E402


def parse_value(v):
    low = v.strip().lower()
    if low in ("true", "yes", "y"):
        return True
    if low in ("false", "no", "n"):
        return False
    try:
        return int(v)
    except ValueError:
        return v.strip()


def main(argv):
    item = None
    if "--item" in argv:
        i = argv.index("--item")
        if i + 1 >= len(argv):
            print("--item needs a value")
            return 2
        item = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if len(argv) < 2 or any("=" not in kv for kv in argv[1:]):
        print(__doc__)
        return 2

    root = dd.project_root()
    d = dd.log_dir(root)
    log = dd.read_jsonl(d / "log.jsonl")
    did = argv[0]
    if did == "last":
        if not log:
            print("No decisions logged yet in %s" % (d / "log.jsonl"))
            return 1
        did = log[-1]["id"]
    known = {}
    for r in log:
        if r.get("id") == did:
            known[(r.get("item"), r.get("key"))] = r
    if not known:
        print("Warning: decision %s not found in the log; saving labels anyway." % did)

    rows = []
    for kv in argv[1:]:
        key, _, val = kv.partition("=")
        key = key.strip()
        truth = parse_value(val)
        r = known.get((item, key))
        if known and r is None:
            print("Warning: %s has no answer '%s'%s" % (did, key, " for item %s" % item if item else ""))
        elif r is not None and r.get("type") == "score" and not isinstance(truth, int):
            print("Warning: %s is a score; expected an integer, got %r" % (key, truth))
        elif r is not None and r.get("type") == "verify" and not isinstance(truth, bool):
            print("Warning: %s is a verify; expected true or false, got %r" % (key, truth))
        rows.append({"id": did, "item": item, "key": key, "truth": truth, "ts": int(time.time())})

    dd.append_jsonl(d / "labels.jsonl", rows)
    print("Saved %d label(s) for %s" % (len(rows), did))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
