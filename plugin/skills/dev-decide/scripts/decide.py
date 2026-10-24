#!/usr/bin/env python3
"""dev-decide command line.

  decide.py "pr :: DIFF"                 JSON decision on stdout
  decide.py --summary "risk :: DIFF"     one summary line
  decide.py --file request.json          request (text or JSON spec) from a file
  decide.py --fail-on block "pr :: DIFF --staged"   exit 1 when any gate blocks (CI, pre-commit)
  decide.py --skill < request            format used by the Claude Code skill

Flags inside the request: +why (evidence), +local (never call a model), +llm (always ask the model),
+deep (model may read repository files), +json (skill replies with JSON), +nocache.

Learning (dev-decide improves from use; see learn.py):
  decide.py fix last issue_type=bug      correct an answer (key=ok confirms it); learned at once
  decide.py status                       what has been learned, spot-check results, recent model switches
  decide.py improve                      learn from everything pending and run queued spot checks now
  decide.py rollback issue_type          undo the last automatic model switch
  decide.py forget recent|memory|all     delete kept text and remembered corrections
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SKILL_JSON_LIMIT = 6000


def _skill_output(out, request_text):
    import ddcore as dd
    wants_json = "+json" in (request_text or "").split("::", 1)[0]
    text = json.dumps(out, separators=(",", ":"))
    lines = ["Summary: " + out.get("summary", "")]
    if out.get("error"):
        lines.append("Reply: tell the user, in one line: " + out["summary"])
    elif wants_json:
        lines.append("Reply: reply with the JSON line below, unchanged, and nothing else.")
    else:
        lines.append("Reply: if the user invoked this directly, reply with the Summary line above, unchanged, "
                     "and nothing else. If you invoked it during a larger task, use the JSON values and continue "
                     "the task.")
    if len(text) > SKILL_JSON_LIMIT and not wants_json:
        try:
            path = dd.state_dir(dd.project_root()) / "last.json"
            path.write_text(json.dumps(out, indent=2), encoding="utf-8")
            lines.append("JSON (abridged; full decision in %s):" % path)
            slim = dict(out)
            if "items" in slim:
                slim["items"] = [{"id": it.get("id"), "gates": it.get("gates"), "error": it.get("error")}
                                 for it in out["items"]]
            else:
                slim["answers"] = {k: {f: v for f, v in a.items() if f not in ("probs", "evidence")}
                                   for k, a in out.get("answers", {}).items()}
            text = json.dumps(slim, separators=(",", ":"))[:SKILL_JSON_LIMIT]
        except Exception:
            lines.append("JSON:")
    else:
        lines.append("JSON:")
    lines.append(text)
    return "\n".join(lines)


def main(argv):
    ap = argparse.ArgumentParser(add_help=True, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skill", action="store_true", help="read the request from stdin and print the skill format")
    ap.add_argument("--summary", action="store_true", help="print only the summary line")
    ap.add_argument("--file", help="read the request from a file")
    ap.add_argument("--cwd", help="run as if started in this directory")
    ap.add_argument("--fail-on", choices=["block", "review"], help="exit 1 when any gate reaches this verdict")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("request", nargs="*")
    args = ap.parse_args(argv)

    if args.skill or args.request == ["-"]:
        text = sys.stdin.read()
    elif args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    else:
        text = " ".join(args.request)
    text = text.strip()

    try:
        import learn
        if learn.is_command(text):
            out = learn.command(text, cwd=args.cwd)
        else:
            import engine
            out = engine.decide(text=text, cwd=args.cwd, use_cache=not args.no_cache)
    except Exception as exc:  # never crash the caller (the skill aborts on a failing command)
        out = {"error": "engine crashed: %s: %s" % (type(exc).__name__, str(exc)[:200]),
               "summary": "dev-decide error: engine crashed (%s)" % type(exc).__name__}

    if args.skill:
        print(_skill_output(out, text))
        return 0
    print(out.get("summary") or out.get("error") if args.summary else json.dumps(out))
    if args.fail_on:
        order = ["pass", "review", "block"]
        limit = order.index(args.fail_on)
        verdicts = [g["verdict"] for g in (out.get("gates") or {}).values()]
        for it in out.get("items") or []:
            verdicts += [g["verdict"] for g in (it.get("gates") or {}).values()]
        if any(order.index(v) >= limit for v in verdicts):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
