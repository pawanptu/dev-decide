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
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(str(repo), ignore_errors=True)


if __name__ == "__main__":
    for step in (manifests, plugin_in_sync, first_run, hook_writes_nothing, secrets, rollback, routing, short_issues,
                 starter_upgrade, optional_route_model, gate_guesses, limit_messages, state_sync, decision_graph,
                 hint_server, server):
        try:
            step()
        except Exception as exc:
            check(step.__name__, False, repr(exc))
    shutil.rmtree(str(TRACES), ignore_errors=True)
    print("\n%d check(s) failed" % len(failures) if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)
