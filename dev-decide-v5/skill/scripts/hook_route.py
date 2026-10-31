#!/usr/bin/env python3
"""UserPromptSubmit hook: suggest the best-matching agent or skill, locally, in milliseconds.

Installed only with `install.py --router-hook`. For every prompt it scores your agents
and skills, Claude Code's built-in agents (Explore, Plan, general-purpose) included, and
only when the match is clear adds one line of context such as:

  dev-decide routing hint: closest skill is `db-migrate`
  (Write and review Prisma database migrations safely) (confidence 0.86).

What decides, in order: what Claude ran after similar prompts in this project (usage.py,
the hint server only), the trained route model, then zero-shot matching on names and
descriptions. A hint that Claude has kept ignoring in this project needs 0.9 confidence.

It never blocks a prompt, never calls a model, writes nothing to the repository, and stays
silent for slash commands, short prompts, unclear matches and messages no person typed
(background-task notices, messages between agents). Tune it in .claude/dev-decide.json:
  "router_hook": {"min_confidence": 0.6, "min_margin": 0.2, "builtins": true, "learn": true, "enabled": true}

For the decision graph it notes what it decided (each step's answer, confidence and
time, the task's type and area, and the prompt's first 40 characters) in
~/.claude/dev-decide/traces (see traces.py); "decision_graph": {"enabled": false} turns
that off, "prompt_chars": 0 keeps no prompt text. As a PostToolUse hook it notes the
agent or skill Claude ran; as a SubagentStop hook, how long an agent really ran; as a Stop
hook, how long the reply took, which tools Claude used (names and counts only) and the tokens it spent (the
counts Claude Code already writes in its transcript, never text). As a PreToolUse hook it runs the token
guard (guard.py).
"""
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

STRICT_AFTER = 5         # uses after a hint before its follow rate counts
STRICT_BELOW = 0.2       # a hint followed less often than this ...
STRICT_CONFIDENCE = 0.9  # ... needs this much confidence from then on
LEARN_WINDOW_S = 2 * 3600


class _Hint(object):
    kind = "choose"

    def __init__(self, probs):
        self.probs = probs


SEARCH_AGENTS = ("Explore", "Plan")  # built-in agents that read many files and return only a conclusion


def _ms(t0):
    return round((time.perf_counter() - t0) * 1000, 1)


# What an editor or Claude Code adds around the typed words (the open file, the selection, reminders): not the
# request, so it must not be routed on, counted as words, or shown as the prompt.
_CONTEXT_TAGS = re.compile(r"<(ide_[a-z_]+|system-reminder)\b[^>]*>.*?</\1>", re.S)
_COMMENTS = re.compile(r"<!--.*?-->", re.S)
_PASTED = re.compile(r"<pasted_content\b[^>]*>(.*?)</pasted_content>", re.S)
# Messages no person typed, delivered as a prompt: a background task's notice, a message between agents.
_AUTOMATED = re.compile(r"^\s*<(task-notification|agent-message|teammate-message|channel-message|local-command-[a-z]+)\b")


def automated(prompt):
    return bool(_AUTOMATED.match(_CONTEXT_TAGS.sub(" ", prompt or "")))


def user_text(prompt):
    """The words a person typed: no editor context, no comments; a paste counts only when they typed little else."""
    t = _COMMENTS.sub(" ", _CONTEXT_TAGS.sub(" ", prompt or ""))
    own = _PASTED.sub(" ", t).strip()
    if len(own.split()) < 4:
        own = _PASTED.sub(lambda m: " " + m.group(1) + " ", t).strip()
    return re.sub(r"^(>\s*)+", "", own).strip()  # a quoted reply's leading markers


def _route_registry(root, routes):
    """The route model to ask, read only: the project's own, unless it is an older copy of the shipped one."""
    import fastmodel
    names = [r["name"] for r in routes]
    reg = fastmodel.Registry(root)
    if reg.enabled("route"):
        return fastmodel.current_starter(reg, "route", names) or reg, "trained model"
    return fastmodel.optional_registry(names) or reg, "trained model"


