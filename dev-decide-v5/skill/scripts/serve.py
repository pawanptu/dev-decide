#!/usr/bin/env python3
"""A small HTTP API, so your own apps can call dev-decide the way they would call Jev.

  python3 serve.py [--port 8765] [--host 127.0.0.1] [--cwd /path/to/repo]

GET  /              a web page with a live console (it holds no data; every call it makes needs a token)
GET  /health        version, whether Claude is available, today's model-call use, and the public demo token if any
POST /v1/decide
  {"state": "text to decide about",                      or "input": "DIFF" / "@path" / "COMMIT <sha>" (local only)
   "diff": "diff --git a/... b/...",                      optional: a unified diff sent inline, parsed like DIFF
   "questions": ["triage"] | {"intent": {"type": "choose", "options": ["refund", "dispute", "other"]}},
   "items": [{"id": "T-1", "state": "...", "diff": "..."}],   optional batch instead of "state"
   "mode": "hybrid" | "local" | "llm",  "why": false}
-> the same JSON the CLI prints.
POST /v1/feedback  {"id": "<decision id> or last", "answers": {"issue_type": "bug"}, "item": "<item id, batches only>"}
                   corrects (or, with "ok", confirms) answers: they are learned from at once and remembered
GET  /v1/presets   the questions this server knows, with their options (for feedback forms)
GET  /v1/learning  what has been learned, spot-check results and the latest model switches

Tokens:
  DEV_DECIDE_TOKEN       (16+ characters) full access: model calls on this server's key, feedback, learning status.
                         Send "Authorization: Bearer <token>" (or "X-Dev-Decide-Token: <token>").
  DEV_DECIDE_DEMO_TOKEN  optional public token for a demo (the page fills it in): answers are local only unless the
                         caller brings their own key; nothing about demo requests is stored or learned from, and
                         feedback and /v1/learning are refused.
Bring-your-own-key: a request may carry "X-Anthropic-Api-Key: sk-ant-...", or "X-OpenAI-Api-Key" or
"X-Gemini-Api-Key" (one per request). That key pays for that request's model calls only; it is never stored, logged
or cached, and those calls do not count against the daily cap. An OpenAI or Gemini key goes only to the endpoint
and model this server is set up with (OPENAI_BASE_URL, DEV_DECIDE_MODEL_OPENAI, DEV_DECIDE_MODEL_GEMINI).

Limits and modes (per server process, so a restart resets the counters):
  DEV_DECIDE_RATE_PER_MIN         requests per minute from one client address (default 30; 0 turns it off)
  DEV_DECIDE_MODEL_CALLS_PER_DAY  model calls per UTC day on this server's key; past it, requests are answered
                                  locally (default 300; 0 = no cap). From 80% on, responses carry a warning.
  DEV_DECIDE_LOCAL_ONLY=1         never use this server's key: every request is answered locally (own keys still work)
  DEV_DECIDE_TRUST_PROXY=1        take the client address from X-Forwarded-For (set it only behind a proxy that writes it)
  DEV_DECIDE_STATE_REPO           keep what the server learns in a git repository (see statesync.py)

It listens on 127.0.0.1 by default. To listen beyond this machine (--host 0.0.0.0, as in a container), a token
is required, and DIFF, COMMIT and @path are not read (callers send "diff" instead); pass --allow-no-auth only
behind your own authenticating proxy, and --allow-file-refs only if every caller may read this machine's files.
"""
import argparse
import hmac
import json
import os
import re
import signal
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ddcore as dd  # noqa: E402
import engine  # noqa: E402
import inputs  # noqa: E402

MAX_BODY = 4 * 1024 * 1024
MIN_TOKEN = 16
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
WARN_AT = 0.8
API_KEY_HEADER = "X-Anthropic-Api-Key"
API_KEY_FORMAT = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,300}$")
# Other providers' keys: (header, provider, format). They reach only the endpoint this server is set up with
# (OPENAI_BASE_URL or DEV_DECIDE_GEMINI_BASE_URL on the server), and the server's model.openai / model.gemini.
OTHER_KEY_HEADERS = (("X-OpenAI-Api-Key", "openai", re.compile(r"^[A-Za-z0-9_\-\.]{20,300}$")),
                     ("X-Gemini-Api-Key", "gemini", re.compile(r"^[A-Za-z0-9_\-]{20,300}$")))
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}
PAGE_CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; img-src data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
# The page's scripts: its own code and Alpine's CSP build (no eval), served from the skill folder.
ASSETS = {"/ui.js": ("ui.js", "no-cache"), "/vendor/alpine-csp.min.js": ("vendor/alpine-csp.min.js", "public, max-age=86400")}


