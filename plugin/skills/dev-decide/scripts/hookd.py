#!/usr/bin/env python3
"""The routing hint without a new process per prompt: a small local server Claude Code's HTTP hook calls.

A command hook starts Python, imports the engine and loads the meaning model on every prompt (about 0.3 s on
Windows). This server keeps all of that loaded, so a hint takes a few milliseconds:

  UserPromptSubmit  {"type": "http", "url": "http://127.0.0.1:47321/route", "timeout": 2}
  PostToolUse       {"type": "http", "url": "http://127.0.0.1:47321/tool", "timeout": 2}   (matcher "Agent|Skill")
  SubagentStop      {"type": "http", "url": "http://127.0.0.1:47321/agent", "timeout": 2}
  Stop              {"type": "http", "url": "http://127.0.0.1:47321/turn", "timeout": 2}
  SessionStart      {"type": "command", "command": "sh <scripts>/run.sh hookd.py --ensure"}

  hookd.py --ensure   start the server unless one with this code is already running (prints nothing)
  hookd.py --serve    run in the foreground (what --ensure starts in the background)
  hookd.py --stop     stop a running server

It also keeps the decision graph (traces.py): after answering, it notes what the hint decided and, from
PostToolUse, which agent or skill Claude really ran and for how long (on this machine, never in the
repository; of a prompt only its first 40 characters, keys masked), and draws it:

  GET /graph        the decision graph page (open http://127.0.0.1:47321/graph in a browser)
  GET /trace        the graph, timeline and totals as JSON (?root=<project folder> or ?project=<id>, &days=1)
  GET /graph.svg    the graph as SVG (&theme=light|dark|auto, &width=960, &header=1 totals and latest, &anim=1), for the side panel

It listens on 127.0.0.1 only and answers no other site's page (a request whose Host is not localhost, or
with an Origin header other than its own page's, is refused). It reads only agent and skill descriptions
(as the command hook does) and exits after DEV_DECIDE_HOOK_IDLE_HOURS (default 12) without a request. The
port is DEV_DECIDE_HOOK_PORT (default 47321). If the server is not running, Claude Code shows a non-blocking
hook error and the prompt goes ahead without a hint; the next session start restarts it.
"""
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PORT = int(os.environ.get("DEV_DECIDE_HOOK_PORT") or 47321)
IDLE_EXIT_S = float(os.environ.get("DEV_DECIDE_HOOK_IDLE_HOURS") or 12) * 3600
MAX_BODY = 1 << 20
LOCAL_HOSTS = ("127.0.0.1", "localhost")
SERVICE = "dev-decide-hook"
PAGE = HERE.parent / "graph.html"
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
}


class _Server(ThreadingHTTPServer):
    """One server per port. Python's default SO_REUSEADDR lets a second server bind the same port on Windows (two
    sessions starting at once would then split the requests, and each would hint), so bind exclusively there."""
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        ThreadingHTTPServer.server_bind(self)


def code_signature():
    """A hash of the engine's code, so --ensure replaces a server running different code. It is based on the
    files' contents, not their times, so two installed copies of the same version (the plugin and a global
    install, say) agree and do not keep restarting each other's server."""
    h = hashlib.sha256()
    for f in sorted(HERE.glob("*.py")):
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def _call(method, path, timeout=0.5):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def running():
    """The running server's /health answer, or None."""
    try:
        h = _call("GET", "/health")
        return h if h.get("service") == SERVICE else None
    except Exception:
        return None