def _decide(text, routes, root, policy, cfg, memory=True):
    """Which agent or skill fits `text`: (best (name, confidence) or None, what decided, the steps tried, silent),
    where silent means the trained model said no agent or skill fits. memory: ask the usage memory first (what Claude
    ran after similar requests); a sub-task is judged without it, since it would only repeat Claude's own choices."""
    import router
    names = set(r["name"] for r in routes)
    min_conf = float(cfg.get("min_confidence", 0.6))
    min_margin = float(cfg.get("min_margin", 0.2))
    steps = []
    best = decided = None
    if memory:
        try:  # 1. what Claude ran after prompts like this one, in this project
            import usage
            if usage.enabled(policy) and usage.path(root).exists():
                s0 = time.perf_counter()
                m = router._meaning_model()
                hit = usage.suggest(root, m.encode(text), names) if m is not None else None
                step = {"step": "your past sessions", "result": "no similar requests yet"}
                if hit:
                    best, decided = (hit[0], hit[1]), "your past sessions"
                    step.update(answer=hit[0], confidence=hit[1], result="suggested (%d similar requests)" % hit[2])
                step["ms"] = _ms(s0)
                steps.append(step)
        except Exception:
            best = decided = None
    if best is None:
        try:  # 2. the trained route model, when enabled and confident
            reg, label = _route_registry(root, routes)
            if reg.enabled("route"):
                s0 = time.perf_counter()
                import fastmodel
                import inputs  # imported only when needed: the analyzers compile many patterns
                import signals
                item = inputs.Item(None)
                item.kind, item.text = "text", text
                F, _ = signals.analyze(item, [])
                ans = reg.answer("route", "choose", fastmodel.Features(item, F))
                step = {"step": label, "result": "not sure enough"}
                if ans:
                    probs = ans["probs"]
                    top = max((k for k in probs if k != "_rest"), key=probs.get, default=None)
                    step.update(answer=top, confidence=round(probs[top], 3) if top else None)
                    # an agent or skill the model has never seen may fit better: let the zero-shot match decide
                    import learn
                    zs = router.route(text, routes)
                    if zs and learn.unknown_option_hint(reg, "route", _Hint(zs[0])):
                        ans = None
                        step["result"] = "a newer agent or skill may fit: asked the zero-shot match"
                if ans:
                    if top is None or top == "none" or probs[top] < min_conf:
                        step.update(result="no agent or skill fits" if top in (None, "none") else "below min confidence",
                                    ms=_ms(s0))
                        steps.append(step)
                        return None, label, steps, True
                    best, decided = (top, probs[top]), label
                    step["result"] = "suggested"
                step["ms"] = _ms(s0)
                steps.append(step)
        except Exception:
            best = decided = None
    if best is None:  # 3. zero-shot: names and descriptions, by meaning and by area
        s0 = time.perf_counter()
        best = router.best_route(text, routes, min_confidence=min_conf, min_margin=min_margin)
        step = {"step": "zero-shot match", "result": "suggested" if best else "no clear match"}
        if best:
            step.update(answer=best[0], confidence=round(best[1], 3))
            decided = "zero-shot match"
        step["ms"] = _ms(s0)
        steps.append(step)
    return best, decided, steps, False


def explain(data):
    """(the hook's JSON output for one prompt or None to stay silent, the trace event or None). The routing hint
    (_explain) plus, when "files_hint" is on, a line naming the files edited after similar requests."""
    out, ev = _explain(data)
    raw = data.get("prompt") or ""
    prompt = user_text(raw)
    if (ev or {}).get("skipped") or not prompt or automated(raw) or prompt.startswith("/") \
            or not 4 <= len(prompt.split()) or len(prompt) > 4000:
        return out, ev
    try:
        import ddcore as dd
        root = dd.project_root(data.get("cwd") or None)
        line = files_note(root, dd.load_policy(root), prompt)
    except Exception:
        line = None
    if line:
        hook = (out or {}).get("hookSpecificOutput") or {"hookEventName": "UserPromptSubmit", "additionalContext": ""}
        hook["additionalContext"] = (hook["additionalContext"] + " " + line).strip()
        out = {"hookSpecificOutput": hook}
        if ev is not None:
            ev["files_hint"] = True
    return out, ev