def _env_int(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


class Limits(object):
    """A per-client request rate and a daily cap on model calls, so a leaked token cannot run up a bill."""

    def __init__(self, per_min, calls_per_day):
        self.per_min, self.calls_per_day = per_min, calls_per_day
        self.lock = threading.Lock()
        self.hits = {}           # client address -> deque of request times in the last minute
        self.day, self.calls = None, 0
        self.warned = set()      # thresholds already logged today

    def allow(self, client):
        if not self.per_min:
            return True
        now = time.monotonic()
        with self.lock:
            q = self.hits.setdefault(client, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_min:
                return False
            q.append(now)
            if len(self.hits) > 10000:  # forget idle clients so memory stays bounded
                for k in [k for k, v in self.hits.items() if not v or now - v[-1] > 60]:
                    del self.hits[k]
            return True

    def _today(self):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self.day:
            self.day, self.calls, self.warned = day, 0, set()

    def model_allowed(self):
        with self.lock:
            self._today()
            return not self.calls_per_day or self.calls < self.calls_per_day

    def spent(self, n):
        with self.lock:
            self._today()
            self.calls += int(n or 0)
            for share in (WARN_AT, 1.0):  # one log line per threshold per day
                if self.calls_per_day and self.calls >= share * self.calls_per_day and share not in self.warned:
                    self.warned.add(share)
                    sys.stderr.write("dev-decide warning: %d of %d model calls used today (%d%%)\n"
                                     % (self.calls, self.calls_per_day, round(100.0 * self.calls / self.calls_per_day)))

    def usage(self):
        with self.lock:
            self._today()
            cap = self.calls_per_day
            share = (self.calls / float(cap)) if cap else 0.0
            return {"used": self.calls, "cap": cap or None, "share": round(share, 3), "warning": bool(cap) and share >= WARN_AT}


def make_handler(cwd, token=None, local_only=True, limits=None, trust_proxy=False, demo_token=None, server_local=False):
    """local_only: listening on this machine only. server_local: never use this server's own key (DEV_DECIDE_LOCAL_ONLY)."""
    secret = token.encode("utf-8") if token else None
    demo = demo_token.encode("utf-8") if demo_token else None
    limits = limits or Limits(0, 0)

    class Handler(BaseHTTPRequestHandler):
        server_version = "dev-decide/" + dd.VERSION
        timeout = 60  # seconds a client may stall while sending; model calls are not limited by this

        def end_headers(self):
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            if trust_proxy and (self.headers.get("X-Forwarded-Proto") or "").lower() == "https":
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            super().end_headers()

        def _send(self, code, obj, headers=None):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # the request line and status only: never headers (tokens, keys)
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

        def _role(self):
            """"admin", "demo", or None (not authorized)."""
            if not self._same_machine():
                return None
            if secret is None:
                return "admin"
            sent = (self.headers.get("X-Dev-Decide-Token") or "").strip()
            if not sent:
                auth = (self.headers.get("Authorization") or "").strip()
                if auth[:7].lower() == "bearer ":
                    sent = auth[7:].strip()
            if not sent:
                return None
            if hmac.compare_digest(sent.encode("utf-8"), secret):
                return "admin"
            if demo is not None and hmac.compare_digest(sent.encode("utf-8"), demo):
                return "demo"
            return None

        def _api_key(self):
            """The caller's own (provider, key), or (None, None); ValueError when a key header is malformed or
            more than one is sent."""
            found = []
            key = (self.headers.get(API_KEY_HEADER) or "").strip()
            if key:
                if not API_KEY_FORMAT.match(key):
                    raise ValueError("the %s header does not look like an Anthropic API key (sk-ant-...)"
                                     % API_KEY_HEADER)
                found.append(("anthropic", key))
            for header, provider, fmt in OTHER_KEY_HEADERS:
                key = (self.headers.get(header) or "").strip()
                if key:
                    if not fmt.match(key):
                        raise ValueError("the %s header does not look like an API key" % header)
                    found.append((provider, key))
            if len(found) > 1:
                raise ValueError("send one API key header per request, not %d" % len(found))
            return found[0] if found else (None, None)

        def _client(self):
            if trust_proxy:  # the last entry is the one our own proxy added; earlier ones can be forged
                fwd = [x.strip() for x in (self.headers.get("X-Forwarded-For") or "").split(",") if x.strip()]
                if fwd:
                    return fwd[-1]
            return self.client_address[0]

        def _throttled(self):
            if limits.allow(self._client()):
                return False
            self._send(429, {"error": "too many requests: wait a minute and try again"}, {"Retry-After": "60"})
            return True

        def _deny(self):
            self._send(401, {"error": "unauthorized: send the header Authorization: Bearer <your token>"},
                       {"WWW-Authenticate": "Bearer"})

        def _forbid(self, what):
            self._send(403, {"error": "the demo token cannot %s; use your own token" % what})

        def _health(self):
            out = {"ok": True, "version": dd.VERSION, "model": "none" if server_local else _model_backend(cwd),
                   "model_calls_left": limits.model_allowed(), "model_calls": limits.usage(),
                   "local_only": server_local, "learning": _learning_on(cwd), "byok": True,
                   "own_keys": _own_keys(cwd)}
            if demo_token:
                out["demo_token"] = demo_token  # public by design: it can only get local answers
            return out

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == "/health":
                self._send(200, self._health())
            elif path in ("", "/ui"):  # the page holds no data; every call it makes still needs a token
                page = (dd.SKILL_DIR / "ui.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Content-Security-Policy", PAGE_CSP)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(page)
            elif path in ASSETS:
                name, cache = ASSETS[path]
                body = (dd.SKILL_DIR / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", cache)
                self.end_headers()
                self.wfile.write(body)
            elif self._throttled():
                return
            else:
                role = self._role()
                if role is None:
                    self._deny()
                elif path == "/v1/presets":
                    self._send(200, _presets(cwd))
                elif path == "/v1/learning":
                    if role == "demo":
                        return self._forbid("read the learning status")
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
            if self._throttled():
                return
            role = self._role()
            if role is None:
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
                if role == "demo":
                    return self._forbid("send feedback")
                import learn
                answers = spec.get("answers")
                if not isinstance(answers, dict) or not answers:
                    self._send(400, {"error": "feedback needs \"answers\": {\"key\": \"correct value\"}"})
                    return
                out = learn.feedback(cwd, spec.get("id") or "last", answers, spec.get("item"))
                self._send(400 if out.get("error") else 200, out)
                return
            try:
                provider, api_key = self._api_key()
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            notes = []
            wants_model = str(spec.get("mode") or "").lower() != "local"
            if api_key is None and wants_model:  # this server's own key would be used
                if role == "demo":
                    spec["mode"] = "local"
                    notes.append("demo token: answered locally; add your own API key to ask a model")
                elif server_local:
                    spec["mode"] = "local"
                    notes.append("this server answers locally only; add your own API key to ask a model")
                elif not limits.model_allowed():
                    spec["mode"] = "local"
                    notes.append("the server's daily model-call cap is reached; answered locally")
            try:
                out = engine.decide(spec=spec, cwd=cwd, api_key=api_key, api_provider=provider or "anthropic",
                                    ephemeral=(role == "demo"))
            except Exception as exc:
                self._send(500, {"error": "engine error: %s" % type(exc).__name__})
                return
            meta = out.get("meta") if isinstance(out.get("meta"), dict) else None
            if meta is not None:
                if api_key is None:
                    limits.spent(meta.get("model_calls"))
                else:
                    meta["own_key"] = True  # paid by the caller; not counted against the server's cap
                u = limits.usage()
                if u["warning"] and api_key is None and role == "admin":
                    notes.append("this server has used %d of %d model calls today (%d%%)"
                                 % (u["used"], u["cap"], round(100 * u["share"])))
                if notes:
                    meta["warnings"] = (meta.get("warnings") or []) + notes
            self._send(400 if out.get("error") and not out.get("answers") and not out.get("items") else 200, out)

    return Handler


def _own_keys(cwd):
    """Whose keys a caller may bring: Anthropic always; OpenAI and Gemini once this server names a model for them."""
    out = ["anthropic"]
    try:
        policy = dd.load_policy(dd.project_root(cwd))
        out += [p for p in ("openai", "gemini") if dd.model_for(policy, p)]
    except Exception:
        pass
    return out


def _model_backend(cwd):
    """Which way this server can ask a model: api, cli, openai, gemini, command or none (the page greys out model
    modes on none)."""
    try:
        import llm
        return llm.backend_choice(dd.load_policy(dd.project_root(cwd)))
    except Exception:
        return "none"


def _learning_on(cwd):
    try:
        import learn
        return bool(learn.config(dd.load_policy(dd.project_root(cwd)))["enabled"])
    except Exception:
        return False


def _presets(cwd):
    """Every question this server knows, with the answers it accepts (for feedback forms)."""
    root = dd.project_root(cwd)
    policy = dd.load_policy(root)
    cat = dd.load_catalog(policy)
    out = {}
    for kind in ("choose", "score", "verify"):
        for name, spec in cat[kind].items():
            spec = spec if isinstance(spec, dict) else {}
            if kind == "choose":
                opts = spec.get("options")
                if opts == "auto":
                    opts = [r["name"] for r in dd.discover_routes(root, policy)]
                opts = list(opts or [])
                if spec.get("allow_none") and "none" not in opts:
                    opts.append("none")
                out[name] = {"type": "choose", "options": opts}
            elif kind == "score":
                out[name] = {"type": "score", "min": int(spec.get("min", 1)), "max": int(spec.get("max", 5))}
            else:
                out[name] = {"type": "verify", "options": ["true", "false"]}
    return {"presets": out, "bundles": cat["bundles"]}


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
    demo_token = (os.environ.get("DEV_DECIDE_DEMO_TOKEN") or "").strip() or None
    local = args.host in LOCAL_HOSTS
    for name, value in ((args.token_env, token), ("DEV_DECIDE_DEMO_TOKEN", demo_token)):
        if value and len(value) < MIN_TOKEN:
            sys.stderr.write("%s is too short (%d characters; use at least %d). Make one with:\n"
                             "  python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
                             % (name, len(value), MIN_TOKEN))
            return 2
    if demo_token and not token:
        sys.stderr.write("DEV_DECIDE_DEMO_TOKEN needs %s as well (the demo token is the limited one)\n" % args.token_env)
        return 2
    if demo_token and token and hmac.compare_digest(demo_token, token):
        sys.stderr.write("DEV_DECIDE_DEMO_TOKEN must differ from %s: the demo token is public\n" % args.token_env)
        return 2
    if not local and not token and not args.allow_no_auth:
        sys.stderr.write("refusing to listen on %s without a token: set %s (at least %d characters), "
                         "or pass --allow-no-auth if your own proxy authenticates every request\n"
                         % (args.host, args.token_env, MIN_TOKEN))
        return 2
    if not local and not args.allow_file_refs:
        inputs.REFS = False  # remote callers must not read this machine's files; they send "diff" instead
    if not local:
        os.environ.setdefault("DEV_DECIDE_TRACE", "0")  # the decision graph is each person's own, not a shared server's

    cwd = str(Path(args.cwd).resolve())
    sync = None
    if os.environ.get("DEV_DECIDE_STATE_REPO"):
        import statesync
        sync = statesync.StateSync.from_env(cwd)
        if sync is not None:
            sync.restore()  # what this server learned before its last restart
            sync.start()
    limits = Limits(_env_int("DEV_DECIDE_RATE_PER_MIN", 30), _env_int("DEV_DECIDE_MODEL_CALLS_PER_DAY", 300))
    trust_proxy = os.environ.get("DEV_DECIDE_TRUST_PROXY") == "1"
    server_local = os.environ.get("DEV_DECIDE_LOCAL_ONLY") == "1"
    httpd = ThreadingHTTPServer((args.host, args.port),
                                make_handler(cwd, token, local, limits, trust_proxy, demo_token, server_local))
    sys.stderr.write("dev-decide %s listening on http://%s:%d (repo: %s; %s%s; file references %s%s)\n"
                     % (dd.VERSION, args.host, args.port, cwd,
                        "token required" if token else "no authentication",
                        " + public demo token" if demo_token else "",
                        "on" if inputs.REFS else "off",
                        "; local answers only" if server_local else ""))
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
        if sync is not None:
            sync.stop()  # save what was learned since the last save
    return 0


if __name__ == "__main__":
    sys.exit(main())
