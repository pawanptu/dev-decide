#!/usr/bin/env python3
"""The routing hint without a new process per prompt: a small local server Claude Code's HTTP hook calls.

A command hook starts Python, imports the engine and loads the meaning model on every prompt (about 0.3 s on
Windows). This server keeps all of that loaded, so a hint takes a few milliseconds:

  UserPromptSubmit  {"type": "http", "url": "http://127.0.0.1:47321/route", "timeout": 2}
  SessionStart      {"type": "command", "command": "sh <scripts>/run.sh hookd.py --ensure"}

  hookd.py --ensure   start the server unless one with this code is already running (prints nothing)
  hookd.py --serve    run in the foreground (what --ensure starts in the background)
  hookd.py --stop     stop a running server

It listens on 127.0.0.1 only, answers no browser (requests with an Origin header, or a Host other than
localhost, are refused), reads only agent and skill descriptions (as the command hook does), writes nothing,
and exits after DEV_DECIDE_HOOK_IDLE_HOURS (default 12) without a request. The port is DEV_DECIDE_HOOK_PORT
(default 47321). If the server is not running, Claude Code shows a non-blocking hook error and the prompt
goes ahead without a hint; the next session start restarts it.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PORT = int(os.environ.get("DEV_DECIDE_HOOK_PORT") or 47321)
IDLE_EXIT_S = float(os.environ.get("DEV_DECIDE_HOOK_IDLE_HOURS") or 12) * 3600
MAX_BODY = 1 << 20
LOCAL_HOSTS = ("127.0.0.1", "localhost")
SERVICE = "dev-decide-hook"


def code_signature():
    """Changes whenever the engine's files change, so --ensure replaces a server running older code."""
    h = hashlib.sha256()
    for f in sorted(HERE.glob("*.py")):
        st = f.stat()
        h.update(("%s|%d|%d\n" % (f.name, st.st_size, int(st.st_mtime))).encode())
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

    class Handler(BaseHTTPRequestHandler):
        server_version = SERVICE

        def log_message(self, fmt, *args):  # quiet: nothing about prompts is written anywhere
            pass

        def _local(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
            return host in LOCAL_HOSTS and not self.headers.get("Origin")

        def _send(self, code, obj=None):
            body = json.dumps(obj).encode("utf-8") if obj is not None else b""
            self.send_response(code)
            if obj is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._local():
                return self._send(403, {"error": "forbidden"})
            if self.path.split("?", 1)[0] == "/health":
                return self._send(200, {"service": SERVICE, "code": sig, "pid": os.getpid()})
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
            if path != "/route":
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(min(max(n, 0), MAX_BODY)).decode("utf-8") or "{}")
                out = hook_route.hint(data) if isinstance(data, dict) else None
            except Exception:
                out = None  # a hint must never break a prompt: answer "no hint"
            self._send(200, out)  # an empty 2xx body means "no hint" to Claude Code

    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    httpd.daemon_threads = True

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