def _explain(data):
    """(the hook's JSON output for one prompt or None to stay silent, the trace event or None). The event holds
    no prompt text; note() adds the task's labels and stores it. Shared with hookd.py."""
    t0 = time.perf_counter()
    raw = data.get("prompt") or ""
    prompt = user_text(raw)
    if not prompt:
        return None, None
    import ddcore as dd
    import traces

    root = dd.project_root(data.get("cwd") or None)
    policy = dd.load_policy(root)
    is_auto = automated(raw)
    ev = {"kind": "hint", "ts": int(time.time() * 1000), "session": data.get("session_id") or None,
          "prompt_id": data.get("prompt_id") or None,
          "text": None if is_auto else traces.preview(prompt, traces.prompt_chars(policy)), "root": str(root),
          "keep_days": traces.keep_days(policy)} if traces.enabled(policy) else None

    def done(out, **facts):
        if ev is not None:
            ev.update(facts, ms=_ms(t0))
        return out, ev

    cfg = policy.get("router_hook") or {}
    if is_auto:
        return done(None, skipped="automated message")
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
    routes = dd.discover_routes(root, policy, include_builtins=bool(cfg.get("builtins", True)))
    if not routes:
        return done(None, skipped="no agents or skills")
    best, decided, steps, silent = _decide(prompt, routes, root, policy, cfg, memory=True)
    if silent:  # the trained model says no agent or skill fits these words: no hint at all
        return done(None, steps=steps, decided_by=decided, hint=None, options=len(routes))
    if best and decided != "your past sessions":  # a hint Claude keeps ignoring here must be very sure
        st = traces.follow_stats(root).get(best[0]) if traces.enabled(policy) else None
        if st and st[1] >= STRICT_AFTER and st[0] < STRICT_BELOW * st[1] and best[1] < STRICT_CONFIDENCE:
            steps.append({"step": "your history", "answer": best[0], "result": "Claude used it after %d of %d such hints: "
                          "needs %.1f" % (st[0], st[1], STRICT_CONFIDENCE), "ms": 0.0})
            return done(None, steps=steps, decided_by="your history", hint=None, options=len(routes))
    if not best:
        return done(None, steps=steps, decided_by=None, hint=None, options=len(routes))
    name, conf = best
    route = next((r for r in routes if r["name"] == name), None)
    if route is None:
        return done(None, steps=steps, decided_by=None, hint=None, options=len(routes))
    desc = (" (%s)" % route["description"][:120]) if route.get("description") else ""
    context = ("dev-decide routing hint: closest %s is `%s`%s (confidence %.2f)." % (route["kind"], name, desc, conf))
    if name in SEARCH_AGENTS:  # an agent's file reads stay out of the main conversation: that is the token saving
        context += " Delegating keeps its file reads out of your context."
    if decided == "your past sessions":
        context += " Claude ran it after similar requests in this project."
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
    traces.follow_stats(root, refresh=True)  # recount for the next prompt, now that this one has its answer


def _used_name(data):
    tool = data.get("tool_name")
    if tool not in ("Agent", "Task", "Skill"):
        return None, None, None
    tin = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    name = (tin.get("skill") or tin.get("name") or tin.get("command")) if tool == "Skill" else \
        (tin.get("subagent_type") or "general-purpose")
    if not isinstance(name, str) or not name.strip():
        return None, None, None
    return ("Skill" if tool == "Skill" else "Agent"), name.strip().lstrip("/")[:80], tin


