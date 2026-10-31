#!/usr/bin/env python3
"""Fast end-to-end checks, run in CI and before a release. No model calls, no network beyond localhost.

  python3 tools/smoke.py
"""
import filecmp
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "dev-decide-v5" / "skill"
SCRIPTS = SKILL / "scripts"
PLUGIN_SKILL = ROOT / "plugin" / "skills" / "dev-decide"
TRACES = Path(tempfile.mkdtemp(prefix="dd-smoke-traces-"))  # the decision graph's records, never the real ~/.claude
os.environ["DEV_DECIDE_TRACE_DIR"] = str(TRACES)
ENV = dict(os.environ, DEV_DECIDE_BACKEND="none", DEV_DECIDE_ESCALATE="0")
failures = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("ok  " if ok else "FAIL", name, (" - " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def decide(repo, request):
    out = subprocess.run([sys.executable, str(SCRIPTS / "decide.py"), request], cwd=str(repo), env=ENV,
                         capture_output=True, text=True, encoding="utf-8", timeout=120)
    return json.loads(out.stdout)


def git_repo():
    d = Path(tempfile.mkdtemp(prefix="dd-smoke-"))
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    return d


def manifests():
    for path in (ROOT / ".claude-plugin" / "marketplace.json", ROOT / "plugin" / ".claude-plugin" / "plugin.json",
                 ROOT / "plugin" / "hooks" / "hooks.json", ROOT / "plugin-graph" / ".claude-plugin" / "plugin.json",
                 ROOT / "plugin-graph" / "hooks" / "hooks.json", SKILL / "presets.json", SKILL / "policy.default.json"):
        try:
            json.loads(path.read_text(encoding="utf-8"))
            check("valid JSON: %s" % path.relative_to(ROOT), True)
        except (OSError, ValueError) as exc:
            check("valid JSON: %s" % path.relative_to(ROOT), False, str(exc))
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    plugin = json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    check("marketplace lists the plugin", any(p.get("name") == plugin.get("name") for p in market.get("plugins", [])))
    check("marketplace lists the decision graph panel",
          any(p.get("name") == "dev-decide-graph" for p in market.get("plugins", [])))


def plugin_in_sync():
    def diff(a, b):
        cmp = filecmp.dircmp(str(a), str(b), ignore=["__pycache__"])
        bad = cmp.left_only + cmp.right_only + cmp.diff_files
        for sub in cmp.common_dirs:
            bad += diff(Path(a) / sub, Path(b) / sub)
        return bad
    bad = diff(SKILL, PLUGIN_SKILL)
    check("plugin copy matches dev-decide-v5/skill (run tools/sync.py)", not bad, ", ".join(bad[:5]))


def first_run():
    repo = git_repo()
    out = decide(repo, "+local issue_type :: Login returns 500 for users with a plus sign in their email")
    a = out.get("answers", {}).get("issue_type", {})
    check("starter models answer issue_type locally", a.get("via") == "learned" and a.get("choice") == "bug", str(a))
    check("first run writes the documented settings file", (repo / ".claude" / "dev-decide.json").is_file())
    check("learned data is git-ignored",
          (repo / ".claude" / "dev-decide" / ".gitignore").read_text(encoding="utf-8").strip() == "*")
    shutil.rmtree(str(repo), ignore_errors=True)


def hook_writes_nothing():
    repo = git_repo()
    payload = json.dumps({"prompt": "rename the user_email column in postgres", "cwd": str(repo)})
    subprocess.run([sys.executable, str(SCRIPTS / "hook_route.py")], input=payload, text=True, env=ENV,
                   capture_output=True, timeout=60)
    check("routing hook writes nothing to the repository", not (repo / ".claude").exists())
    shutil.rmtree(str(repo), ignore_errors=True)


def secrets():
    repo = git_repo()
    (repo / ".env").write_text("API_KEY=a8f3k29dLq0ZxW7p\n", encoding="utf-8")
    subprocess.run(["git", "add", ".env"], cwd=str(repo), check=True)
    out = decide(repo, "+local secret_leak :: DIFF --staged")
    p = out.get("answers", {}).get("secret_leak", {}).get("p_true", 0)
    check("unquoted .env secret is caught", p >= 0.7, str(p))
    (repo / ".env").write_text("MAX_TOKENS=4096\n", encoding="utf-8")
    subprocess.run(["git", "add", ".env"], cwd=str(repo), check=True)
    out = decide(repo, "+local secret_leak :: DIFF --staged")
    p = out.get("answers", {}).get("secret_leak", {}).get("p_true", 1)
    check("harmless setting is not a secret", p < 0.3, str(p))
    shutil.rmtree(str(repo), ignore_errors=True)


def rollback():
    sys.path.insert(0, str(SCRIPTS))
    import learn
    root = Path(tempfile.mkdtemp(prefix="dd-smoke-"))
    p = learn.Paths(root).ensure()
    p.models.mkdir(parents=True, exist_ok=True)
    for v in range(1, 6):
        (p.models / "risk.json").write_text(json.dumps({"v": v}))
        learn.archive(p, "risk", {"version": v})
    try:
        n, err = learn.rollback(p, "risk", 1)
        ok = err is None and json.loads((p.models / "risk.json").read_text()) == {"v": 1}
        check("rollback to the oldest archived version", ok, err or "")
    except Exception as exc:
        check("rollback to the oldest archived version", False, repr(exc))
    shutil.rmtree(str(root), ignore_errors=True)


def routing():
    sys.path.insert(0, str(SCRIPTS))
    sys.path.insert(0, str(ROOT / "tools"))
    import router
    import eval_router
    one = [{"name": "db-migrate", "kind": "skill", "description": "Database schema changes, migrations and rollbacks"}]
    hit = router.best_route("rename the user_email column to email in postgres without downtime", one,
                            min_confidence=0.6, min_margin=0.2)
    check("routing hint: postgres column rename reaches a db-migrate skill", bool(hit) and hit[0] == "db-migrate",
          str(hit))
    miss = router.best_route("fix the CSS on the login button", one, min_confidence=0.6, min_margin=0.2)
    check("routing hint: an unrelated request stays silent", miss is None, str(miss))
    routes, rows = eval_router.load("test")
    r = eval_router.evaluate(routes, rows)
    check("routing hints on held-out requests: precision >= 85%", (r["hint_precision"] or 0) >= 0.85,
          "%.3f" % (r["hint_precision"] or 0))
    check("routing hints with one unrelated skill: false hints <= 6%", r["one_skill_false_hints"] <= 0.06,
          "%.3f" % r["one_skill_false_hints"])


def short_issues():
    sys.path.insert(0, str(ROOT / "tools"))
    import eval_issue_type
    r = eval_issue_type.evaluate(SKILL / "starter-models")
    check("starter issue_type model on held-out short reports: accuracy >= 85%", r["accuracy"] >= 0.85,
          "%.3f" % r["accuracy"])


def starter_upgrade():
    sys.path.insert(0, str(SCRIPTS))
    import ddcore as dd
    import fastmodel as fm
    starters = json.loads((SKILL / "starter-models" / "index.json").read_text(encoding="utf-8"))
    preset = next((k for k, v in starters.items() if v.get("replaces")), None)
    if preset is None:
        check("starter upgrade: a starter model lists the versions it replaces", False)
        return
    old = starters[preset]["replaces"][0]
    for case, patch, upgrade in [("an old starter is upgraded", {}, True),
                                 ("a model the learning loop promoted is kept",
                                  {"source": "dev-decide-learn", "version_id": "learn:1:1"}, False),
                                 ("a model you rolled back to is kept", {"restored": "2026-10-24 10:00"}, False),
                                 ("your own exported model is kept", {"trained": "2026-10-24 09:00:00"}, False)]:
        root = Path(tempfile.mkdtemp(prefix="dd-smoke-"))
        dest = dd.state_dir(root) / "models"
        dest.mkdir(parents=True, exist_ok=True)
        entry = {"enabled": True, "featurizer": "f1", "source": "dev-decide-kit", "trained": old}
        entry.update(patch)
        (dest / "index.json").write_text(json.dumps({preset: entry}), encoding="utf-8")
        (dest / ("%s.json" % preset)).write_text('{"marker": "mine"}', encoding="utf-8")
        fm.seed_models(root)
        kept = json.loads((dest / ("%s.json" % preset)).read_text(encoding="utf-8")).get("marker") == "mine"
        check("starter upgrade: " + case, kept != upgrade)
        shutil.rmtree(str(root), ignore_errors=True)


def hint_server():
    port = _free_port()
    env = dict(ENV, DEV_DECIDE_HOOK_PORT=str(port))
    subprocess.run([sys.executable, str(SCRIPTS / "hookd.py"), "--ensure"], env=env, timeout=60)
    repo = git_repo()
    (repo / ".claude" / "skills" / "db-migrate").mkdir(parents=True)
    (repo / ".claude" / "skills" / "db-migrate" / "SKILL.md").write_text(
        "---\nname: db-migrate\ndescription: Database schema changes, migrations and rollbacks\n---\n", encoding="utf-8")
    body = json.dumps({"prompt": "rename the user_email column to email in postgres without downtime",
                       "cwd": str(repo)}).encode()
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/route" % port, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read().decode() or "{}")
        ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
        check("hint server: answers the routing hint over HTTP", "db-migrate" in ctx, ctx[:120])
        req = urllib.request.Request("http://127.0.0.1:%d/route" % port, data=b"{}", method="POST",
                                     headers={"Origin": "https://example.com"})
        try:
            urllib.request.urlopen(req, timeout=10)
            check("hint server: refuses browser requests", False)
        except urllib.error.HTTPError as e:
            check("hint server: refuses browser requests", e.code == 403)
        check("hint server: writes nothing to the repository", not (repo / ".claude" / "dev-decide").exists())

        def call(path, data=None, headers=None):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method="POST" if data else "GET",
                                         data=json.dumps(data).encode() if data else None, headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, r.read().decode("utf-8"), r.headers
            except urllib.error.HTTPError as e:
                return e.code, "", e.headers
        time.sleep(0.5)  # the agent starts after the prompt and runs for a while
        call("/tool", {"session_id": "", "cwd": str(repo), "hook_event_name": "PostToolUse", "tool_name": "Agent",
                       "tool_use_id": "t1", "duration_ms": 200,
                       "tool_input": {"subagent_type": "db-migrate", "description": "Rename the column",
                                      "prompt": "do the private thing"}})
        time.sleep(1.5)  # traces are written after the answer
        q = "/trace?root=" + urllib.request.quote(str(repo))
        code, body, _ = call(q)
        stats = json.loads(body)["summary"]["stats"] if code == 200 else {}
        check("decision graph: a hint and the agent Claude then used are linked",
              stats.get("hinted") == 1 and stats.get("used") == 1 and stats.get("followed") == 1, str(stats))
        raw = "".join(f.read_text(encoding="utf-8") for f in TRACES.glob("*.jsonl"))
        check("decision graph: keeps only the first 40 characters of a prompt",
              "rename the user_email column to email in" in raw and "without downtime" not in raw
              and "private thing" not in raw)
        code, body, hdr = call("/graph")
        check("decision graph: the page is served with a CSP", code == 200 and "Decision Graph" in body
              and "frame-ancestors 'none'" in (hdr.get("Content-Security-Policy") or ""))
        code, body, _ = call(q.replace("/trace", "/savings") + "&days=1")
        check("decision graph: /savings answers with the token report",
              code == 200 and "flagged_share" in body and call("/savings", headers={"Origin": "https://example.com"})[0] == 403)
        check("decision graph: the page has a token savings card", "sav-body" in call("/graph")[1])
        code, body, hdr = call(q.replace("/trace", "/graph.svg") + "&theme=dark")
        check("decision graph: the SVG names the agent", code == 200 and body.startswith("<svg") and "db-migrate" in body)
        check("decision graph: other sites' pages are refused",
              call(q, headers={"Origin": "https://example.com"})[0] == 403)
        second = subprocess.Popen([sys.executable, str(SCRIPTS / "hookd.py"), "--serve"], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            second.wait(timeout=20)
        except subprocess.TimeoutExpired:
            second.kill()
            second.wait(timeout=10)
            check("hint server: a second server cannot take the same port", False, "it kept running")
        else:
            check("hint server: a second server cannot take the same port", call("/health")[0] == 200)
    finally:
        subprocess.run([sys.executable, str(SCRIPTS / "hookd.py"), "--stop"], env=env, timeout=30)
        shutil.rmtree(str(repo), ignore_errors=True)


def hint_learning():
    sys.path.insert(0, str(SCRIPTS))
    import json as _json
    import fastmodel as fm
    import hook_route
    import traces
    import usage
    # inputs no person typed, and wrappers around typed words
    check("routing hint: background-task notices and agent messages are skipped",
          hook_route.automated("<task-notification> <task-id>b1</task-id> done") and
          hook_route.automated("<agent-message from=\"a1\">please review the change</agent-message>"))
    check("routing hint: comments, paste wrappers and quote marks are not the request",
          hook_route.user_text("<!-- reply 1 --> > Exclude places from the offer list") == "Exclude places from the offer list"
          and hook_route.user_text('<pasted_content id="1">a long log</pasted_content> why does this login test fail')
          == "why does this login test fail")
    repo = git_repo()
    out, ev = hook_route.explain({"prompt": "<task-notification> <task-id>x</task-id> <status>completed</status>",
                                  "cwd": str(repo), "session_id": "s"})
    check("routing hint: an automated message gets no hint and keeps no text",
          out is None and ev["skipped"] == "automated message" and ev.get("text") is None, str(ev))
    # a project holding an older copy of the shipped route model is answered by the shipped one
    names = ["backend-reviewer", "db-migrate", "debugger", "devops-deploy", "docs-writer", "frontend-dev",
             "security-auditor", "test-writer"]
    shipped = _json.loads((fm.OPTIONAL_DIR / "index.json").read_text(encoding="utf-8"))["route"]
    old = fm.Registry(None, models_dir=fm.OPTIONAL_DIR)
    old.index = {"route": dict(shipped, trained=(shipped.get("replaces") or ["?"])[0])}
    check("routing hint: an older copy of the shipped route model gives way to the shipped one",
          fm.current_starter(old, "route", names) is not None)
    old.index["route"]["version_id"] = "learn:3:1"
    check("routing hint: a route model the project trained itself is kept",
          fm.current_starter(old, "route", names) is None)
    # the usage memory: what Claude ran after similar requests
    import router
    m = router._meaning_model()
    for text in ("rename the orders column in postgres", "rename the user email column in the postgres table",
                 "write a migration that renames the user email column"):
        usage.record(repo, m.encode(text), "db-migrate")
    hit = usage.suggest(repo, m.encode("rename the customer email column in postgres"), {"db-migrate", "debugger"})
    check("usage memory: a request like earlier ones gets the agent Claude ran for them", bool(hit) and hit[0] == "db-migrate",
          str(hit))
    check("usage memory: an unrelated request gets nothing",
          usage.suggest(repo, m.encode("thanks, that looks great to me"), {"db-migrate"}) is None)
    raw = usage.path(repo).read_text(encoding="utf-8")
    check("usage memory: keeps meaning vectors, never the text", "orders" not in raw and "postgres" not in raw)
    # real run times from SubagentStop, and launch times dropped
    tr = repo / "agent.jsonl"
    tr.write_text(_json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 90))}) + "\n",
                  encoding="utf-8")
    traces.record(repo, {"kind": "hint", "session": "s9", "hint": "debugger", "ts": int(time.time() * 1000) - 2000})
    hook_route.used({"tool_name": "Agent", "tool_input": {"subagent_type": "debugger"}, "duration_ms": 25,
                     "session_id": "s9", "cwd": str(repo)})
    hook_route.ran({"agent_type": "debugger", "agent_transcript_path": str(tr), "session_id": "s9", "cwd": str(repo)})
    hook_route.used({"tool_name": "Agent", "tool_input": {"subagent_type": "Explore"}, "duration_ms": 30,
                     "session_id": "s9", "cwd": str(repo)})
    summ = traces.summary(traces.load(traces.slug(repo), 1))
    times = {n["name"]: n.get("avg_ms") for n in summ["nodes"] if n["col"] == 3}
    check("decision graph: an agent's run time comes from SubagentStop, not its launch",
          85000 <= (times.get("used debugger") or 0) <= 120000 and times.get("used Explore") is None, str(times))
    traces.record(repo, {"kind": "ran", "session": "fg", "name": "Plan", "ms": 12654, "ts": int(time.time() * 1000) - 5002})
    traces.record(repo, {"kind": "used", "session": "fg", "tool": "Agent", "name": "Plan", "ms": 12668,
                         "ts": int(time.time() * 1000) - 5000})  # a foreground call: recorded just after the agent ended
    plan = [n for n in traces.summary(traces.load(traces.slug(repo), 1))["nodes"] if n["name"] == "used Plan"]
    check("decision graph: a foreground agent is counted once", len(plan) == 1 and plan[0]["count"] == 1, str(plan))
    check("decision graph: a background agent with its real run time still joins the hint before it",
          summ["follow"].get("debugger") == [1, 2], str(summ["follow"]))
    # who did the work: Claude itself (from the Stop hook), and a short prompt owns the agents after it
    now_ms = int(time.time() * 1000)
    stamp = lambda ms: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000.0)) + ".000Z"  # noqa: E731
    traces.record(repo, {"kind": "hint", "session": "t1", "hint": None, "decided_by": "trained model", "ts": now_ms - 70000,
                         "text": "update the readme"})
    lines = [{"type": "user", "timestamp": stamp(now_ms - 70000), "message": {"role": "user", "content": "update the readme"}},
             {"type": "assistant", "timestamp": stamp(now_ms - 60000), "message": {"content": [
                 {"type": "tool_use", "name": "Read", "input": {"file_path": "secret.txt"}},
                 {"type": "tool_use", "name": "Edit", "input": {"old_string": "private words"}}]}},
             {"type": "user", "timestamp": stamp(now_ms - 59000), "message": {"content": [{"type": "tool_result", "content": "x"}]}},
             {"type": "assistant", "timestamp": stamp(now_ms - 50000), "message": {"content": [
                 {"type": "tool_use", "name": "Edit", "input": {}}]}}]
    (repo / "main.jsonl").write_text("".join(_json.dumps(l) + "\n" for l in lines), encoding="utf-8")
    hook_route.turn({"transcript_path": str(repo / "main.jsonl"), "session_id": "t1", "cwd": str(repo)})
    traces.record(repo, {"kind": "hint", "session": "t2", "skipped": "under 4 words", "ts": now_ms - 30000, "text": "go build it"})
    traces.record(repo, {"kind": "used", "session": "t2", "tool": "Agent", "name": "general-purpose", "ts": now_ms - 20000})
    summ = traces.summary(traces.load(traces.slug(repo), 1))
    by_text = {t.get("text"): t for t in summ["timeline"]}
    me = by_text.get("update the readme", {})
    check("decision graph: a reply with no agent is handled by Claude itself, with its tools and time",
          me.get("turn", {}).get("tools") == {"Edit": 2, "Read": 1} and 65000 <= me["turn"]["ms"] <= 80000 and
          any(n["name"] == "Claude itself" for n in summ["nodes"]) and "private words" not in
          traces.path(repo).read_text(encoding="utf-8"), str(me))
    check("decision graph: a short prompt (\"go build it\") owns the agents Claude ran after it",
          [u["name"] for u in by_text.get("go build it", {}).get("used", [])] == ["general-purpose"], str(by_text.get("go build it")))
    # a big request: plan steps, agents nested under the agent that launched them, dev-decide's own pick per sub-task
    t0 = int(time.time() * 1000) - 600000
    rec = lambda e: traces.record(repo, e)  # noqa: E731
    rec({"kind": "hint", "session": "big", "prompt_id": "P1", "hint": "Plan", "decided_by": "zero-shot match", "ts": t0,
         "text": "build the SaaS from plan.md"})
    rec({"kind": "task", "event": "created", "task_id": "1", "subject": "Data model", "session": "big", "prompt_id": "P1", "ts": t0 + 1000})
    rec({"kind": "task", "event": "created", "task_id": "2", "subject": "Backend", "session": "big", "prompt_id": "P1", "ts": t0 + 1100})
    rec({"kind": "used", "session": "big", "prompt_id": "P1", "tool": "Agent", "name": "db-migrate", "ts": t0 + 5000, "ms": 40,
         "text": "Write the schema", "judge": {"agent": "db-migrate", "agrees": True}})
    rec({"kind": "start", "session": "big", "agent_id": "A1", "name": "db-migrate", "ts": t0 + 5000})
    rec({"kind": "ran", "session": "big", "agent_id": "A1", "name": "db-migrate", "ms": 60000, "ts": t0 + 65000, "tools": {"Write": 3}})
    rec({"kind": "task", "event": "completed", "task_id": "1", "session": "big", "prompt_id": "P1", "ts": t0 + 70000})
    rec({"kind": "used", "session": "big", "prompt_id": "P1", "tool": "Agent", "name": "backend-reviewer", "ts": t0 + 80000,
         "ms": 30, "text": "Build the billing API", "judge": {"agent": "debugger", "agrees": False}})
    rec({"kind": "start", "session": "big", "agent_id": "A2", "name": "backend-reviewer", "ts": t0 + 80000})
    rec({"kind": "used", "session": "big", "prompt_id": "inner", "tool": "Agent", "name": "test-writer", "ts": t0 + 90000, "ms": 20,
         "caller": "A2", "text": "Tests for billing"})
    plan = [t for t in traces.summary(traces.load(traces.slug(repo), 1))["timeline"] if t.get("text") == "build the SaaS from plan.md"][0]["plan"]
    names = {u["name"]: u for u in plan["uses"]}
    check("plan tree: steps from the task list, agents under the step in progress, nested under their launcher",
          [st["subject"] for st in plan["steps"]] == ["Data model", "Backend"] and names["db-migrate"]["step"] == "1"
          and names["backend-reviewer"]["step"] == "2" and names["test-writer"]["parent"] == names["backend-reviewer"]["key"]
          and names["db-migrate"]["tools"] == {"Write": 3} and names["db-migrate"]["ms"] == 60000, str(plan)[:300])
    summ2 = traces.summary(traces.load(traces.slug(repo), 1))
    check("plan tree: dev-decide's pick per sub-task is counted", summ2["judge"].get("db-migrate") == [1, 1]
          and summ2["judge"].get("backend-reviewer") == [0, 1], str(summ2["judge"]))
    # a plan made with TodoWrite: snapshots give each step's start and end
    rec({"kind": "hint", "session": "td", "prompt_id": "P2", "hint": None, "ts": t0, "text": "two step job"})
    snap = lambda a, b: [{"subject": "Find the add function", "status": a}, {"subject": "Find the readme", "status": b}]  # noqa: E731
    rec({"kind": "todos", "session": "td", "prompt_id": "P2", "items": snap("pending", "pending"), "ts": t0 + 1000})
    rec({"kind": "todos", "session": "td", "prompt_id": "P2", "items": snap("in_progress", "pending"), "ts": t0 + 2000})
    rec({"kind": "used", "session": "td", "prompt_id": "P2", "tool": "Agent", "name": "Explore", "ts": t0 + 12000, "ms": 9000})  # ends at ts
    rec({"kind": "todos", "session": "td", "prompt_id": "P2", "items": snap("completed", "in_progress"), "ts": t0 + 13000})
    rec({"kind": "used", "session": "td", "prompt_id": "P2", "tool": "Agent", "name": "Plan", "ts": t0 + 19000, "ms": 5000})
    rec({"kind": "todos", "session": "td", "prompt_id": "P2", "items": snap("completed", "completed"), "ts": t0 + 20000})
    tp = [t for t in traces.summary(traces.load(traces.slug(repo), 1))["timeline"] if t.get("text") == "two step job"][0]["plan"]
    sid = {st["subject"]: st["id"] for st in tp["steps"]}
    by = {u["name"]: u["step"] for u in tp["uses"]}
    check("plan tree: TodoWrite steps, each agent under the step that was in progress",
          by.get("Explore") == sid.get("Find the add function") and by.get("Plan") == sid.get("Find the readme")
          and all(st["completed"] for st in tp["steps"]), str(tp)[:300])
    # a hint Claude keeps ignoring needs 0.9
    sid = "s-strict"
    for i in range(6):
        traces.record(repo, {"kind": "hint", "session": sid, "hint": "docs-writer", "ts": 1000 + 10 * i + int(time.time() * 1000) - 60000})
        traces.record(repo, {"kind": "used", "session": sid, "tool": "Agent", "name": "backend-reviewer",
                             "ts": 1005 + 10 * i + int(time.time() * 1000) - 60000})
    check("routing hint: Claude's follow rate per hinted agent is counted",
          traces.follow_stats(repo, refresh=True).get("docs-writer") == [0, 6], str(traces.follow_stats(repo)))
    shutil.rmtree(str(repo), ignore_errors=True)


