#!/usr/bin/env python3
"""A small HTTP API, so your own apps can call dev-decide the way they would call Jev.

  python3 serve.py [--port 8765] [--host 127.0.0.1] [--cwd /path/to/repo]

POST /v1/decide
  {"state": "text to decide about",                      or "input": "DIFF" / "@path" / "COMMIT <sha>" (local only)
   "diff": "diff --git a/... b/...",                      optional: a unified diff sent inline, parsed like DIFF
   "questions": ["triage"] | {"intent": {"type": "choose", "options": ["refund", "dispute", "other"]}},
   "items": [{"id": "T-1", "state": "...", "diff": "..."}],   optional batch instead of "state"
   "mode": "hybrid" | "local" | "llm",  "why": false}
-> the same JSON the CLI prints.
GET /health -> {"ok": true, "version": "..."}
POST /v1/feedback  {"id": "<decision id> or last", "answers": {"issue_type": "bug"}, "item": "<item id, batches only>"}
                   corrects (or, with "ok", confirms) answers: they are learned from at once and remembered
GET /v1/learning -> what has been learned, spot-check results and the latest model switches

Authentication: when the environment variable DEV_DECIDE_TOKEN is set (16 characters or more), every
endpoint except /health needs "Authorization: Bearer <token>" (or "X-Dev-Decide-Token: <token>").
It listens on 127.0.0.1 by default. To listen beyond this machine (--host 0.0.0.0, as in a container),
a token is required, and DIFF, COMMIT and @path are not read (callers send "diff" instead); pass
--allow-no-auth only behind your own authenticating proxy, and --allow-file-refs only if every caller
may read this machine's files. Custom questions are answered by the model tier until you define them as
presets in .claude/dev-decide.json, log answers and train them.
"""
import argparse
import hmac
import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ddcore as dd  # noqa: E402
import engine  # noqa: E402
import inputs  # noqa: E402

MAX_BODY = 4 * 1024 * 1024
MIN_TOKEN = 16
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def make_handler(cwd, token=None, local_only=True):
    secret = token.encode("utf-8") if token else None

    class Handler(BaseHTTPRequestHandler):
        server_version = "dev-decide/" + dd.VERSION
        timeout = 60  # seconds a client may stall while sending; model calls are not limited by this

        def _send(self, code, obj, headers=None):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            sys.stderr.write("dev-decide %s\n" % (fmt % args))

        def _same_machine(self):
            """Without a token, refuse browsers acting for other websites (DNS rebinding, cross-site POSTs)."""
            if secret is not None or not local_only:
                return True
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            origin = self.headers.get("Origin")
            if host not in LOCAL_HOSTS:
                return False
            if origin:
                o = origin.split("://", 1)[-1].rsplit(":", 1)[0].strip("[]").lower()
                return o in LOCAL_HOSTS
            return True

        def _authorized(self):
            if not self._same_machine():
                return False
            if secret is None:
                return True
            sent = (self.headers.get("X-Dev-Decide-Token") or "").strip()
            if not sent:
                auth = (self.headers.get("Authorization") or "").strip()
                if auth[:7].lower() == "bearer ":
                    sent = auth[7:].strip()
            return bool(sent) and hmac.compare_digest(sent.encode("utf-8"), secret)

        def _deny(self):
            self._send(401, {"error": "unauthorized: send the header Authorization: Bearer <your DEV_DECIDE_TOKEN>"},
                       {"WWW-Authenticate": "Bearer"})

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == "/health":
                self._send(200, {"ok": True, "version": dd.VERSION})
            elif path in ("", "/ui"):  # the page holds no data; every call it makes still needs the token
                page = (dd.SKILL_DIR / "ui.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                                 "script-src 'unsafe-inline'; connect-src 'self'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(page)
            elif not self._authorized():
                self._deny()
            elif path == "/v1/learning":
                import learn
                self._send(200, learn.command("status", cwd=cwd))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if not self._authorized():
                if 0 < length <= MAX_BODY:
                    self.rfile.read(length)  # drain it, so the client reads the 401 instead of a reset
                self._deny()
                return
            if path not in ("/v1/decide", "/v1/feedback"):
                self._send(404, {"error": "not found"})
                return
            if length <= 0 or length > MAX_BODY:
                self._send(400, {"error": "send a JSON body under 4 MB"})
                return
            try:
                spec = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                self._send(400, {"error": "body is not valid JSON"})
                return
            if not isinstance(spec, dict):
                self._send(400, {"error": "body must be a JSON object"})
                return
            if path == "/v1/feedback":
                import learn
                answers = spec.get("answers")
                if not isinstance(answers, dict) or not answers:
                    self._send(400, {"error": "feedback needs \"answers\": {\"key\": \"correct value\"}"})
                    return
                out = learn.feedback(cwd, spec.get("id") or "last", answers, spec.get("item"))
                self._send(400 if out.get("error") else 200, out)
                return
            try:
                out = engine.decide(spec=spec, cwd=cwd)
            except Exception as exc:
                self._send(500, {"error": "engine error: %s" % type(exc).__name__})
                return
            self._send(400 if out.get("error") and not out.get("answers") and not out.get("items") else 200, out)

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--cwd", default=".")
    ap.add_argument("--token-env", default="DEV_DECIDE_TOKEN",
                    help="environment variable that holds the access token (default DEV_DECIDE_TOKEN)")
    ap.add_argument("--allow-no-auth", action="store_true",
                    help="listen beyond localhost without a token (only behind your own authenticating proxy)")
    ap.add_argument("--allow-file-refs", action="store_true",
                    help="let callers use DIFF, COMMIT and @path even when listening beyond localhost")
    args = ap.parse_args()

    token = (os.environ.get(args.token_env) or "").strip() or None
    local = args.host in LOCAL_HOSTS
    if token and len(token) < MIN_TOKEN:
        sys.stderr.write("%s is too short (%d characters; use at least %d). Make one with:\n"
                         "  python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
                         % (args.token_env, len(token), MIN_TOKEN))
        return 2
    if not local and not token and not args.allow_no_auth:
        sys.stderr.write("refusing to listen on %s without a token: set %s (at least %d characters), "
                         "or pass --allow-no-auth if your own proxy authenticates every request\n"
                         % (args.host, args.token_env, MIN_TOKEN))
        return 2
    if not local and not args.allow_file_refs:
        inputs.REFS = False  # remote callers must not read this machine's files; they send "diff" instead

    cwd = str(Path(args.cwd).resolve())
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(cwd, token, local))
    sys.stderr.write("dev-decide %s listening on http://%s:%d (repo: %s; %s; file references %s)\n"
                     % (dd.VERSION, args.host, args.port, cwd,
                        "token required" if token else "no authentication",
                        "on" if inputs.REFS else "off"))
    if not token and not local:
        sys.stderr.write("warning: no authentication; anyone who can reach %s:%d can use it\n" % (args.host, args.port))

    def _stop(signum, frame):  # `docker stop` and platform restarts send SIGTERM; stop at once
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
