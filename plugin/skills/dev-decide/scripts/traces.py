"""A local record of what dev-decide decided, for the decision graph (hookd.py's /graph page and the side panel).

Three kinds of event, one JSON line each, in ~/.claude/dev-decide/traces/<project>.jsonl (outside the repository, so
the routing hint still writes nothing there; one file per project):

  hint      the routing hint for one prompt: each step tried (trained model, zero-shot match) with its answer,
            confidence and milliseconds, and the agent or skill suggested, or none and why
  decision  one /dev-decide answer: the questions, where each answer came from, the gates, the time it took
  used      an agent or skill Claude actually ran (Claude Code's Agent and Skill tools), and for how long

Of a prompt, only its first "prompt_chars" characters (default 40; 0 keeps none) are kept, with anything that looks
like a key or token masked, so the timeline can show which request it was. A task is labeled by its type and
technical area (for example "bugfix · database"), decided locally. Events older than "keep_days" are dropped, at most
once a day per file. summary() turns events into the graph and the timeline both views draw. Turn it off with
"decision_graph": {"enabled": false} in .claude/dev-decide.json or DEV_DECIDE_TRACE=0 (serve.py sets that when it
listens beyond this machine); DEV_DECIDE_TRACE_DIR moves the folder.
"""
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

import ddcore as dd

KEEP_DAYS = 30
PROMPT_CHARS = 40
TIMELINE = 60
_TOKENISH = re.compile(r"[A-Za-z0-9_\-+/=.]{20,}")  # keys, tokens, long hashes: never kept, even in a short preview
_LOCK = threading.Lock()
_PRUNED = {}  # file -> day it was last pruned by this process


def folder():
    env = os.environ.get("DEV_DECIDE_TRACE_DIR")
    return Path(env) if env else dd.home_claude() / "dev-decide" / "traces"


def enabled(policy):
    if os.environ.get("DEV_DECIDE_TRACE", "").lower() in ("0", "false", "off", "no"):
        return False
    return (policy.get("decision_graph") or {}).get("enabled", True) is not False


def keep_days(policy):
    try:
        return max(1, int((policy.get("decision_graph") or {}).get("keep_days", KEEP_DAYS)))
    except (TypeError, ValueError):
        return KEEP_DAYS


def prompt_chars(policy):
    try:
        return max(0, min(200, int((policy.get("decision_graph") or {}).get("prompt_chars", PROMPT_CHARS))))
    except (TypeError, ValueError):
        return PROMPT_CHARS


def preview(text, n):
    """The first n characters of a text on one line, keys and tokens masked; None when n is 0 or there is no text."""
    if not n or not isinstance(text, str):
        return None
    one = _TOKENISH.sub("[…]", " ".join(text.split()))  # masked before the cut, so no fragment of a key is kept
    if not one:
        return None
    return one if len(one) <= n else one[:n].rstrip() + "…"


def slug(root):
    """A file name for a project: its folder name and a short hash of its full path."""
    p = str(Path(root).resolve())
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(p).name)[:40] or "project"
    return "%s-%s" % (name, hashlib.sha256(p.lower().encode("utf-8")).hexdigest()[:8])


def path(root):
    return folder() / ("%s.jsonl" % slug(root))


def _top(probs, skip=("_rest", "none")):
    return max((k for k in probs if k not in skip), key=probs.get, default=None) if probs else None


def labels(item, facts, root=None):
    """(task type, technical area) of an input, decided locally (the trained domain model when it is sure, else
    keyword signals); either may be None. Reads models, never writes them."""
    import signals
    task = area = None
    try:
        a = signals.decide("task_type", facts, item)
        task = _top(a.probs) if a is not None else None
    except Exception:
        pass
    try:
        import fastmodel as fm
        reg = fm.Registry(root) if root is not None else None
        if reg is None or not reg.enabled("domain"):  # not seeded here yet: the shipped model, read only
            reg = fm.Registry(None, models_dir=dd.SKILL_DIR / "starter-models")
        ans = reg.answer("domain", "choose", fm.Features(item, facts)) if reg.enabled("domain") else None
        area = _top(ans["probs"]) if ans else None
    except Exception:
        pass
    if area is None:
        try:
            a = signals.decide("domain", facts, item)
            area = _top(a.probs) if a is not None else None
        except Exception:
            pass
    return task, area