def used(data):
    """A PostToolUse hook input for Claude Code's Agent or Skill tool -> recorded as what Claude really used: the
    agent type or skill name, how long the call took (for a background agent only its launch; SubagentStop gives the
    run), and for an agent the start of its short task description (never the prompt Claude gave it, nor any
    result)."""
    if data.get("tool_name") == "TodoWrite":
        return todos(data)
    tool, name, tin = _used_name(data)
    if not name:
        return
    import ddcore as dd
    import traces

    root = dd.project_root(data.get("cwd") or None)
    policy = dd.load_policy(root)
    if not traces.enabled(policy):
        return
    ms = data.get("duration_ms")
    ev = {"kind": "used", "session": data.get("session_id") or None, "prompt_id": data.get("prompt_id") or None,
          "tool": tool, "name": name, "ms": ms if isinstance(ms, (int, float)) else None,
          "tool_use_id": data.get("tool_use_id") or None,
          # inside an agent, the hook says which one: that agent launched this one (the tree's parent)
          "caller": data.get("agent_id") or None, "caller_type": data.get("agent_type") or None}
    if tool != "Skill":
        ev["text"] = traces.preview(tin.get("description"), traces.prompt_chars(policy))
        task = subtask_text(tin)
        if task:
            try:
                ev["judge"] = judge(root, policy, task, name)
            except Exception:
                pass
            learn_subtask(root, policy, task, name)
    traces.record(root, ev, traces.keep_days(policy))


def todos(data):
    """A PostToolUse hook input for TodoWrite -> a snapshot of Claude's plan: each step's subject (as much as a
    prompt keeps) and status. Successive snapshots give when each step started and finished."""
    import ddcore as dd
    import traces

    tin = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    items = tin.get("todos")
    if not isinstance(items, list):
        return
    root = dd.project_root(data.get("cwd") or None)
    policy = dd.load_policy(root)
    if not traces.enabled(policy):
        return
    n = max(traces.prompt_chars(policy), 0)
    steps = [{"subject": traces.preview(str(t.get("content") or t.get("subject") or ""), n), "status": str(t.get("status") or "")[:20]}
             for t in items[:40] if isinstance(t, dict)]
    traces.record(root, {"kind": "todos", "session": data.get("session_id") or None, "prompt_id": data.get("prompt_id") or None,
                         "agent_id": data.get("agent_id") or None, "items": steps}, traces.keep_days(policy))


def subtask_text(tin):
    """The words of a sub-task Claude gave an agent: its short description and the start of its prompt. Used here,
    in memory, to judge and to learn; never stored."""
    parts = [str(tin.get(k) or "").strip() for k in ("description", "prompt")]
    return ". ".join(p for p in parts if p)[:1500]


def judge(root, policy, text, used_name):
    """dev-decide's own pick for a sub-task Claude delegated, beside Claude's: {"agent", "confidence", "by", "agrees"}.
    It asks the trained route model and the zero-shot match, not the usage memory (that would repeat Claude's own
    choices); "agent" is None when no agent clearly fits."""
    import ddcore as dd
    cfg = policy.get("router_hook") or {}
    routes = dd.discover_routes(root, policy, include_builtins=bool(cfg.get("builtins", True)))
    if not routes:
        return None
    best, decided, _, _ = _decide(text, routes, root, policy, cfg, memory=False)
    sure = bool(best)
    if not best:  # below the hint's bar: still dev-decide's best guess, marked unsure (a judge interrupts nobody)
        import router
        ranked = router.route(text, routes)
        probs = {k: v for k, v in (ranked[0] if ranked else {}).items() if k not in ("none", "_rest")}
        if not probs:
            return {"agent": None, "by": decided or "no clear match"}
        top = max(probs, key=probs.get)
        best, decided = (top, probs[top]), "zero-shot best guess"
    return {"agent": best[0], "confidence": round(best[1], 3), "by": decided, "agrees": best[0] == used_name,
            "sure": sure}


def learn_subtask(root, policy, text, name):
    """A sub-task's own words are close to an agent's job description: teach the usage memory that they led to it."""
    try:
        import ddcore as dd
        import router
        import usage
        if not usage.enabled(policy):
            return
        cfg = policy.get("router_hook") or {}
        names = set(r["name"] for r in dd.discover_routes(root, policy, include_builtins=bool(cfg.get("builtins", True))))
        m = router._meaning_model()
        if name in names and m is not None:
            usage.record(root, m.encode(text), name)
    except Exception:
        pass


