"""The teacher: the Claude model that writes and labels bootstrap data.

Backends:
  cli   `claude -p` with your Claude Code sign-in (no tools, no settings, one turn)
  api   Anthropic Messages API when ANTHROPIC_API_KEY is set (prompt-cached system prompt)
  mock  a deterministic offline stand-in that understands the kit's own prompts (tests, dry runs)

Every call goes through a hard call budget and an on-disk cache, so a re-run after a crash
or a change to the training code never pays for the same answer twice.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .common import KitError, log, read_json, sha, write_json

API_MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
CHILD_ENV_DROP = ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_DIAGNOSTICS_FILE",
                  "CLAUDE_CODE_TEE_SDK_STDOUT")
CACHE_VERSION = "t1"


LIMIT_MARKERS = ("hit your session limit", "hit your usage limit", "usage limit reached", "rate limit reached",
                 "credit balance is too low")


def _limit_message(text):
    """Claude Code returns plan and usage limits as ordinary reply text; they must never pass for an answer."""
    low = (text or "")[:300].lower()
    return len(low) < 300 and any(m in low for m in LIMIT_MARKERS)


class BudgetExceeded(KitError):
    pass


class Budget(object):
    """A hard cap on model calls, shared by every teacher in one run."""

    def __init__(self, max_calls):
        self.max_calls = max_calls
        self.used = 0
        self._lock = threading.Lock()

    def take(self):
        with self._lock:
            if self.max_calls is not None and self.used >= self.max_calls:
                raise BudgetExceeded("call budget of %d reached" % self.max_calls)
            self.used += 1


def pick_backend(requested="auto"):
    requested = (requested or "auto").lower()
    if requested in ("mock", "cli", "api"):
        if requested == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
            raise KitError("backend api needs ANTHROPIC_API_KEY")
        if requested == "cli" and not shutil.which("claude"):
            raise KitError("backend cli needs the claude command (Claude Code)")
        return requested
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    if shutil.which("claude"):
        return "cli"
    raise KitError("no teacher available: install Claude Code (claude) or set ANTHROPIC_API_KEY")


class Teacher(object):
    def __init__(self, model, backend="auto", budget=None, workers=3, cache_dir=None, timeout=240,
                 max_tokens=8000, mock_fn=None, temperature=1.0):
        self.model = model
        self.temperature = temperature  # honored by the api backend; claude -p uses its default
        self.backend = pick_backend(backend)
        self.budget = budget or Budget(None)
        self.workers = max(1, int(workers))
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.mock_fn = mock_fn
        self.calls = 0
        self.cache_hits = 0
        self.failures = 0
        self.cost_usd = 0.0
        self.seconds = 0.0
        self._lock = threading.Lock()
        if self.backend == "mock" and mock_fn is None:
            raise KitError("mock backend needs a mock function")

    # ------------------------------------------------------------ public

    def ask(self, system, user):
        """Return (text, info). Raises BudgetExceeded when the cap is hit."""
        key = sha(CACHE_VERSION, self.backend if self.backend == "mock" else "live", self.model, system, user)
        cached = self._cache_get(key)
        if cached is not None:
            with self._lock:
                self.cache_hits += 1
            return cached.get("text", ""), dict(cached.get("info", {}), cached=True)
        self.budget.take()
        started = time.time()
        text, info, err = "", {}, None
        for attempt in (1, 2):
            try:
                text, info = self._call(system, user)
                if text.strip():
                    break
                err = "empty reply"
            except BudgetExceeded:
                raise
            except Exception as exc:  # network, CLI or API errors: retry once
                err = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            if attempt == 1:
                time.sleep(1.5)
        elapsed = time.time() - started
        with self._lock:
            self.calls += 1
            self.seconds += elapsed
            self.cost_usd += float(info.get("cost_usd") or 0.0)
        if not text.strip():
            with self._lock:
                self.failures += 1
            return "", {"error": err or "no reply", "seconds": round(elapsed, 2)}
        info["seconds"] = round(elapsed, 2)
        self._cache_put(key, {"text": text, "info": info})
        return text, info

    def ask_many(self, prompts, label="calls"):
        """prompts: list of (system, user). Returns a list of (text, info) in the same order."""
        results = [None] * len(prompts)
        done = [0]
        lock = threading.Lock()
        stopped = []

        def run(i):
            if stopped:
                results[i] = ("", {"error": "skipped: budget reached"})
                return
            try:
                results[i] = self.ask(*prompts[i])
            except BudgetExceeded as exc:
                stopped.append(str(exc))
                results[i] = ("", {"error": str(exc)})
            with lock:
                done[0] += 1
                if done[0] == len(prompts) or done[0] % max(1, len(prompts) // 10) == 0:
                    log("  %s: %d/%d" % (label, done[0], len(prompts)))

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            list(ex.map(run, range(len(prompts))))
        if stopped:
            log("  stopped early: %s" % stopped[0])
        return results

    def summary(self):
        return {"model": self.model, "backend": self.backend, "calls": self.calls, "cache_hits": self.cache_hits,
                "failures": self.failures, "cost_usd": round(self.cost_usd, 4),
                "avg_seconds": round(self.seconds / self.calls, 2) if self.calls else None}

    # ------------------------------------------------------------ backends

    def _call(self, system, user):
        if self.backend == "mock":
            return self.mock_fn(system, user), {"backend": "mock"}
        if self.backend == "api":
            return self._call_api(system, user)
        return self._call_cli(system, user)

    def _call_cli(self, system, user):
        exe = shutil.which("claude")
        if exe is None:
            raise RuntimeError("the claude command was not found")
        fd, sysfile = tempfile.mkstemp(prefix="ddkit-system-", suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(system)
            args = [exe, "-p", "--model", self.model, "--system-prompt-file", sysfile, "--max-turns", "1",
                    "--tools", "", "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
                    "--output-format", "json"]
            env = dict(os.environ)
            env["DEV_DECIDE_CHILD"] = "1"
            for var in CHILD_ENV_DROP:
                env.pop(var, None)
            proc = subprocess.run(args, input=user, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=self.timeout, env=env, cwd=tempfile.gettempdir())
        finally:
            try:
                os.unlink(sysfile)
            except OSError:
                pass
        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out)
        except ValueError:
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or out)[:300] or "claude exited %d" % proc.returncode)
            return out, {"backend": "cli:%s" % self.model}
        text = data.get("result") or ""
        if data.get("is_error") or _limit_message(text):  # e.g. "You've hit your session limit": not an answer
            raise RuntimeError((text or str(data.get("subtype") or "model call failed"))[:300])
        usage = data.get("usage") or {}
        return text, {"backend": "cli:%s" % self.model, "cost_usd": data.get("total_cost_usd"),
                      "output_tokens": usage.get("output_tokens")}

    def _call_api(self, system, user):
        model = API_MODELS.get(self.model, self.model)
        body = {"model": model, "max_tokens": self.max_tokens, "temperature": self.temperature,
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": user}]}
        url = os.environ.get("DEV_DECIDE_API_URL") or "https://api.anthropic.com/v1/messages"
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
            "x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""), "anthropic-version": "2023-06-01",
            "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 529):
                time.sleep(3.0)
            raise RuntimeError("API error %d: %s" % (exc.code, exc.read().decode("utf-8", "replace")[:200]))
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage") or {}
        return text, {"backend": "api:%s" % model, "output_tokens": usage.get("output_tokens"),
                      "input_tokens": usage.get("input_tokens")}

    # ------------------------------------------------------------ cache

    def _cache_path(self, key):
        return None if not self.cache_dir else self.cache_dir / key[:2] / (key + ".json")

    def _cache_get(self, key):
        p = self._cache_path(key)
        return read_json(p) if p and p.exists() else None

    def _cache_put(self, key, obj):
        p = self._cache_path(key)
        if p:
            write_json(p, obj, indent=None)