def record(root, event, days=KEEP_DAYS):
    """Append one event (a dict) for a project. Never raises: a trace must not break a decision."""
    try:
        p = path(root)
        event = dict(event, project=Path(str(root)).name)
        event.setdefault("ts", int(time.time() * 1000))  # a hint carries the time its prompt arrived
        line = json.dumps(event, separators=(",", ":")) + "\n"
        with _LOCK:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(str(p), "a", encoding="utf-8") as fh:
                fh.write(line)
            _index(root)
            _prune(p, days)
    except Exception:
        pass


def _index(root):
    """Remember which project each trace file belongs to (for the page's project list)."""
    idx_path = folder() / "index.json"
    idx = dd.read_json(idx_path, default={}) or {}
    key = slug(root)
    now = int(time.time() * 1000)
    entry = idx.get(key) or {}
    if entry.get("root") != str(root) or now - int(entry.get("last", 0)) > 60000:
        idx[key] = {"root": str(root), "name": Path(str(root)).name, "last": now}
        dd.write_json(idx_path, idx, indent=1)


def _prune(p, days=KEEP_DAYS):
    today = time.strftime("%Y-%m-%d")
    if _PRUNED.get(str(p)) == today:
        return
    _PRUNED[str(p)] = today
    cutoff = (time.time() - days * 86400) * 1000
    rows = dd.read_jsonl(p)
    kept = [r for r in rows if r.get("ts", 0) >= cutoff]
    if len(kept) < len(rows):
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in kept), encoding="utf-8")
        os.replace(str(tmp), str(p))


def projects():
    """Projects with a trace, most recently active first."""
    idx = dd.read_json(folder() / "index.json", default={}) or {}
    out = [dict(v, id=k) for k, v in idx.items() if (folder() / ("%s.jsonl" % k)).exists()]
    return sorted(out, key=lambda r: -int(r.get("last", 0)))


def load(project_id, days=1):
    """Events of one project (by its id, from projects()) within the last `days` days, oldest first."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+-[0-9a-f]{8}", project_id or ""):
        return []
    cutoff = (time.time() - float(days) * 86400) * 1000
    return [r for r in dd.read_jsonl(folder() / ("%s.jsonl" % project_id)) if r.get("ts", 0) >= cutoff]


def task_label(e):
    parts = [x for x in (e.get("task_type"), e.get("domain")) if x]
    return " · ".join(parts) if parts else "unclassified"


def _start(e):
    """When an event began: a tool's end time minus how long it ran."""
    return e.get("ts", 0) - int(e.get("ms") or 0) if e.get("kind") == "used" else e.get("ts", 0)


BUCKETS = 12


