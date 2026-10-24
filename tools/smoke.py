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
                 ROOT / "plugin" / "hooks" / "hooks.json", SKILL / "presets.json", SKILL / "policy.default.json"):
        try:
            json.loads(path.read_text(encoding="utf-8"))
            check("valid JSON: %s" % path.relative_to(ROOT), True)
        except (OSError, ValueError) as exc:
            check("valid JSON: %s" % path.relative_to(ROOT), False, str(exc))
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    plugin = json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    check("marketplace lists the plugin", any(p.get("name") == plugin.get("name") for p in market.get("plugins", [])))


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


def server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    token = "smoke-test-token-0123456789"
    env = dict(ENV, DEV_DECIDE_TOKEN=token, DEV_DECIDE_RATE_PER_MIN="100")
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
        check("API refuses a missing token", get("/v1/learning")[0] == 401)
        code, body = get("/v1/decide", {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                         json.dumps({"state": "Login returns 500 for users with a plus sign in their email",
                                     "questions": ["issue_type"], "mode": "local"}).encode())
        check("API answers with the token", code == 200 and b'"issue_type"' in body, body.decode()[:120])
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(str(repo), ignore_errors=True)


if __name__ == "__main__":
    for step in (manifests, plugin_in_sync, first_run, hook_writes_nothing, secrets, rollback, routing, short_issues,
                 starter_upgrade, server):
        try:
            step()
        except Exception as exc:
            check(step.__name__, False, repr(exc))
    print("\n%d check(s) failed" % len(failures) if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)