def option_order_and_audit():
    sys.path.insert(0, str(SCRIPTS))
    import ddcore as dd
    import engine
    import finalize
    import inputs
    import llm
    import request
    repo = git_repo()
    policy = dd.load_policy(repo)
    cat = dd.load_catalog(policy)

    def order(text):
        req = request.parse_text("issue_type :: " + text, cat)
        st = engine.ItemState(0, inputs.resolve(None, text, str(repo), str(repo)))
        st.pending = list(req.questions)
        _, user = llm.build_messages([st], req.questions, {"routes": [], "policy": policy}, set(), False)
        return [l for l in user.splitlines() if "choose one of" in l][0]
    orders = set(order("input number %d about a failing login" % i) for i in range(6))
    check("model prompt: options are shuffled per input, and stable for one input",
          len(orders) > 1 and order("same input") == order("same input"))
    saved = os.environ.get("DEV_DECIDE_BACKEND")
    os.environ["DEV_DECIDE_BACKEND"] = "mock"  # the built-in stand-in model: no network, no cost
    try:
        out = engine.decide("+llm issue_type :: Login returns 500 for users with a plus sign in their email", cwd=str(repo))
    finally:
        os.environ["DEV_DECIDE_BACKEND"] = saved or "none"
    rows = dd.read_jsonl(repo / ".claude" / "dev-decide" / "log.jsonl")
    need = {"threshold", "next", "v", "input", "model"}
    check("decision log rows are audit records (version, input hash, model, threshold, band)",
          bool(rows) and all(need <= set(r) for r in rows), str(rows[:1])[:200])
    a = {"type": "verify", "p_true": 0.5, "via": "llm"}
    ctx = {"cat": cat, "policy": dict(policy, escalate={"band": 0.0}), "warnings": [], "floors": {}}
    res, _ = finalize.finalize_answer("secret_leak", a, ctx)
    check("the escalate band is a setting", res["next"] == "ask_human" and out is not None, str(res))
    shutil.rmtree(str(repo), ignore_errors=True)