def series(events, span_ms, now_ms):
    """Activity over the period in BUCKETS equal slices, oldest first, for the sparklines."""
    step = max(1, int(span_ms // BUCKETS))
    start = now_ms - step * BUCKETS
    out = {"start": start, "step_ms": step, "prompts": [0] * BUCKETS, "hints": [0] * BUCKETS,
           "used": [0] * BUCKETS, "decisions": [0] * BUCKETS}
    for e in events:
        i = (e.get("ts", 0) - start) // step
        if not 0 <= i < BUCKETS:
            continue
        kind = e.get("kind")
        if kind == "hint" and not e.get("skipped"):
            out["prompts"][i] += 1
            out["hints"][i] += bool(e.get("hint"))
        elif kind == "hint" and e.get("command") or kind == "used":
            out["used"][i] += 1
        elif kind == "decision":
            out["decisions"][i] += 1
    return out


def summary(events, span_ms=None, now_ms=None):
    """The graph (nodes in four columns and weighted edges), the timeline and totals, from a project's events, and
    with span_ms the activity over that period (BUCKETS slices ending now_ms, default now).

    Columns: the task (type · area) -> what decided (the step that answered, or the /dev-decide questions) -> the
    suggestion (an agent or skill, a gate verdict, or none) -> what Claude really used after it in that session.
    """
    events = sorted(events, key=_start)
    hints = [e for e in events if e.get("kind") == "hint" and not e.get("skipped")]
    decisions = [e for e in events if e.get("kind") == "decision"]
    used = [dict(e) for e in events if e.get("kind") == "used"]
    # typing /skill runs a skill without the Skill tool: the prompt hook noted its name
    used += [{"kind": "used", "tool": "typed", "name": e["command"], "ts": e.get("ts", 0), "session": e.get("session"),
              "text": e.get("text")} for e in events if e.get("kind") == "hint" and e.get("command")]
    used.sort(key=_start)
    # each use of an agent or skill belongs to the latest routing hint before it in the same session
    by_session = {}
    for h in hints:
        by_session.setdefault(h.get("session") or "", []).append(h)
    links, alone = {}, []
    for u in used:
        prior = [h for h in by_session.get(u.get("session") or "", []) if h.get("ts", 0) <= _start(u)]
        if u.get("tool") != "typed" and prior:
            h = prior[-1]
            if h.get("hint"):
                u["followed"] = h["hint"] == u.get("name")
            links.setdefault(id(h), []).append(u)
        else:
            alone.append(u)
    nodes, edges, paths = {}, {}, {}

    def path(task, decided, suggested, rows, ms):
        u = ", ".join(dict.fromkeys(r.get("name") or "?" for r in rows)) if rows else None
        p = paths.setdefault((task, decided, suggested, u), {"task": task, "decided": decided, "suggested": suggested,
                                                             "used": u, "count": 0, "ms": 0.0, "timed": 0})
        p["count"] += 1
        if ms is not None:
            p["ms"] += float(ms)
            p["timed"] += 1

    def node(col, name, ms=None):
        key = "%d:%s" % (col, name)
        n = nodes.setdefault(key, {"id": key, "col": col, "name": name, "count": 0, "ms": 0.0, "timed": 0})
        n["count"] += 1
        if ms is not None:
            n["ms"] += float(ms)
            n["timed"] += 1
        return key

    def edge(a, b, followed=None):
        e = edges.setdefault((a, b), {"from": a, "to": b, "count": 0})
        e["count"] += 1
        if followed is not None:
            e["followed"] = followed  # Claude used what was suggested (or did not)

    for h in hints:
        t = node(0, task_label(h))
        d = node(1, h.get("decided_by") or "no clear match", h.get("ms"))
        s = node(2, h.get("hint") or "no hint")
        edge(t, d)
        edge(d, s)
        for u in links.get(id(h), []):
            edge(s, node(3, "used " + (u.get("name") or "?"), u.get("ms")), u.get("followed"))
        path(task_label(h), h.get("decided_by") or "no clear match", h.get("hint") or "no hint",
             links.get(id(h), []), h.get("ms"))
    for u in alone:
        typed = u.get("tool") == "typed"
        task, decided = ("typed /command", "your choice") if typed else ("no routing hint ran", "Claude's own choice")
        t = node(0, task)
        d = node(1, decided)
        s = node(2, "not suggested")
        edge(t, d)
        edge(d, s)
        edge(s, node(3, "used " + (u.get("name") or "?"), u.get("ms")))
        path(task, decided, "not suggested", [u], None)
    for dcs in decisions:
        t = node(0, task_label(dcs))
        d = node(1, "/dev-decide " + (",".join(dcs.get("bundles") or dcs.get("questions") or ["?"]))[:48], dcs.get("ms"))
        verdicts = sorted(set((dcs.get("gates") or {}).values()))
        s = node(2, ("gate: " + "/".join(verdicts)) if verdicts else "answered")
        edge(t, d)
        edge(d, s)
        path(task_label(dcs), nodes[d]["name"], nodes[s]["name"], [], dcs.get("ms"))
    for n in list(nodes.values()) + list(paths.values()):
        n["avg_ms"] = round(n["ms"] / n["timed"], 1) if n["timed"] else None
        del n["ms"], n["timed"]

    def uses(rows):
        return [{"name": u.get("name"), "tool": u.get("tool"), "ms": u.get("ms"), "followed": u.get("followed"),
                 "text": u.get("text")} for u in rows]

    timeline = []
    for e in sorted(hints + decisions + alone, key=lambda e: -e.get("ts", 0))[:TIMELINE]:
        item = {k: e.get(k) for k in ("kind", "ts", "task_type", "domain", "hint", "hint_kind", "decided_by", "steps",
                                      "ms", "confidence", "options", "questions", "bundles", "answers", "gates",
                                      "model_calls", "name", "tool", "text")}
        item = {k: v for k, v in item.items() if v is not None}
        if e.get("kind") != "used":
            item["used"] = uses(links.get(id(e), []))
        timeline.append(item)
    asked = [u for u in used if "followed" in u]
    out = {
        "nodes": sorted(nodes.values(), key=lambda n: (n["col"], -n["count"])),
        "paths": sorted(paths.values(), key=lambda p: (-p["count"], p["task"])),
        "edges": sorted(edges.values(), key=lambda e: -e["count"]),
        "timeline": timeline,
        "stats": {
            "prompts": len(hints), "hinted": sum(1 for h in hints if h.get("hint")),
            "skipped": sum(1 for e in events if e.get("kind") == "hint" and e.get("skipped")),
            "decisions": len(decisions), "used": len(used),
            "followed": sum(1 for u in asked if u["followed"]), "after_hint": len(asked),
            "avg_hint_ms": round(sum(float(h.get("ms") or 0) for h in hints) / len(hints), 1) if hints else None,
            "avg_decision_ms": round(sum(float(d.get("ms") or 0) for d in decisions) / len(decisions), 1)
            if decisions else None,
            "first": min(e.get("ts", 0) for e in events) if events else None,
            "last": max(e.get("ts", 0) for e in events) if events else None,
        },
    }
    if span_ms:
        out["series"] = series(events, span_ms, now_ms if now_ms is not None else int(time.time() * 1000))
    return out


COLUMNS = ("Task", "Decided by", "Suggested", "Claude used")
QUESTIONS = ("What kind of task?", "Who chose?", "What was suggested?", "What really ran?")
MAX_PER_COLUMN = 8
QUIET = ("no hint", "not suggested", "no clear match", "unclassified", "no routing hint ran")
# The validated categorical slots 1-4 (light on #ffffff, dark on #141821) for the four stages; status colors only for
# whether Claude followed a hint, always with a check or cross beside them; text in ink tokens, never a stage color.
PALETTE = {  # light, dark
    "bg": ("#ffffff", "#141821"), "card": ("#f7f8fa", "#1a1f2a"), "ink": ("#0e1117", "#eef1f6"),
    "ink2": ("#465063", "#b9c1ce"), "muted": ("#768092", "#8a93a4"), "line": ("#d3d8e1", "#323b4b"),
    "c0": ("#2a78d6", "#3987e5"), "c1": ("#eb6834", "#d95926"), "c2": ("#1baf7a", "#199e70"),
    "c3": ("#eda100", "#c98500"), "quiet": ("#a6adba", "#5b6474"), "good": ("#0ca30c", "#0ca30c"),
    "bad": ("#d03b3b", "#d03b3b"),
}


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _fit(s, n):
    s = str(s)
    return s if len(s) <= n else s[:max(1, n - 1)] + "…"


def ms_text(ms):
    if ms is None:
        return ""
    if ms >= 60000:
        return "%.1f min" % (ms / 60000.0)
    if ms >= 1000:
        return "%.1f s" % (ms / 1000.0)
    return "%.0f ms" % ms if ms >= 10 else "%.1f ms" % ms


def _limit(nodes, edges):
    """At most MAX_PER_COLUMN nodes a column; the rest merge into one 'N more' node."""
    keep, moved = {}, {}
    for col in range(4):
        cn = sorted((n for n in nodes if n["col"] == col), key=lambda n: -n["count"])
        if len(cn) > MAX_PER_COLUMN:
            rest = cn[MAX_PER_COLUMN - 1:]
            other = {"id": "%d:_more" % col, "col": col, "name": "%d more" % len(rest),
                     "count": sum(n["count"] for n in rest), "avg_ms": None}
            cn = cn[:MAX_PER_COLUMN - 1] + [other]
            for n in rest:
                moved[n["id"]] = other["id"]
        for n in cn:
            keep[n["id"]] = n
    merged = {}
    for e in edges:
        a, b = moved.get(e["from"], e["from"]), moved.get(e["to"], e["to"])
        m = merged.setdefault((a, b), {"from": a, "to": b, "count": 0, "followed": e.get("followed")})
        m["count"] += e["count"]
        if m.get("followed") != e.get("followed"):
            m["followed"] = None
    return list(keep.values()), list(merged.values())


def _css(p):
    return (".bg{fill:%(bg)s}.card{fill:%(card)s;stroke:%(line)s}.ink{fill:%(ink)s}.ink2{fill:%(ink2)s}"
            ".mut{fill:%(muted)s}.c0{fill:%(c0)s}.c1{fill:%(c1)s}.c2{fill:%(c2)s}.c3{fill:%(c3)s}.cq{fill:%(quiet)s}"
            ".s0{stroke:%(c0)s}.s1{stroke:%(c1)s}.s2{stroke:%(c2)s}.s3{stroke:%(c3)s}.sq{stroke:%(quiet)s}"
            ".sg{stroke:%(good)s}.sb{stroke:%(bad)s}.fg{fill:%(good)s}.fb{fill:%(bad)s}.ring{stroke:%(bg)s}"
            ".track{fill:%(line)s}" % p)


def _latest_line(s):
    """The latest routing hint or /dev-decide answer, as one sentence for the strip under the graph."""
    items = [t for t in s.get("timeline") or [] if t.get("kind") != "used"]
    if not items:
        return None
    it = items[0]
    parts = []
    if it.get("text"):
        parts.append("“%s”" % it["text"])
    parts.append(" · ".join(x for x in (it.get("task_type"), it.get("domain")) if x) or "unclassified")
    if it.get("kind") == "decision":
        parts.append("/dev-decide " + ",".join(it.get("bundles") or it.get("questions") or []))
        parts += ["gate %s: %s" % kv for kv in sorted((it.get("gates") or {}).items())]
    else:
        last = (it.get("steps") or [{}])[-1]
        if last.get("answer") and last.get("answer") != "none":
            parts.append("%s %d%%" % (last.get("step"), round(100 * (last.get("confidence") or 0))))
        parts.append(it.get("hint") or "no hint")
        for u in it.get("used") or []:
            parts.append("used %s%s" % (u.get("name"), " ✓" if u.get("followed") else " ✗" if u.get("followed") is False else ""))
    return " → ".join(parts) + "  (%s)" % ms_text(it.get("ms"))


def svg(s, theme="auto", width=960, animate=False, header=False):
    """The decision graph as one self-contained SVG (no script, no external files): four columns of boxes, links as
    thick as they were taken (green with a check: Claude used the suggested one; red with a cross: another), and a
    <title> on each for tooltips. theme: light, dark or auto. header adds the totals on top and the latest request
    underneath; animate sends dots along the busiest links (SVG's own animation, no script). Drawn by the
    dev-decide-graph side panel; the /graph page draws its own, interactive one."""
    try:
        width = max(420, min(int(width or 960), 2400))
    except (TypeError, ValueError):
        width = 960
    nodes, edges = _limit(s.get("nodes") or [], s.get("edges") or [])
    gap = max(26, width // 15)
    col_w = (width - 24 - 3 * gap) // 4
    chars = max(8, int((col_w - 22) / 6.8))
    node_h, vgap = 42, 9
    top = 12
    stats = s.get("stats") or {}
    if header:
        top += 74
    head_y = top
    top += 40
    pos, cols = {}, []
    for col in range(4):  # the first column by how often; the others by where their inputs sit (fewer crossings)
        cn = [n for n in nodes if n["col"] == col]
        more = lambda n: n["id"].endswith("_more")  # noqa: E731  ("N more" always sits last)
        if col == 0:
            cn.sort(key=lambda n: (more(n), -n["count"]))
        else:
            def bary(n):
                ins = [(pos[e["from"]], e["count"]) for e in edges if e["to"] == n["id"] and e["from"] in pos]
                w = sum(c for _, c in ins)
                return sum(p * c for p, c in ins) / float(w) if w else 1e9
            cn.sort(key=lambda n: (more(n), bary(n), -n["count"]))
        for i, n in enumerate(cn):
            pos[n["id"]] = i
        cols.append(cn)
    rows = max([len(c) for c in cols] + [1])
    plot_h = rows * (node_h + vgap)
    latest = _latest_line(s) if header else None
    height = top + (plot_h if nodes else 70) + (36 if latest else 6)
    xy = {}
    for col, cn in enumerate(cols):
        x = 12 + col * (col_w + gap)
        offset = (rows - len(cn)) * (node_h + vgap) / 2.0  # centre short columns
        for i, n in enumerate(cn):
            xy[n["id"]] = (x, top + offset + i * (node_h + vgap))
    light = {k: v[0] for k, v in PALETTE.items()}
    dark = {k: v[1] for k, v in PALETTE.items()}
    if theme == "dark":
        style = _css(dark)
    elif theme == "light":
        style = _css(light)
    else:
        style = _css(light) + "@media (prefers-color-scheme:dark){" + _css(dark) + "}"
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
           'font-family="system-ui,-apple-system,Segoe UI,sans-serif" font-size="12">' % (width, height, width, height),
           "<style>%s.n:hover .card{stroke-width:2}@media (prefers-reduced-motion:reduce){.dot{display:none}}</style>" % style,
           '<rect class="bg" width="100%" height="100%" rx="12"/>']
    if header:
        prompts, hinted = stats.get("prompts") or 0, stats.get("hinted") or 0
        fol, after = stats.get("followed") or 0, stats.get("after_hint") or 0
        cards = [("Prompts routed", str(prompts), "%d skipped" % (stats.get("skipped") or 0), None),
                 ("Hints given", str(hinted), "%d%% of prompts" % round(100.0 * hinted / prompts) if prompts else "–",
                  hinted / float(prompts) if prompts else 0),
                 ("Hint followed", "%d/%d" % (fol, after) if after else "–",
                  "%d%% of uses" % round(100.0 * fol / after) if after else "no uses yet", fol / float(after) if after else 0),
                 ("Avg hint time", ms_text(stats.get("avg_hint_ms")) or "–", "a blink is ~300 ms", None)]
        cw = (width - 24 - 3 * 10) / 4.0
        for i, (label, value, sub, share) in enumerate(cards):
            x = 12 + i * (cw + 10)
            out.append('<rect class="card" x="%.1f" y="12" width="%.1f" height="62" rx="10"/>' % (x, cw))
            out.append('<text class="ink2" x="%.1f" y="30" font-size="11.5">%s</text>' % (x + 12, _esc(label)))
            out.append('<text class="ink" x="%.1f" y="53" font-size="20" font-weight="700">%s</text>' % (x + 12, _esc(value)))
            if share is not None:
                out.append('<rect class="track" x="%.1f" y="62" width="%.1f" height="5" rx="2.5"/>' % (x + 12, cw - 24))
                out.append('<rect class="c0" x="%.1f" y="62" width="%.1f" height="5" rx="2.5"/>'
                           % (x + 12, max(2, (cw - 24) * max(0.0, min(1.0, share)))))
            else:
                out.append('<text class="mut" x="%.1f" y="67" font-size="10.5">%s</text>' % (x + 12, _esc(sub)))
    for col, label in enumerate(COLUMNS):
        x = 12 + col * (col_w + gap)
        out.append('<rect class="c%d" x="%d" y="%d" width="10" height="10" rx="3"/>' % (col, x, head_y + 4))
        out.append('<text class="ink" x="%d" y="%d" font-size="11.5" font-weight="650">%s</text>'
                   % (x + 16, head_y + 13, _esc(_fit(label.upper(), chars + 4))))
        out.append('<text class="mut" x="%d" y="%d" font-size="10.5">%s</text>'
                   % (x + 16, head_y + 27, _esc(_fit(QUESTIONS[col], chars + 6))))
    if not nodes:
        out.append('<text class="mut" x="%d" y="%d" text-anchor="middle">No decisions recorded in this period yet. '
                   'Send Claude a request, or run /dev-decide.</text>' % (width // 2, top + 30))
        out.append("</svg>")
        return "".join(out)
    names = {n["id"]: n["name"] for n in nodes}
    peak = max([e["count"] for e in edges] + [1])
    badges, dots = [], []
    for e in sorted(edges, key=lambda e: e["count"]):
        if e["from"] not in xy or e["to"] not in xy:
            continue
        (x1, y1), (x2, y2) = xy[e["from"]], xy[e["to"]]
        x1 += col_w
        y1 += node_h / 2.0
        y2 += node_h / 2.0
        a, b = names[e["from"]], names[e["to"]]
        col = int(e["from"].split(":", 1)[0])
        followed = e.get("followed")
        if followed is True:
            cls = "sg"
        elif followed is False:
            cls = "sb"
        else:
            cls = "sq" if a in QUIET or e["from"].endswith("_more") else "s%d" % col
        mid = (x1 + x2) / 2.0
        d = "M%.1f %.1fC%.1f %.1f %.1f %.1f %.1f %.1f" % (x1, y1, mid, y1, mid, y2, x2, y2)
        tip = "%s → %s: %d×%s" % (a, b, e["count"], "" if followed is None else
                                           " (Claude used the suggested one)" if followed else " (Claude used another)")
        out.append('<path class="%s" d="%s" fill="none" stroke-width="%.1f" stroke-opacity="%s"><title>%s</title></path>'
                   % (cls, d, 1.5 + 9.0 * e["count"] / peak, ".6" if followed is not None else ".3", _esc(tip)))
        if followed is not None:
            badges.append((x2 - 11, y2, followed))
        dots.append((e["count"], d, cls))
    if animate:
        for i, (count, d, cls) in enumerate(sorted(dots, reverse=True)[:14]):
            dur = 3.2 - 1.6 * count / float(peak)
            fill = cls.replace("s", "f", 1) if cls in ("sg", "sb") else cls.replace("s", "c", 1)
            out.append('<circle class="dot %s" r="2.6"><animateMotion dur="%.1fs" begin="%.2fs" repeatCount="indefinite" '
                       'path="%s"/></circle>' % (fill, dur, (i * 0.37) % dur, d))
    for x, y, ok in badges:
        out.append('<g transform="translate(%.1f,%.1f)"><circle class="%s ring" r="7" stroke-width="2"/>'
                   '<path d="%s" stroke="#fff" stroke-width="1.8" fill="none" stroke-linecap="round"/></g>'
                   % (x, y, "fg" if ok else "fb", "M-3 .2l2 2 4-4.2" if ok else "M-2.6 -2.6l5.2 5.2M2.6 -2.6l-5.2 5.2"))
    for n in nodes:
        x, y = xy[n["id"]]
        quiet = n["name"] in QUIET or n["id"].endswith("_more")
        accent = "cq" if quiet else "c%d" % n["col"]
        sub = "%d×" % n["count"]
        if n.get("avg_ms") is not None:
            sub += " · %s%s" % ("ran " if n["col"] == 3 else "", ms_text(n["avg_ms"]))
        tip = "%s\n%s\n%s" % (n["name"], COLUMNS[n["col"]], sub)
        out.append('<g class="n" data-id="%s"><title>%s</title>'
                   '<rect class="card" x="%.1f" y="%.1f" width="%d" height="%d" rx="9"/>'
                   '<rect class="%s" x="%.1f" y="%.1f" width="4" height="%d" rx="2"/>'
                   '<text class="ink" x="%.1f" y="%.1f" font-weight="600">%s</text>'
                   '<text class="mut" x="%.1f" y="%.1f" font-size="11">%s</text></g>'
                   % (_esc(n["id"]), _esc(tip), x, y, col_w, node_h, accent, x + 1, y + 5, node_h - 10,
                      x + 12, y + 17, _esc(_fit(n["name"], chars)), x + 12, y + 33, _esc(sub)))
    if latest:
        y = top + plot_h + 6
        out.append('<rect class="card" x="12" y="%.1f" width="%d" height="26" rx="8"/>' % (y, width - 24))
        out.append('<text class="ink2" x="24" y="%.1f" font-size="11.5"><tspan font-weight="650" class="ink">Latest  </tspan>%s</text>'
                   % (y + 17, _esc(_fit(latest, int((width - 80) / 6.2)))))
    out.append("</svg>")
    return "".join(out)


def text_tree(s, limit=10):
    """The graph as plain text, for a terminal: each task with a bar for how often, then each distinct route its
    requests took (decided by -> suggested -> used, with a check when Claude used the suggested one)."""
    by_task = {}
    for p in s.get("paths") or []:
        by_task.setdefault(p["task"], []).append(p)
    if not by_task:
        return ["No decisions recorded in this period yet."]
    totals = {t: sum(p["count"] for p in rows) for t, rows in by_task.items()}
    peak = max(totals.values())
    width = max(len(t) for t in list(totals)[:limit])
    lines = []
    for task, rows in sorted(by_task.items(), key=lambda kv: -totals[kv[0]])[:limit]:
        bar = "█" * max(1, int(round(12.0 * totals[task] / peak)))
        lines.append("%s  %s %d" % (task.ljust(width), bar, totals[task]))
        for p in rows[:4]:
            t = "  └ %s → %s" % (p["decided"], p["suggested"])
            if p.get("used"):
                mark = " ✓" if p["used"] == p["suggested"] else " ✗" if p["suggested"] not in QUIET else ""
                t += " → used " + p["used"] + mark
            t += "   %d×" % p["count"]
            if p.get("avg_ms") is not None:
                t += " · " + ms_text(p["avg_ms"])
            lines.append(t)
    return lines