def serve():
    import hook_route  # loads the engine once; the meaning model loads on the first prompt and stays loaded

    state = {"last": time.monotonic()}
    sig = code_signature()
    # The same prompt can arrive twice: Claude Code runs a plugin's hook and a settings file's identical hook
    # separately. Only the first gets the hint, so Claude never sees it twice.
    seen, seen_lock = {}, threading.Lock()

    def first_time(data):
        key = hashlib.sha256(("%s\0%s" % (data.get("session_id") or "", data.get("prompt") or "")).encode(
            "utf-8", "replace")).hexdigest()
        now = time.monotonic()
        with seen_lock:
            for k in [k for k, t in seen.items() if now - t > 30]:
                del seen[k]
            if key in seen:
                return False
            seen[key] = now
            return True

    def first_use(data):  # a tool use reaches every PostToolUse hook that matches: note it once
        tid = data.get("tool_use_id") or json.dumps([data.get("session_id"), data.get("tool_name"),
                                                     data.get("tool_input")], sort_keys=True, default=str)
        return first_time({"session_id": "tool", "prompt": tid})

    # Each session's latest real prompt, in memory only (never written): when Claude then runs an agent or skill,
    # the usage memory learns "a request like this one led to that" (as a meaning vector, see usage.py).
    last_prompt, learned = {}, set()

    def remember(data, ev):
        if ev and not ev.get("skipped") and data.get("session_id"):
            with seen_lock:
                for k in [k for k, v in last_prompt.items() if time.time() - v[0] > hook_route.LEARN_WINDOW_S]:
                    del last_prompt[k]
                last_prompt[data["session_id"]] = (time.time(), hook_route.user_text(data.get("prompt")))

    def learn_from(data):
        with seen_lock:
            hit = last_prompt.get(data.get("session_id") or "")
        _, name, _ = hook_route._used_name(data)
        if hit and name and (hit[0], name) not in learned:  # one lesson per prompt and agent
            learned.add((hit[0], name))
            hook_route.learn_use(data, hit[1], hit[0])

    def later(fn, *args):  # trace writes happen after the answer is sent, so they never slow a prompt
        def run():
            try:
                fn(*args)
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def graph_data(q):
        import traces
        projects = traces.projects()
        pid = (q.get("project") or [""])[0]
        root = (q.get("root") or [""])[0]
        if not pid and root:
            pid = traces.slug(root)
        if not pid and projects:
            pid = projects[0]["id"]
        try:
            days = min(max(float((q.get("days") or ["1"])[0]), 1 / 24.0), 366.0)
        except ValueError:
            days = 1.0
        summ = traces.summary(traces.load(pid, days), span_ms=days * 86400000)
        n, ts = traces.last_use(pid)
        summ["recent"] = {"used_30d": n, "last_used_ts": ts}  # so an empty period can point to the last run
        return traces, projects, pid, days, summ

    class Handler(BaseHTTPRequestHandler):
        server_version = SERVICE

        def log_message(self, fmt, *args):  # quiet: nothing about prompts is written anywhere
            pass

        def _local(self, page=False):
            # Claude Code's hooks and the side panel send no Origin; the graph page may send its own
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
            origin = self.headers.get("Origin")
            own = origin in ("http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT)
            return host in LOCAL_HOSTS and (not origin or (page and own))

        def _send(self, code, obj=None, body=None, ctype="application/json", headers=None):
            if body is None:
                body = json.dumps(obj).encode("utf-8") if obj is not None else b""
            self.send_response(code)
            if body:
                self.send_header("Content-Type", ctype)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._local(page=True):
                return self._send(403, {"error": "forbidden"})
            path, _, query = self.path.partition("?")
            if path == "/health":
                return self._send(200, {"service": SERVICE, "code": sig, "pid": os.getpid()})
            nocache = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
            try:
                if path in ("/graph", "/graph/"):
                    return self._send(200, body=PAGE.read_bytes(), ctype="text/html; charset=utf-8",
                                      headers=PAGE_HEADERS)
                if path == "/trace":
                    traces, projects, pid, days, summ = graph_data(parse_qs(query))
                    return self._send(200, {"projects": projects, "project": pid, "days": days, "summary": summ,
                                            "text": traces.text_tree(summ)}, headers=nocache)
                if path == "/graph.svg":
                    q = parse_qs(query)
                    traces, _, _, _, summ = graph_data(q)
                    out = traces.svg(summ, theme=(q.get("theme") or ["auto"])[0], width=(q.get("width") or [960])[0],
                                     animate=(q.get("anim") or ["0"])[0] == "1", header=(q.get("header") or ["0"])[0] == "1")
                    return self._send(200, body=out.encode("utf-8"), ctype="image/svg+xml; charset=utf-8",
                                      headers=dict(nocache, **{"Content-Security-Policy": "default-src 'none'; "
                                                                                        "style-src 'unsafe-inline'"}))
            except Exception as e:
                return self._send(500, {"error": type(e).__name__})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._local():
                return self._send(403, {"error": "forbidden"})
            path = self.path.split("?", 1)[0]
            state["last"] = time.monotonic()
            if path == "/shutdown":
                self._send(200, {"ok": True})
                threading.Thread(target=httpd.shutdown, daemon=True).start()
                return
            if path not in ("/route", "/tool", "/agent", "/turn"):
                return self._send(404, {"error": "not found"})
            out = ev = data = None
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(min(max(n, 0), MAX_BODY)).decode("utf-8") or "{}")
                if not isinstance(data, dict):
                    data = None
                elif path == "/tool":
                    if first_use(data):
                        later(hook_route.used, data)
                        later(learn_from, data)
                elif path == "/turn":  # Stop: the reply to the latest prompt is done
                    if data.get("hook_event_name") == "Stop" and first_time(
                            {"session_id": "turn", "prompt": "%s %s" % (data.get("session_id"), int(time.time()))}):
                        later(hook_route.turn, data)
                elif path == "/agent":
                    if data.get("hook_event_name") == "SubagentStop" and first_time(
                            {"session_id": "agent", "prompt": str(data.get("agent_id"))}):
                        later(hook_route.ran, data)
                elif first_time(data):
                    out, ev = hook_route.explain(data)
                    remember(data, ev)
            except Exception:
                out = None  # a hint must never break a prompt: answer "no hint"
            self._send(200, out)  # an empty 2xx body means "no hint" (or "go ahead") to Claude Code
            if ev:
                later(hook_route.note, ev, data.get("prompt"))

    httpd = _Server(("127.0.0.1", PORT), Handler)

    def idle_watch():
        while True:
            time.sleep(60)
            if time.monotonic() - state["last"] > IDLE_EXIT_S:
                httpd.shutdown()
                return

    def warm_up():  # load the engine's modules and the meaning model now, not on the first prompt
        try:
            hook_route.hint({"prompt": "warm up the routing model please", "cwd": str(HERE)})
            import router
            m = router._meaning_model()
            if m is not None:
                m.encode("warm up")
        except Exception:
            pass

    threading.Thread(target=idle_watch, daemon=True).start()
    threading.Thread(target=warm_up, daemon=True).start()
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def start_background():
    args = [sys.executable, "-S", str(Path(__file__).resolve()), "--serve"]
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True,
          "cwd": str(HERE)}
    if os.name == "nt":
        base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:  # leave the hook's job object, if it allows that, so the server outlives the hook process
            subprocess.Popen(args, creationflags=base | 0x01000000, **kw)  # CREATE_BREAKAWAY_FROM_JOB
        except OSError:
            subprocess.Popen(args, creationflags=base, **kw)
    else:
        subprocess.Popen(args, start_new_session=True, **kw)


def ensure():
    h = running()
    if h and h.get("code") == code_signature():
        return
    if h:  # an older version is running: replace it
        try:
            _call("POST", "/shutdown")
        except Exception:
            pass
        for _ in range(20):
            if running() is None:
                break
            time.sleep(0.05)
    start_background()
    for _ in range(60):  # wait up to 3 s so the session's first prompt already gets a hint
        if running():
            return
        time.sleep(0.05)


def stop():
    if running():
        try:
            _call("POST", "/shutdown")
        except Exception:
            pass


if __name__ == "__main__":
    arg = (sys.argv[1:] or ["--ensure"])[0]
    try:
        if arg == "--serve":
            serve()
        elif arg == "--stop":
            stop()
        else:
            ensure()
    except Exception:
        pass  # a session start must never fail because of the hint server
    sys.exit(0)