def decision_graph():
    sys.path.insert(0, str(SCRIPTS))
    import traces
    check("decision graph: a pasted key is masked in the preview",
          traces.preview("use sk-ant-api03-AbCdEfGhIjKlMnOpQrSt to call the api", 40) == "use [\u2026] to call the api")
    check("decision graph: prompt_chars 0 keeps no text", traces.preview("anything at all", 0) is None)
    ev = [{"kind": "hint", "ts": 1000, "session": "s", "task_type": "bugfix", "decided_by": "trained model",
           "hint": "debugger", "ms": 4},
          {"kind": "used", "ts": 3000, "ms": 500, "session": "s", "tool": "Agent", "name": "Explore"},
          {"kind": "used", "ts": 9000, "ms": 100, "session": "other", "tool": "Agent", "name": "debugger"}]
    s = traces.summary(ev)
    check("decision graph: a use after a hint counts against it, a use in another session does not",
          s["stats"]["after_hint"] == 1 and s["stats"]["followed"] == 0 and
          any(p["used"] == "Explore" and p["suggested"] == "debugger" for p in s["paths"]), json.dumps(s["paths"]))
    check("decision graph: an empty period draws an empty graph", "No decisions recorded" in traces.svg(traces.summary([])))
    repo = git_repo()
    import hook_route
    ide = ("<ide_opened_file>The user opened the file src/components/Button.tsx in the IDE. This may or may not be "
           "related to the current task.</ide_opened_file> ok")
    out, ev = hook_route.explain({"prompt": ide, "cwd": str(repo)})
    check("routing hint: the editor's context is not taken for the request",
          out is None and ev.get("skipped") == "under 4 words" and ev.get("text") == "ok", str(ev))
    decide(repo, "+local issue_type :: Login returns 500 for users with a plus sign in their email")
    rows = traces.load(traces.slug(repo), 1)
    check("decision graph: a /dev-decide answer is recorded outside the repository",
          any(r.get("kind") == "decision" and r.get("answers", {}).get("issue_type") for r in rows)
          and not list(repo.glob(".claude/dev-decide/traces*")), str(rows)[:160])
    shutil.rmtree(str(repo), ignore_errors=True)