def learn_use(data, prompt, hinted_at):
    """Teach the usage memory that this session's latest prompt led Claude to the agent or skill in this PostToolUse
    input. `prompt` is that prompt's text, which the hint server holds in memory only; it is turned into a meaning
    vector here and never stored."""
    tool, name, _ = _used_name(data)
    if not name or time.time() - hinted_at > LEARN_WINDOW_S:
        return
    import ddcore as dd
    import router
    import usage

    root = dd.project_root(data.get("cwd") or None)
    policy = dd.load_policy(root)
    if not usage.enabled(policy):
        return
    cfg = policy.get("router_hook") or {}
    names = set(r["name"] for r in dd.discover_routes(root, policy, include_builtins=bool(cfg.get("builtins", True))))
    m = router._meaning_model()
    if name in names and m is not None:
        usage.record(root, m.encode(prompt), name)


EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_FILES_SEEN = set()
_SECRET_FILE = re.compile(r"(^|[/\\._-])(env|secret|secrets|credential|credentials|token|tokens|password|passwords|"
                          r"id_rsa|id_ed25519|pem|key|keys|pfx|p12|kdbx|htpasswd|netrc|npmrc|pypirc)($|[/\\._-])", re.I)


def files_enabled(policy):
    cfg = policy.get("files_hint")
    return isinstance(cfg, dict) and cfg.get("enabled") is True and usage_enabled(policy)


def usage_enabled(policy):
    import usage
    return usage.enabled(policy)


def learn_files(data, prompt, hinted_at):
    """Experimental ("files_hint": {"enabled": true}). Teach the usage memory that this session's latest prompt led
    Claude to edit the file in this PostToolUse input (Edit, Write, ...). Only the file's path relative to the project
    is kept, with the prompt's meaning vector, never any text; files outside the project, in .git, or with a name
    that suggests a secret (.env, keys, tokens, credentials) are never kept."""
    if data.get("tool_name") not in EDIT_TOOLS or time.time() - hinted_at > LEARN_WINDOW_S:
        return
    tin = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    target = tin.get("file_path") or tin.get("notebook_path")
    if not isinstance(target, str) or not target:
        return
    import ddcore as dd
    import router
    import usage

    root = dd.project_root(data.get("cwd") or None)
    policy = dd.load_policy(root)
    if not files_enabled(policy):
        return
    try:
        rel = os.path.relpath(os.path.join(str(data.get("cwd") or root), target), str(root)).replace(os.sep, "/")
    except ValueError:
        return
    if rel.startswith("..") or rel.split("/")[0] == ".git" or len(rel) > 200 or _SECRET_FILE.search(rel):
        return
    if (hinted_at, rel) in _FILES_SEEN:  # one lesson per prompt and file, however many edits it took
        return
    m = router._meaning_model()
    if m is not None:
        _FILES_SEEN.add((hinted_at, rel))
        if len(_FILES_SEEN) > 5000:
            _FILES_SEEN.clear()
        usage.record(root, m.encode(prompt), rel, ".files")


def files_note(root, policy, prompt):
    """A line naming the files Claude edited after similar past prompts here, or None. Experimental, off by default."""
    if not files_enabled(policy):
        return None
    import router
    import usage

    m = router._meaning_model()
    if m is None:
        return None
    found = [(f, n) for f, n in usage.suggest_files(root, m.encode(prompt)) if os.path.isfile(os.path.join(str(root), f))]
    if not found:
        return None
    return "After similar requests here you edited: %s." % ", ".join(f for f, _ in found)


