#!/usr/bin/env python3
"""UserPromptSubmit hook: suggest the best-matching agent or skill, locally, in milliseconds.

Installed only with `install.py --router-hook`. For every prompt it scores your agents
and skills (the learned route model when one is trained and confident, otherwise
zero-shot matching on names and descriptions) and, only when the match is clear,
adds one line of context such as:

  dev-decide routing hint: this request most closely matches the skill `db-migrate`
  (Write and review Prisma database migrations safely); match confidence 0.86.

It never blocks a prompt, never calls a model, and stays silent for slash commands,
short prompts and unclear matches. Tune it in .claude/dev-decide.json:
  "router_hook": {"min_confidence": 0.6, "min_margin": 0.2, "builtins": false, "enabled": true}

For the decision graph it notes what it decided (each step's answer, confidence and
time, the task's type and area, and the prompt's first 40 characters) in
~/.claude/dev-decide/traces (see traces.py); "decision_graph": {"enabled": false} turns
that off, "prompt_chars": 0 keeps no prompt text.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


class _Hint(object):
    kind = "choose"

    def __init__(self, probs):
        self.probs = probs


def _ms(t0):
    return round((time.perf_counter() - t0) * 1000, 1)


# What an editor or Claude Code adds around the typed words (the open file, the selection, reminders): not the
# request, so it must not be routed on, counted as words, or shown as the prompt.
_CONTEXT_TAGS = re.compile(r"<(ide_[a-z_]+|system-reminder)\b[^>]*>.*?</\1>", re.S)


def user_text(prompt):
    return _CONTEXT_TAGS.sub(" ", prompt or "").strip()


def explain(data):
    """(the hook's JSON output for one prompt or None to stay silent, the trace event or None). The event holds
    no prompt text; note() adds the task's labels and stores it. Shared with hookd.py."""
    t0 = time.perf_counter()
    prompt = user_text(data.get("prompt"))
    if not prompt:
        return None, None
    import ddcore as dd
    import router
    import traces

    root = dd.project_root(data.get("cwd") or None)
    policy = dd.load_policy(root)
    ev = {"kind": "hint", "ts": int(time.time() * 1000), "session": data.get("session_id") or None,
          "text": traces.preview(prompt, traces.prompt_chars(policy)), "root": str(root),
          "keep_days": traces.keep_days(policy)} if traces.enabled(policy) else None

    def done(out, **facts):
        if ev is not None:
            ev.update(facts, ms=_ms(t0))
        return out, ev

    cfg = policy.get("router_hook") or {}
    if prompt.startswith("/"):  # typing /skill runs it without the Skill tool: note which skill, nothing else
        m = re.match(r"/([\w:.-]+)", prompt)
        name = m.group(1).split(":")[-1] if m and ev is not None else None
        if name and not any(r["name"] == name for r in dd.discover_routes(root, policy, include_builtins=False)):
            name = None
        return done(None, skipped="slash command", command=name)
    if len(prompt.split()) < 4:
        return done(None, skipped="under 4 words")
    if len(prompt) > 4000:
        return done(None, skipped="over 4000 characters")
    if cfg.get("enabled") is False:
        return done(None, skipped="routing hint turned off")
    routes = dd.discover_routes(root, policy, include_builtins=bool(cfg.get("builtins", False)))
    if not routes:
        return done(None, skipped="no agents or skills")
    min_conf = float(cfg.get("min_confidence", 0.6))
    min_margin = float(cfg.get("min_margin", 0.2))

    steps = []
    best = decided = None
    try:  # a trained route model wins when it is enabled and confident
        import fastmodel
        reg = fastmodel.Registry(root)
        if not reg.enabled("route"):  # no route model here yet: the shipped one, if it knows these agents (read only)
            reg = fastmodel.optional_registry([r["name"] for r in routes]) or reg
        if reg.enabled("route"):
            s0 = time.perf_counter()
            import inputs  # imported only when needed: the analyzers compile many patterns
            import signals
            item = inputs.Item(None)
            item.kind, item.text = "text", prompt
            F, _ = signals.analyze(item, [])
            ans = reg.answer("route", "choose", fastmodel.Features(item, F))
            step = {"step": "trained model", "result": "not sure enough"}
            if ans:
                probs = ans["probs"]
                top = max((k for k in probs if k != "_rest"), key=probs.get, default=None)
                step.update(answer=top, confidence=round(probs[top], 3) if top else None)
                # an agent or skill the model has never seen may fit better: let the zero-shot match decide
                import learn
                zs = router.route(prompt, routes)
                if zs and learn.unknown_option_hint(reg, "route", _Hint(zs[0])):
                    ans = None
                    step["result"] = "a newer agent or skill may fit: asked the zero-shot match"
            if ans:
                if top is None or top == "none" or probs[top] < min_conf:
                    step.update(result="no agent or skill fits" if top in (None, "none") else "below min confidence",
                                ms=_ms(s0))
                    steps.append(step)
                    # the trained model says no agent or skill fits these words: no hint at all
                    return done(None, steps=steps, decided_by="trained model", hint=None, options=len(routes))
                best, decided = (top, probs[top]), "trained model"
                step["result"] = "suggested"
            step["ms"] = _ms(s0)
            steps.append(step)
    except Exception:
        best = decided = None
    if best is None:
        s0 = time.perf_counter()
        best = router.best_route(prompt, routes, min_confidence=min_conf, min_margin=min_margin)
        step = {"step": "zero-shot match", "result": "suggested" if best else "no clear match"}
        if best:
            step.update(answer=best[0], confidence=round(best[1], 3))
            decided = "zero-shot match"
        step["ms"] = _ms(s0)
        steps.append(step)
    if not best:
        return done(None, steps=steps, decided_by=None, hint=None, options=len(routes))
    name, conf = best
    route = next((r for r in routes if r["name"] == name), None)
    if route is None:
        return done(None, steps=steps, decided_by=None, hint=None, options=len(routes))
    desc = (" (%s)" % route["description"][:120]) if route.get("description") else ""
    context = ("dev-decide routing hint: this request most closely matches the %s `%s`%s; match confidence %.2f."
               % (route["kind"], name, desc, conf))
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
    return done(out, steps=steps, decided_by=decided, hint=name, hint_kind=route["kind"], confidence=round(conf, 3),
                options=len(routes))


def hint(data):
    """The hook's JSON output for one prompt (a dict), or None to stay silent."""
    return explain(data)[0]


def note(ev, prompt):
    """Label a hint event with the task's type and area (from the prompt, which is not stored) and record it."""
    if not ev:
        return
    import traces
    ev = dict(ev)
    root, days = ev.pop("root"), ev.pop("keep_days", traces.KEEP_DAYS)
    if not ev.get("skipped"):
        try:
            import inputs
            import signals
            item = inputs.Item(None)
            item.kind, item.text = "text", user_text(prompt)
            F, _ = signals.analyze(item, [])
            ev["task_type"], ev["domain"] = traces.labels(item, F, root)
        except Exception:
            pass
    traces.record(root, ev, days)


def used(data):
    """A PostToolUse hook input for Claude Code's Agent or Skill tool -> recorded as what Claude really used: the
    agent type or skill name, the time it ran, and for an agent the start of its short task description (never
    the prompt Claude gave it, nor any result)."""
    tool = data.get("tool_name")
    if tool not in ("Agent", "Task", "Skill"):
        return
    import ddcore as dd
    import traces

    tin = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    if tool == "Skill":
        name = tin.get("skill") or tin.get("name") or tin.get("command")
    else:
        name = tin.get("subagent_type") or "general-purpose"
    if not isinstance(name, str) or not name.strip():
        return
    root = dd.project_root(data.get("cwd") or None)
    policy = dd.load_policy(root)
    if not traces.enabled(policy):
        return
    ms = data.get("duration_ms")
    ev = {"kind": "used", "session": data.get("session_id") or None, "tool": "Skill" if tool == "Skill" else "Agent",
          "name": name.strip().lstrip("/")[:80], "ms": ms if isinstance(ms, (int, float)) else None}
    if tool != "Skill":
        ev["text"] = traces.preview(tin.get("description"), traces.prompt_chars(policy))
    traces.record(root, ev, traces.keep_days(policy))


def main():
    if os.environ.get("DEV_DECIDE_CHILD"):
        return
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    if data.get("hook_event_name") == "PostToolUse":
        return used(data)
    out, ev = explain(data)
    if out:
        print(json.dumps(out))
        sys.stdout.flush()
    note(ev, data.get("prompt"))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a hook must never break the session
    sys.exit(0)