def optional_route_model():
    sys.path.insert(0, str(SCRIPTS))
    import fastmodel as fm
    names = ["backend-reviewer", "db-migrate", "debugger", "devops-deploy", "docs-writer", "frontend-dev",
             "security-auditor", "test-writer"]
    check("optional route model: used when its agents exist", fm.optional_registry(names) is not None)
    check("optional route model: not used for other agents", fm.optional_registry(["my-own-agent"]) is None)
    import inputs
    import signals
    reg = fm.optional_registry(names)
    chat =[json.loads(l)["input"]["text"] for l in (ROOT / "dev-decide-kit" / "pretrained" / "data" / "route" /
                                                     "chat_test.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    hinted = 0
    for text in chat:
        item = inputs.Item(None)
        item.kind, item.text = "text", text
        F, _ = signals.analyze(item, [])
        ans = reg.answer("route", "choose", fm.Features(item, F))
        if ans:
            top = max((k for k in ans["probs"] if k != "_rest"), key=ans["probs"].get)
            hinted += top != "none"
    check("route model: held-out chat messages get no agent (at most 10%)", hinted <= 0.1 * len(chat),
          "%d of %d" % (hinted, len(chat)))


def gate_guesses():
    sys.path.insert(0, str(SCRIPTS))
    import finalize
    gate = {"block": ["security_relevant.p_true >= 0.8 and risk.score >= 4", "secret_leak.p_true >= 0.7"]}
    guess = {"security_relevant": {"p_true": 0.82, "via": "hint"}, "risk": {"score": 4.2, "via": "hint"},
             "secret_leak": {"p_true": 0.04, "via": "rule"}}
    found = dict(guess, secret_leak={"p_true": 0.97, "via": "rule"})
    check("a gate never blocks on rough guesses", finalize.eval_gate(gate, guess, [])["verdict"] == "review")
    check("a gate still blocks on a rule's finding", finalize.eval_gate(gate, found, [])["verdict"] == "block")


def limit_messages():
    sys.path.insert(0, str(SCRIPTS))
    import llm
    check("a Claude plan limit message is not taken for an answer",
          llm._limit_message("You've hit your session limit · resets 1:30am") and
          not llm._limit_message("issue_type choose bug=0.9 _rest=0.1"))


LINES = {"sure": "issue_type choose bug=0.9 _rest=0.1", "unsure": "issue_type choose bug=0.4 _rest=0.6"}
ISSUE = "Login returns 500 for users with a plus sign in their email"


def _fake_llm():
    """A local stand-in for OpenAI-compatible and Gemini endpoints. Returns (server, base URL, list of requests)."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
            model = body.get("model") or self.path.split("/models/")[-1].split(":")[0]
            if model == "picky-model" and "max_tokens" in body:  # like newer OpenAI models
                return self._reply(400, {"error": {"message": "Unsupported parameter: 'max_tokens'. Use "
                                                              "'max_completion_tokens' instead."}})
            text = LINES["unsure" if model == "unsure-model" else "sure"]
            if self.path.endswith("/chat/completions"):
                return self._reply(200, {"choices": [{"message": {"content": text}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
            if self.path.endswith(":generateContent"):
                return self._reply(200, {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]})
            self._reply(404, {"error": "no such path"})

        def _reply(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1], seen


def other_backends():
    srv, base, seen = _fake_llm()
    home = Path(tempfile.mkdtemp(prefix="dd-smoke-home-"))  # no personal settings leak in from this machine
    repo = git_repo()
    (repo / ".claude").mkdir()
    # A repository's own settings must not redirect a key or name a program to run.
    (repo / ".claude" / "dev-decide.json").write_text(json.dumps({
        "openai": {"base_url": "http://127.0.0.1:9/v1", "key_env": "PATH"}, "gemini": {"base_url": "http://127.0.0.1:9"},
        "command": ["python", "-c", "open('pwned','w')"]}), encoding="utf-8")
    env = dict(ENV, HOME=str(home), USERPROFILE=str(home), DEV_DECIDE_ESCALATE="0")
    for var in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEV_DECIDE_COMMAND",
                "DEV_DECIDE_MODEL", "DEV_DECIDE_OPENAI_BASE_URL", "DEV_DECIDE_GEMINI_BASE_URL"):
        env.pop(var, None)

    def run(extra, request="+llm +nocache issue_type :: " + ISSUE):
        out = subprocess.run([sys.executable, str(SCRIPTS / "decide.py"), request], cwd=str(repo),
                             env=dict(env, **extra), capture_output=True, text=True, encoding="utf-8", timeout=120)
        return json.loads(out.stdout)

    def answered(out, backend):
        a = (out.get("answers") or {}).get("issue_type") or {}
        return a.get("choice") == "bug" and str((out.get("meta") or {}).get("backend") or "").startswith(backend)

    try:
        openai = {"DEV_DECIDE_BACKEND": "openai", "OPENAI_BASE_URL": base + "/v1", "OPENAI_API_KEY": "sk-test-0123456789",
                  "DEV_DECIDE_MODEL_OPENAI": "test-model"}
        out = run(openai)
        req = seen[-1] if seen else {}
        check("openai backend: answers through an OpenAI-compatible endpoint", answered(out, "openai:test-model"),
              json.dumps(out.get("meta"))[:160])
        check("openai backend: sends the key, the model and the system prompt",
              req.get("headers", {}).get("Authorization") == "Bearer sk-test-0123456789" and
              req.get("body", {}).get("model") == "test-model" and
              (req.get("body", {}).get("messages") or [{}])[0].get("role") == "system")
        (home / ".claude").mkdir()
        (home / ".claude" / "dev-decide.json").write_text(json.dumps({"openai": {"base_url": base + "/v1"}}),
                                                           encoding="utf-8")
        out = run(dict(openai, OPENAI_BASE_URL=""))
        run({"DEV_DECIDE_BACKEND": "command"})
        check("a repository's settings cannot redirect a key or run a program; personal settings can",
              answered(out, "openai") and len(seen) == 2 and seen[-1]["headers"].get("Authorization") ==
              "Bearer sk-test-0123456789" and not (repo / "pwned").exists())
        (home / ".claude" / "dev-decide.json").unlink()
        n = len(seen)
        out = run(dict(openai, DEV_DECIDE_MODEL_OPENAI="picky-model"))
        check("openai backend: retries with max_completion_tokens when a model asks for it",
              answered(out, "openai") and len(seen) == n + 2 and "max_completion_tokens" in seen[-1]["body"])
        out = run(dict(openai, DEV_DECIDE_MODEL_OPENAI=""))
        check("openai backend: a missing model name is reported, not guessed",
              "no openai model set" in json.dumps(out.get("meta")), json.dumps(out.get("meta"))[:160])
        n = len(seen)
        out = run({"DEV_DECIDE_BACKEND": "openai"})
        check("a named backend that is not set up never falls back to another provider",
              len(seen) == n and not answered(out, "openai"))

        n = len(seen)
        esc = dict(openai, DEV_DECIDE_MODEL_OPENAI="unsure-model", DEV_DECIDE_ESCALATE="1")
        run(esc)
        check("escalation is skipped when no stronger model is named for the backend", len(seen) == n + 1)
        n = len(seen)
        run(dict(esc, DEV_DECIDE_STRONG_MODEL_OPENAI="test-model"))
        check("escalation uses the backend's stronger model when one is named",
              len(seen) == n + 2 and seen[-1]["body"].get("model") == "test-model")

        out = run({"DEV_DECIDE_BACKEND": "gemini", "DEV_DECIDE_GEMINI_BASE_URL": base + "/v1beta",
                   "GEMINI_API_KEY": "gem-test-0123456789", "DEV_DECIDE_MODEL_GEMINI": "gem-model"})
        req = seen[-1] if seen else {}
        check("gemini backend: answers through the Gemini API", answered(out, "gemini:gem-model") and
              req.get("path") == "/v1beta/models/gem-model:generateContent" and
              req.get("headers", {}).get("X-Goog-Api-Key", req.get("headers", {}).get("x-goog-api-key")) == "gem-test-0123456789",
              json.dumps(out.get("meta"))[:160])

        cli = home / "fake_cli.py"
        cli.write_text("import sys\nprompt = sys.stdin.read()\n"
                       "print('" + LINES["sure"] + "' if sys.argv[1] == 'cli-model' and 'issue_type' in prompt else 'no')\n",
                       encoding="utf-8")
        out = run({"DEV_DECIDE_BACKEND": "command", "DEV_DECIDE_COMMAND": json.dumps([sys.executable, str(cli), "{model}"]),
                   "DEV_DECIDE_MODEL_COMMAND": "cli-model"})
        check("command backend: runs another model CLI with the prompt on stdin", answered(out, "command:cli-model"),
              json.dumps(out.get("meta"))[:160])
    finally:
        srv.shutdown()
        shutil.rmtree(str(repo), ignore_errors=True)
        shutil.rmtree(str(home), ignore_errors=True)


def mcp():
    repo = git_repo()
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "decide", "arguments": {
                "questions": "issue_type", "input": ISSUE, "mode": "local"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "decide", "arguments": {
                "questions": "issue_type", "input": "@/etc/passwd"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "catalog", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 6, "method": "no/such"}]
    try:
        proc = subprocess.run([sys.executable, str(SCRIPTS / "mcp_server.py"), "--cwd", str(repo)], env=ENV,
                              input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True,
                              encoding="utf-8", timeout=120)
        replies = {}
        bad = []
        for line in proc.stdout.splitlines():
            try:
                r = json.loads(line)
                replies[r.get("id")] = r
            except ValueError:
                bad.append(line)
        init = (replies.get(1) or {}).get("result") or {}
        check("mcp: initializes, and stdout carries nothing but protocol messages",
              init.get("protocolVersion") == "2025-06-18" and "tools" in init.get("capabilities", {}) and not bad,
              (bad[:1] or [proc.stderr[-200:]])[0])
        names = [t["name"] for t in ((replies.get(2) or {}).get("result") or {}).get("tools", [])]
        check("mcp: lists decide, correct and catalog", names == ["decide", "correct", "catalog"], str(names))
        res = (replies.get(3) or {}).get("result") or {}
        check("mcp: decide returns typed answers",
              not res.get("isError") and "issue_type" in (res.get("structuredContent") or {}).get("answers", {}),
              json.dumps(res)[:200])
        check("mcp: refuses @path inputs", ((replies.get(4) or {}).get("result") or {}).get("isError") is True)
        cat = ((replies.get(5) or {}).get("result") or {}).get("structuredContent") or {}
        check("mcp: catalog lists bundles and presets", "pr" in cat.get("bundles", {}) and "risk" in cat.get("score", []))
        check("mcp: unknown methods get a JSON-RPC error", (replies.get(6) or {}).get("error", {}).get("code") == -32601)
    finally:
        shutil.rmtree(str(repo), ignore_errors=True)


def state_sync():
    sys.path.insert(0, str(SCRIPTS))
    import statesync
    remote = Path(tempfile.mkdtemp(prefix="dd-smoke-remote-"))
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    a = git_repo()
    s1 = statesync.StateSync(str(a), str(remote), b"unused-for-a-local-remote")
    s1.restore()  # as the server does when it starts; what changes afterwards gets saved
    st = a / ".claude" / "dev-decide"
    for d in ("models", "learn", "cache"):
        (st / d).mkdir(parents=True, exist_ok=True)
    (st / "models" / "x.json").write_text('{"v": 1}', encoding="utf-8")
    (st / "learn" / "memory.jsonl").write_text(json.dumps({"preset": "p", "x": [[1, 1.0]], "text": "secret input"}) + "\n",
                                                encoding="utf-8")
    (st / "cache" / "c.json").write_text("{}", encoding="utf-8")
    (st / "learn" / "recent.jsonl").write_text('{"text": "secret"}\n', encoding="utf-8")
    saved = s1.save()
    b = git_repo()
    s2 = statesync.StateSync(str(b), str(remote), b"unused-for-a-local-remote")
    s2.restore()
    got = b / ".claude" / "dev-decide"
    check("state sync: saves and restores learned models", saved and (got / "models" / "x.json").is_file())
    mem = (got / "learn" / "memory.jsonl").read_text(encoding="utf-8") if (got / "learn" / "memory.jsonl").exists() else ""
    check("state sync: corrections keep no text", '"x"' in mem and "secret" not in mem, mem[:80])
    check("state sync: never saves the cache or recent inputs",
          not (got / "cache" / "c.json").exists() and not (got / "learn" / "recent.jsonl").exists())
    for d in (remote, a, b):
        shutil.rmtree(str(d), ignore_errors=True)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    token = "smoke-test-token-0123456789"
    demo = "demo-smoke-token-0123456789"
    env = dict(ENV, DEV_DECIDE_TOKEN=token, DEV_DECIDE_DEMO_TOKEN=demo, DEV_DECIDE_RATE_PER_MIN="100")
    repo = git_repo()
    proc = subprocess.Popen([sys.executable, str(SCRIPTS / "serve.py"), "--port", str(port), "--cwd", str(repo)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port

    def get(path, headers=None, body=None):
        req = urllib.request.Request(base + path, data=body, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    try:
        for _ in range(50):
            try:
                if get("/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        code, body = get("/health")
        health = json.loads(body)
        check("server /health", code == 200 and health.get("model") == "none", body.decode()[:120])
        code, body = get("/")
        check("server serves the web page", code == 200 and b"Dev Decide" in body)
        with urllib.request.urlopen(base + "/", timeout=30) as r:
            csp = r.headers.get("Content-Security-Policy") or ""
        check("web page allows no inline or eval'd script", "script-src 'self';" in csp and "unsafe-eval" not in csp, csp)
        for asset in ("/ui.js", "/vendor/alpine-csp.min.js"):
            req = urllib.request.Request(base + asset)
            with urllib.request.urlopen(req, timeout=30) as r:
                ok = r.status == 200 and "javascript" in (r.headers.get("Content-Type") or "") and len(r.read()) > 1000
            check("server serves %s as JavaScript" % asset, ok)
        check("API refuses a missing token", get("/v1/learning")[0] == 401)
        code, body = get("/v1/decide", {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                         json.dumps({"state": "Login returns 500 for users with a plus sign in their email",
                                     "questions": ["issue_type"], "mode": "local"}).encode())
        check("API answers with the token", code == 200 and b'"issue_type"' in body, body.decode()[:120])
        check("health shows the public demo token", health.get("demo_token") == demo)
        req = urllib.request.Request(base + "/health")
        with urllib.request.urlopen(req, timeout=30) as r:
            hdr = r.headers
        check("security headers on every response", hdr.get("X-Frame-Options") == "DENY" and
              hdr.get("X-Content-Type-Options") == "nosniff" and hdr.get("Referrer-Policy") == "no-referrer")
        code, body = get("/v1/decide", {"Authorization": "Bearer " + demo, "Content-Type": "application/json"},
                         json.dumps({"state": "Checkout crashes for all users", "questions": ["issue_type"],
                                     "mode": "hybrid"}).encode())
        warns = (json.loads(body).get("meta") or {}).get("warnings") or []
        check("demo token: answered locally, with a note", code == 200 and any("demo token" in w for w in warns),
              str(warns)[:120])
        code, _ = get("/v1/feedback", {"Authorization": "Bearer " + demo, "Content-Type": "application/json"},
                      json.dumps({"id": "last", "answers": {"issue_type": "bug"}}).encode())
        check("demo token: feedback refused", code == 403)
        code, _ = get("/v1/decide", {"Authorization": "Bearer " + demo, "X-Anthropic-Api-Key": "not-a-key",
                                     "Content-Type": "application/json"},
                      json.dumps({"state": "x y z w", "questions": ["issue_type"]}).encode())
        check("own key: a malformed key is refused", code == 400)
        code, _ = get("/v1/decide", {"Authorization": "Bearer " + demo, "X-Anthropic-Api-Key": "sk-ant-" + "a" * 30,
                                     "X-OpenAI-Api-Key": "b" * 30, "Content-Type": "application/json"},
                      json.dumps({"state": "x y z w", "questions": ["issue_type"]}).encode())
        check("own key: two key headers are refused", code == 400)
        check("health lists whose keys a caller may bring", health.get("own_keys") == ["anthropic"], str(health.get("own_keys")))
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(str(repo), ignore_errors=True)


def token_savings():
    sys.path.insert(0, str(SCRIPTS))
    import guard
    import tokens
    import traces
    use = {"input_tokens": 3, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 5}
    rows = [{"type": "assistant", "message": {"id": "m1", "usage": dict(use)}},  # one message, written as two lines
            {"type": "assistant", "message": {"id": "m1", "usage": dict(use, output_tokens=40)}},
            {"type": "assistant", "message": {"id": "m2", "model": "<synthetic>", "usage": dict(use)}},
            {"type": "assistant", "message": {"id": "m3", "usage": dict(use)}}, {"type": "user", "message": {}}]
    t = tokens.count(rows)
    check("tokens: a message written as several lines is counted once, synthetic ones not at all",
          t == {"input": 6, "cache_write": 200, "cache_read": 100, "output": 45, "calls": 2}, str(t))
    check("tokens: no assistant message gives no count", tokens.count([{"type": "user"}]) is None)
    ev = [{"kind": "hint", "ts": 1000, "session": "s", "prompt_id": "p1", "hint": None},
          {"kind": "turn", "ts": 5000, "session": "s", "prompt_id": "p1", "prompt_ts": 1000, "ms": 4000, "tools": {},
           "tokens": t},
          {"kind": "guard", "ts": 2000, "rule": "big-file", "tool": "Read", "tokens": 9000}, {"kind": "turn", "ts": 9}]
    st = traces.summary(ev)["stats"]
    check("decision graph: tokens per prompt and the guard's notes are totalled",
          (st.get("tokens") or {}).get("fresh") == 251 and (st.get("guard") or {}).get("notes") == 1
          and st["guard"]["at_stake"] == 9000, json.dumps({k: st.get(k) for k in ("tokens", "guard")}))
    d = Path(tempfile.mkdtemp(prefix="dd-smoke-guard-"))
    try:
        big, small = d / "big.py", d / "small.py"
        big.write_text(("x = 1" + chr(10)) * 20000, encoding="utf-8")
        small.write_text(("x = 1" + chr(10)) * 10, encoding="utf-8")
        cfg = guard.config({})

        def run(tool, config=cfg, sid="s", **tin):
            return guard.check({"tool_name": tool, "tool_input": tin, "cwd": str(d), "session_id": sid}, config)
        out, ev1 = run("Read", file_path=str(big))
        note = ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")
        check("token guard: a whole read of a big file gets a note and the read still goes ahead",
              "big.py" in note and "Grep" in note and (ev1 or {}).get("rule") == "big-file" and "permissionDecision" not in str(out))
        check("token guard: a read of the same unchanged file again is noted",
              (run("Read", file_path=str(big))[1] or {}).get("rule") == "re-read")
        check("token guard: a bounded read and a small file are left alone",
              run("Read", file_path=str(big), limit=50, sid="t")[0] is None and run("Read", file_path=str(small))[0] is None)
        check("token guard: git log with no count is noted, with a count it is not",
              (run("Bash", command="git log")[1] or {}).get("rule") == "unbounded-log"
              and run("Bash", command="git log -n 5")[0] is None and run("Bash", command="git log --oneline | head")[0] is None)
        check("token guard: cat of a big file is noted; a compound command is not judged",
              (run("Bash", command="cat big.py")[1] or {}).get("rule") == "cat-big-file"
              and run("Bash", command="cat big.py; echo done")[0] is None)
        check("token guard: the event holds no path and no command",
              all(str(d) not in json.dumps(e) and "big.py" not in json.dumps(e) for e in (ev1,)))
        first = guard.config({"token_guard": {"mode": "block_first"}})
        a = run("Read", first, "b", file_path=str(big))[0]
        b = run("Read", first, "b", file_path=str(big))[0]
        check("token guard: block_first refuses the first big read once and lets the retry through",
              ((a or {}).get("hookSpecificOutput") or {}).get("permissionDecision") == "deny" and b is None, str((a, b)))
        off = guard.config({"token_guard": {"enabled": False}})
        check("token guard: can be turned off", off["enabled"] is False)
        hooks = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        pre = json.dumps(hooks.get("PreToolUse"))
        check("token guard: the plugin registers it for Read and Bash only", '"Read|Bash"' in pre and ":47321/guard" in pre)
        port = _free_port()
        env = dict(ENV, DEV_DECIDE_HOOK_PORT=str(port))
        subprocess.run([sys.executable, str(SCRIPTS / "hookd.py"), "--ensure"], env=env, timeout=60)
        repo = git_repo()
        try:
            def post(path, data):
                req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=json.dumps(data).encode(), method="POST")
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.read().decode("utf-8")
            body = post("/guard", {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_use_id": "g1", "session_id": "q",
                                   "cwd": str(repo), "tool_input": {"file_path": str(big)}})
            check("hint server: /guard answers a note for a big read",
                  "additionalContext" in body and "big.py" in body, body[:120])
            check("hint server: /guard stays silent for the same tool use arriving twice",
                  post("/guard", {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_use_id": "g1", "session_id": "q",
                                  "cwd": str(repo), "tool_input": {"file_path": str(big)}}) == "")
            check("hint server: /guard stays silent about a small file",
                  post("/guard", {"tool_name": "Read", "tool_use_id": "g2", "cwd": str(repo),
                                  "tool_input": {"file_path": str(small)}}) == "")
            time.sleep(1.5)
            raw = "".join(f.read_text(encoding="utf-8") for f in TRACES.glob("*.jsonl"))
            check("token guard: the trace keeps the rule and a token estimate, never the path",
                  '"rule":"big-file"' in raw and "big.py" not in raw and str(d) not in raw)
        finally:
            subprocess.run([sys.executable, str(SCRIPTS / "hookd.py"), "--stop"], env=env, timeout=30)
            shutil.rmtree(str(repo), ignore_errors=True)
    finally:
        shutil.rmtree(str(d), ignore_errors=True)


def lean_plugin():
    sys.path.insert(0, str(SCRIPTS))
    import savings
    lite = ROOT / "plugin-lite"
    for rel in (".claude-plugin/plugin.json", "hooks/hooks.json"):
        try:
            json.loads((lite / rel).read_text(encoding="utf-8"))
            check("valid JSON: plugin-lite/%s" % rel, True)
        except (OSError, ValueError) as exc:
            check("valid JSON: plugin-lite/%s" % rel, False, str(exc))
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    check("marketplace lists the lean plugin", any(p.get("name") == "dev-decide-lite" and p.get("source") == "./plugin-lite"
                                                   for p in market.get("plugins", [])))
    bad = [n for n in ("run.sh", "guard.py", "tokens.py", "savings.py", "lite.py")
           if not (lite / "scripts" / n).exists() or (lite / "scripts" / n).read_bytes() != (SCRIPTS / n).read_bytes()]
    check("lean plugin scripts match the engine (run tools/sync.py)", not bad, ", ".join(bad))
    home = Path(tempfile.mkdtemp(prefix="dd-smoke-lite-"))
    repo = git_repo()
    big = repo / "big.py"
    big.write_text(("x = 1" + chr(10)) * 20000, encoding="utf-8")
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    env.pop("DEV_DECIDE_CHILD", None)

    def hook(file_path, tool_use_id):
        out = subprocess.run([sys.executable, str(lite / "scripts" / "lite.py")], env=env, capture_output=True, text=True,
                             encoding="utf-8", timeout=60, input=json.dumps({
                                 "hook_event_name": "PreToolUse", "tool_name": "Read", "tool_use_id": tool_use_id,
                                 "session_id": "s1", "cwd": str(repo), "tool_input": {"file_path": file_path}}))
        return out.stdout.strip()
    try:
        first, second = hook(str(big), "a"), hook(str(big), "b")
        check("lean plugin: a big read gets a note", "big.py" in first and "additionalContext" in first, first[:120])
        check("lean plugin: the same unchanged file read again is remembered between calls", "read" in second
              and "min ago" in second, second[:160])
        state = "".join(f.read_text(encoding="utf-8") for f in (home / ".claude" / "dev-decide" / "guard").glob("*"))
        check("lean plugin: nothing on disk holds a path", "big.py" not in state and str(repo) not in state and state)
        (repo / ".claude").mkdir()
        (repo / ".claude" / "dev-decide.json").write_text(json.dumps({"token_guard": {"enabled": False}}), encoding="utf-8")
        check("lean plugin: can be turned off in the project", hook(str(big), "c") == "")
        # a transcript with a big read, the same read again, and a small one, 10 days old and 2 days old
        proj = home / ".claude" / "projects" / __import__("re").sub(r"[^A-Za-z0-9]", "-", str(repo.resolve()))
        proj.mkdir(parents=True)
        rows = []
        now = time.time()
        for i, (age, name) in enumerate(((2, "big"), (2, "again"))):
            ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - age * 86400 + i))
            tid = "t%d" % i
            rows.append({"type": "assistant", "timestamp": ts, "sessionId": "s", "message": {
                "id": "m%d" % i, "usage": {"input_tokens": 1, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 0,
                                           "output_tokens": 10},
                "content": [{"type": "tool_use", "id": tid, "name": "Read", "input": {"file_path": "/x/big.py"}}]}})
            rows.append({"type": "user", "timestamp": ts, "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "y" * 80000}]}})
        (proj / "s.jsonl").write_text(chr(10).join(json.dumps(r) for r in rows) + chr(10), encoding="utf-8")
        saved = os.environ.get("HOME"), os.environ.get("USERPROFILE")
        os.environ.update(HOME=str(home), USERPROFILE=str(home))
        try:
            rep = savings.report(7, str(repo))
        finally:
            for k, v in zip(("HOME", "USERPROFILE"), saved):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        f = rep["now"]["flagged"]
        check("savings: a big read and the same read again are counted from the transcript",
              f["big-file"]["count"] == 1 and f["repeat"]["count"] == 1 and f["big-file"]["tokens"] == 20000
              and rep["now"]["spent"]["calls"] == 2, json.dumps(rep["now"])[:300])
        check("savings: the report says it is an upper bound", "upper bound" in savings.text(rep))
    finally:
        shutil.rmtree(str(home), ignore_errors=True)
        shutil.rmtree(str(repo), ignore_errors=True)


def instant_answers():
    sys.path.insert(0, str(SCRIPTS))
    import hook_route
    repo = git_repo()
    saved = dict(os.environ)
    os.environ.update(DEV_DECIDE_BACKEND="none", DEV_DECIDE_ESCALATE="0", HOME=str(repo), USERPROFILE=str(repo))
    try:
        ask = {"prompt": "/dev-decide status", "cwd": str(repo)}
        check("instant: off unless the setting is on", hook_route.instant(ask) is None)
        (repo / ".claude").mkdir()
        (repo / ".claude" / "dev-decide.json").write_text(json.dumps({"instant": {"enabled": True}}), encoding="utf-8")
        out = hook_route.instant(ask) or {}
        check("instant: a /dev-decide prompt is answered without Claude", out.get("decision") == "block"
              and "dev-decide learning" in out.get("reason", ""), str(out)[:160])
        check("instant: other prompts and failed answers go on to Claude",
              hook_route.instant({"prompt": "fix the login button please", "cwd": str(repo)}) is None
              and hook_route.instant({"prompt": "/dev-decide risk :: drop the users table", "cwd": str(repo)}) is None)
        hooks = json.dumps(json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"])
        check("instant: the plugin registers its hook with a long timeout", ":47321/instant" in hooks)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        shutil.rmtree(str(repo), ignore_errors=True)


def files_hint():
    sys.path.insert(0, str(SCRIPTS))
    import hook_route
    repo = git_repo()
    try:
        (repo / "src").mkdir()
        for name in ("a.py", "keys.py"):
            (repo / "src" / name).write_text("x = 1", encoding="utf-8")
        (repo / ".claude").mkdir()
        ask = "add a retry with backoff to the payment webhook handler"
        now = time.time()

        def edit(rel, at):
            hook_route.learn_files({"tool_name": "Edit", "cwd": str(repo), "session_id": "f",
                                    "tool_input": {"file_path": str(repo / rel)}}, ask, at)
        for i in range(4):  # four earlier prompts of this kind, each editing a.py (and one touching a secret-looking file)
            edit("src/a.py", now - 10 - i)
            edit("src/a.py", now - 10 - i)  # a second edit in the same prompt adds no vote
            edit("src/keys.py", now - 10 - i)
        edit("../outside.py", now)
        hint = lambda: ((hook_route.explain({"prompt": ask, "cwd": str(repo)})[0] or {}).get("hookSpecificOutput") or {}
                        ).get("additionalContext", "")
        check("files hint: off by default, nothing is remembered", "edited" not in hint())
        (repo / ".claude" / "dev-decide.json").write_text(json.dumps({"files_hint": {"enabled": True}}), encoding="utf-8")
        for i in range(4):
            edit("src/a.py", now - 20 - i)
            edit("src/a.py", now - 20 - i)
            edit("src/keys.py", now - 20 - i)
        edit("../outside.py", now)
        line = hint()
        check("files hint: the file edited after similar requests is named", "src/a.py" in line, line[:160])
        check("files hint: secret-looking names and files outside the project are never kept",
              "keys.py" not in line and "outside" not in line
              and "keys.py" not in "".join(f.read_text(encoding="utf-8") for f in TRACES.glob("usage/*.jsonl")))
        check("files hint: a different kind of request gets no files",
              "edited" not in ((hook_route.explain({"prompt": "translate the readme into french and spanish please",
                                                    "cwd": str(repo)})[0] or {}).get("hookSpecificOutput") or {}
                               ).get("additionalContext", ""))
    finally:
        shutil.rmtree(str(repo), ignore_errors=True)


if __name__ == "__main__":
    for step in (manifests, plugin_in_sync, first_run, hook_writes_nothing, secrets, rollback, routing, short_issues,
                 starter_upgrade, optional_route_model, gate_guesses, limit_messages, other_backends, mcp, state_sync,
                 option_order_and_audit, hint_learning, decision_graph, token_savings, lean_plugin, instant_answers, files_hint, hint_server, server):
        try:
            step()
        except Exception as exc:
            check(step.__name__, False, repr(exc))
    shutil.rmtree(str(TRACES), ignore_errors=True)
    print("\n%d check(s) failed" % len(failures) if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)