def _iso_ms(stamp):
    return datetime.datetime.strptime(stamp.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z").timestamp() * 1000


def ran(data):
    """A SubagentStop hook input -> how long that agent really ran: from the first line of its transcript (only its
    timestamp is read) to now. Recorded as a "ran" event, which the views join to the matching use."""
    name = data.get("agent_type")
    if not isinstance(name, str) or not name.strip():
        return
    import ddcore as dd
    import traces

    root = dd.project_root(data.get("cwd") or None)
    policy = dd.load_policy(root)
    if not traces.enabled(policy):
        return
    ms, tools, used_tokens = None, {}, None
    try:
        with open(str(data.get("agent_transcript_path") or ""), encoding="utf-8") as fh:
            first = json.loads(fh.readline())
        ms = max(0, int(time.time() * 1000 - _iso_ms(first["timestamp"])))
        import tokens
        rows = _tail_rows(str(data.get("agent_transcript_path")))
        tools, used_tokens = _count_tools(rows), tokens.count(rows)
    except Exception:
        pass
    traces.record(root, {"kind": "ran", "session": data.get("session_id") or None, "agent_id": data.get("agent_id") or None,
                         "prompt_id": data.get("prompt_id") or None, "name": name.strip()[:80], "ms": ms, "tools": tools,
                         "tokens": used_tokens},
                  traces.keep_days(policy))


def started(data):
    """A SubagentStart hook input -> the agent's id as it starts, so the agents it launches nest under it while a long
    build is still running."""
    name = data.get("agent_type")
    if not isinstance(name, str) or not name.strip() or not data.get("agent_id"):
        return
    import ddcore as dd
    import traces

    root = dd.project_root(data.get("cwd") or None)
    policy = dd.load_policy(root)
    if traces.enabled(policy):
        traces.record(root, {"kind": "start", "session": data.get("session_id") or None, "agent_id": data["agent_id"],
                             "prompt_id": data.get("prompt_id") or None, "name": name.strip()[:80]},
                      traces.keep_days(policy))


def task(data):
    """A TaskCreated or TaskCompleted hook input -> a step of Claude's plan (its task list): its id, the start of its
    subject (as much as a prompt keeps), and when it was created or done."""
    event = data.get("hook_event_name")
    if event not in ("TaskCreated", "TaskCompleted") or not data.get("task_id"):
        return
    import ddcore as dd
    import traces

    root = dd.project_root(data.get("cwd") or None)
    policy = dd.load_policy(root)
    if traces.enabled(policy):
        traces.record(root, {"kind": "task", "event": "created" if event == "TaskCreated" else "completed",
                             "task_id": str(data["task_id"])[:80], "session": data.get("session_id") or None,
                             "prompt_id": data.get("prompt_id") or None, "agent_id": data.get("agent_id") or None,
                             "subject": traces.preview(data.get("task_subject"), max(traces.prompt_chars(policy), 0)),
                             "teammate": data.get("teammate_name") or None}, traces.keep_days(policy))


TAIL_BYTES = 4 << 20  # the end of a transcript is enough to find the latest prompt and the reply to it


def _prompt_line(r):
    """A transcript line that is a prompt a person typed: not a tool result, not Claude Code's own note, and not a
    notice (a background agent's end arrives as one, and Claude's reply to it continues the same job)."""
    if r.get("type") != "user" or r.get("isMeta"):
        return False
    c = (r.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return False
        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return isinstance(c, str) and bool(c.strip()) and not automated(c)


def turn(data):
    """A Stop hook input -> the end of Claude's reply to the latest prompt: how long the reply took and which tools it
    used, by name and count only (read from the transcript's tail; never their inputs, results or the text). The
    views join it to its prompt, so a prompt no agent handled shows "Claude itself" and what it did."""
    import ddcore as dd
    import traces

    root = dd.project_root(data.get("cwd") or None)
    policy = dd.load_policy(root)
    if not traces.enabled(policy):
        return
    try:
        with open(str(data.get("transcript_path") or ""), "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return
    if size > TAIL_BYTES:
        lines = lines[1:]  # the first line is cut
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    start = max((i for i, r in enumerate(rows) if _prompt_line(r)), default=None)
    if start is None:
        return
    try:
        began = _iso_ms(rows[start]["timestamp"])
    except (KeyError, ValueError):
        return
    reply = rows[start + 1:]
    import tokens
    traces.record(root, {"kind": "turn", "session": data.get("session_id") or None,
                         "prompt_id": rows[start].get("promptId") or data.get("prompt_id") or None, "prompt_ts": int(began),
                         "ms": max(0, int(time.time() * 1000 - began)), "tools": _count_tools(reply),
                         # an agent's own calls are counted from its transcript (ran), not twice
                         "tokens": tokens.count([r for r in reply if not r.get("isSidechain")])},
                  traces.keep_days(policy))


INSTANT_TIMEOUT_S = 50
_INSTANT = re.compile(r"/(?:dev-decide:)?dev-decide\s+(.+)\Z", re.S)


def instant(data):
    """A UserPromptSubmit hook input -> the hook's JSON output for a prompt that starts with `/dev-decide `, answered
    here instead of by Claude (so that turn costs no model tokens), or None to let Claude handle the prompt as usual.
    Off unless "instant": {"enabled": true} is set; it runs the engine as `decide.py --summary` (the same local rules
    and, when needed, the same one model call as the skill) and shows the one-line answer to the person."""
    prompt = data.get("prompt")
    m = _INSTANT.match(prompt.strip()) if isinstance(prompt, str) else None
    if not m or len(prompt) > 20000:
        return None
    import ddcore as dd
    import subprocess

    root = dd.project_root(data.get("cwd") or None)
    cfg = dd.load_policy(root).get("instant")
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return None
    cwd = str(root) if os.path.isdir(str(root)) else None
    try:
        p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "decide.py"), "--summary", "--cwd",
                            cwd or ".", "-"], input=m.group(1).strip(), capture_output=True, text=True, cwd=cwd,
                           timeout=INSTANT_TIMEOUT_S, env=dict(os.environ, DEV_DECIDE_CHILD="1"))
    except (OSError, subprocess.SubprocessError):
        return None
    answer = (p.stdout or "").strip()
    if p.returncode != 0 or not answer or answer.startswith("dev-decide error") or re.search(r"\| [\w.]+ error", answer):
        return None  # anything unexpected: let Claude run the skill as usual
    return {"decision": "block", "reason": "dev-decide answered this itself, so Claude was not used (no tokens spent): " + answer,
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": True}}


def guard(data):
    """A PreToolUse hook input -> (the hook's JSON output or None, a trace event or None): a note before a wasteful
    Read or Bash call (see guard.py). Never raises into the caller's answer: any failure means "no opinion"."""
    import ddcore as dd
    import guard as token_guard
    import traces

    root = dd.project_root(data.get("cwd") or None)
    policy = dd.load_policy(root)
    cfg = token_guard.config(policy)
    if not cfg["enabled"]:
        return None, None
    out, ev = token_guard.check(data, cfg)
    if ev and traces.enabled(policy):
        ev = dict(ev, root=str(root), keep_days=traces.keep_days(policy))
    else:
        ev = None
    return out, ev


def _count_tools(rows):
    from collections import Counter
    tools = Counter()
    for r in rows:
        if r.get("type") == "assistant":
            for b in (r.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("name"), str):
                    tools[b["name"][:60]] += 1
    return dict(tools.most_common(20))


def _tail_rows(path):
    """The parsed lines of a transcript's tail (an agent's whole transcript, usually)."""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - TAIL_BYTES))
        lines = fh.read().decode("utf-8", "replace").splitlines()
    rows = []
    for line in lines[1:] if size > TAIL_BYTES else lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def _tools_after(path, start):
    """Tool names and counts in a transcript's tail (an agent's whole transcript, usually)."""
    return _count_tools(_tail_rows(path)[start:])


def main():
    if os.environ.get("DEV_DECIDE_CHILD"):
        return
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    event = data.get("hook_event_name")
    if event == "PreToolUse":  # command form: no memory between calls, so only the rules that need none
        out, ev = guard(data)
        if out:
            print(json.dumps(out))
            sys.stdout.flush()
        if ev:
            import traces
            ev = dict(ev)
            root, days = ev.pop("root"), ev.pop("keep_days", traces.KEEP_DAYS)
            traces.record(root, ev, days)
        return
    if event == "PostToolUse":
        return used(data)
    if event == "SubagentStop":
        return ran(data)
    if event == "SubagentStart":
        return started(data)
    if event in ("TaskCreated", "TaskCompleted"):
        return task(data)
    if event == "Stop":
        return turn(data)
    out = instant(data)
    if out:
        print(json.dumps(out))
        sys.stdout.flush()
        return
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
