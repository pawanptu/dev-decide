#!/usr/bin/env python3
"""UserPromptSubmit hook: suggest the best-matching agent or skill, locally, in milliseconds.

Installed only with `install.py --router-hook`. For every prompt it scores your agents
and skills (the learned route model when one is trained and confident, otherwise
zero-shot matching on names and descriptions) and, only when the match is clear,
adds one line of context such as:

  dev-decide routing hint: this request most closely matches the skill `db-migrate`
  (Write and review Prisma database migrations safely); match confidence 0.86.

It never blocks a prompt, never calls a model, stores nothing, and stays silent for
slash commands, short prompts and unclear matches. Tune it in .claude/dev-decide.json:
  "router_hook": {"min_confidence": 0.6, "min_margin": 0.2, "builtins": false, "enabled": true}
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


class _Hint(object):
    kind = "choose"

    def __init__(self, probs):
        self.probs = probs


def main():
    if os.environ.get("DEV_DECIDE_CHILD"):
        return
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    prompt = (data.get("prompt") or "").strip()
    if not prompt or prompt.startswith("/") or len(prompt.split()) < 4 or len(prompt) > 4000:
        return
    import ddcore as dd
    import router

    root = dd.project_root(data.get("cwd") or None)
    policy = dd.load_policy(root)
    cfg = policy.get("router_hook") or {}
    if cfg.get("enabled") is False:
        return
    routes = dd.discover_routes(root, policy, include_builtins=bool(cfg.get("builtins", False)))
    if not routes:
        return
    min_conf = float(cfg.get("min_confidence", 0.6))
    min_margin = float(cfg.get("min_margin", 0.2))

    best = None
    try:  # a trained route model wins when it is enabled and confident
        import fastmodel
        reg = fastmodel.Registry(root)
        if reg.enabled("route"):
            import inputs  # imported only when needed: the analyzers compile many patterns
            import signals
            item = inputs.Item(None)
            item.kind, item.text = "text", prompt
            F, _ = signals.analyze(item, [])
            ans = reg.answer("route", "choose", fastmodel.Features(item, F))
            if ans:  # an agent or skill the model has never seen may fit better: let the zero-shot match decide
                import learn
                zs = router.route(prompt, routes)
                if zs and learn.unknown_option_hint(reg, "route", _Hint(zs[0])):
                    ans = None
            if ans:
                name = max((k for k in ans["probs"] if k not in ("none", "_rest")), key=ans["probs"].get, default=None)
                if name:
                    best = (name, ans["probs"][name])
    except Exception:
        best = None
    if best is None:
        best = router.best_route(prompt, routes, min_confidence=min_conf, min_margin=min_margin)
    if not best:
        return
    name, conf = best
    route = next((r for r in routes if r["name"] == name), None)
    if route is None:
        return
    desc = (" (%s)" % route["description"][:120]) if route.get("description") else ""
    context = ("dev-decide routing hint: this request most closely matches the %s `%s`%s; match confidence %.2f."
               % (route["kind"], name, desc, conf))
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a hook must never break the session
    sys.exit(0)
