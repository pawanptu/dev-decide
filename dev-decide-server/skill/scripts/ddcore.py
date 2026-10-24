"""Shared helpers for dev-decide.

Standard library only. Works on Python 3.8 and later.
"""
import hashlib
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path

VERSION = "5.0.1"
SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent
SKILL_NAME = "dev-decide"
LEGACY_AGENT = "dev-decide-engine"
DEFAULT_THRESHOLD = 0.6
MAX_ROUTES = 40

BUILTIN_ROUTES = [
    ("general-purpose", "agent", "Built-in agent for multi-step research and code changes when no specialist fits"),
    ("Explore", "agent", "Built-in read-only agent for searching and understanding a codebase"),
    ("Plan", "agent", "Built-in agent that researches the code and designs an implementation plan"),
]

API_MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}


# ---------------------------------------------------------------- environment

def home_claude():
    return Path(os.path.expanduser("~")) / ".claude"


def run(cmd, cwd=None, timeout=5.0):
    """Run a command and return stdout, or None on any failure."""
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout,
                             text=True, encoding="utf-8", errors="replace")
        if out.returncode == 0:
            return out.stdout
    except Exception:
        pass
    return None


_ROOTS = {}  # (cwd, CLAUDE_PROJECT_DIR) -> (when, root): a long-running process asks git once per ROOT_TTL_S
ROOT_TTL_S = 30.0


def project_root(cwd=None):
    base = Path(cwd) if cwd else Path.cwd()
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    key = (str(base), env)
    hit = _ROOTS.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < ROOT_TTL_S:
        return hit[1]
    top = run(["git", "rev-parse", "--show-toplevel"], cwd=str(base))
    if top and top.strip():
        root = Path(top.strip())
    elif env and Path(env).is_dir():
        root = Path(env)
    else:
        root = base
    _ROOTS[key] = (now, root)
    return root


def state_dir(root):
    """Per-repo folder for logs, labels, models and cache. Always gitignored."""
    d = Path(root) / ".claude" / "dev-decide"
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    return d


log_dir = state_dir  # v2 name, kept for label.py and calibrate.py


def stable_hash(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()[:24]


# ---------------------------------------------------------------- json files

def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def write_json(path, obj, indent=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent, separators=None if indent else (",", ":")) + "\n",
                   encoding="utf-8")
    os.replace(str(tmp), str(path))


def read_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except (FileNotFoundError, NotADirectoryError):
        pass
    return rows


def append_jsonl(path, rows):
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------- policy and catalog

def policy_path(root):
    return Path(root) / ".claude" / "dev-decide.json"


def load_policy(root):
    """User policy (~/.claude/dev-decide.json) overlaid by project policy (.claude/dev-decide.json)."""
    user = read_json(home_claude() / "dev-decide.json")
    proj = read_json(policy_path(root))
    user = user if isinstance(user, dict) else {}
    proj = proj if isinstance(proj, dict) else {}
    return deep_merge(user, proj)


def policy_get(policy, dotted, default=None):
    cur = policy
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def load_catalog(policy):
    base = read_json(SKILL_DIR / "presets.json")
    custom = policy.get("presets") if isinstance(policy.get("presets"), dict) else {}
    cat = {}
    for kind in ("choose", "score", "verify"):
        merged = dict(base.get(kind, {}))
        merged.update(custom.get(kind) or {})
        cat[kind] = merged
    cat["bundles"] = dict(base.get("bundles", {}))
    cat["bundles"].update(policy.get("bundles") or {})
    cat["gates"] = dict(base.get("gates", {}))
    cat["gates"].update(policy.get("gates") or {})
    return cat


def find_preset(cat, name):
    if not name:
        return None, None
    for kind in ("choose", "score", "verify"):
        if name in cat[kind]:
            spec = cat[kind][name]
            if kind == "verify" and isinstance(spec, str):
                spec = {"statement": spec}
            return kind, dict(spec)
    return None, None


def threshold_for(policy, key, preset):
    cal = (policy.get("calibration") or {}).get(preset or key) or {}
    if "threshold" in cal:
        try:
            return float(cal["threshold"])
        except (TypeError, ValueError):
            pass
    th = policy.get("thresholds") or {}
    for k in (key, preset, "default"):
        if k and k in th:
            try:
                return float(th[k])
            except (TypeError, ValueError):
                pass
    return DEFAULT_THRESHOLD


def temperature_for(policy, name):
    """Calibration temperature for model (LLM) answers to a preset; 1.0 when not calibrated."""
    cal = (policy.get("calibration") or {}).get(name) or {}
    try:
        t = float(cal.get("temperature", 1.0))
    except (TypeError, ValueError):
        t = 1.0
    return t if 0.2 <= t <= 5.0 else 1.0


def model_for(policy, backend):
    env = os.environ.get("DEV_DECIDE_MODEL")
    m = policy.get("model")
    if isinstance(m, dict):
        m = m.get(backend)
    value = env or (m if isinstance(m, str) and m else None)
    if backend == "api":
        if not value:
            return API_MODELS["haiku"]
        return API_MODELS.get(value, value)
    return value or "haiku"


# ---------------------------------------------------------------- paths

def glob_regex(pattern):
    """Glob to regex with ** (any depth), * (one segment) and ? support."""
    pat = str(pattern).replace("\\", "/").lstrip("/")
    i, out = 0, ""
    while i < len(pat):
        if pat.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pat.startswith("**", i):
            out += ".*"
            i += 2
        elif pat[i] == "*":
            out += "[^/]*"
            i += 1
        elif pat[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pat[i])
            i += 1
    return re.compile("^" + out + "$")


def norm_path(p, root=None):
    p = str(p).strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if root is not None:
        rootp = Path(root)
        try:
            if Path(p).is_absolute():
                p = str(Path(p).resolve().relative_to(rootp.resolve())).replace("\\", "/")
        except Exception:
            pass
        if p[:2] in ("a/", "b/") and not (rootp / p).exists() and (rootp / p[2:]).exists():
            p = p[2:]
    return p


# ---------------------------------------------------------------- route discovery

def frontmatter(path):
    """Minimal YAML frontmatter reader: top-level `key: value` and folded blocks."""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:  # -sig: a BOM must not hide the "---"
            text = f.read(20000)
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta, block = {}, None
    for line in text[3:end].splitlines():
        if not line.strip():
            continue
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m and not line[0].isspace():
            key, val = m.group(1), m.group(2).strip()
            if val in (">", "|", ">-", "|-", ">+", "|+"):
                meta[key], block = "", key
                continue
            block = None
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            meta[key] = val
        elif block and line[0].isspace():
            meta[block] = (meta[block] + " " + line.strip()).strip()
    return meta


_ROUTES = {}  # long-running processes re-read agent and skill files at most every ROUTES_TTL_S
ROUTES_TTL_S = 2.0


def _route_dirs(root):
    return [base / sub for base in (Path(root) / ".claude", home_claude())
            for sub in ("agents", "skills", "skills/synced")]


def _dir_stamp(path):
    try:
        st = os.stat(str(path))
        return (st.st_mtime_ns, st.st_ino)
    except OSError:
        return None


def discover_routes(root, policy, include_builtins=None):
    """Agents and skills a request could be routed to, project scope first.

    Adding or removing an agent or skill is seen at once (the folder changes); an edit to an existing
    file is seen within ROUTES_TTL_S in a long-running process such as the HTTP API.
    """
    cfg = policy.get("routes") or {}
    key = (str(root), json.dumps(cfg, sort_keys=True, default=str), include_builtins)
    stamp = tuple(_dir_stamp(d) for d in _route_dirs(root))
    now = time.monotonic()
    hit = _ROUTES.get(key)
    if hit and hit[1] == stamp and now - hit[0] < ROUTES_TTL_S:
        return [dict(r) for r in hit[2]]
    routes = _discover_routes(root, policy, include_builtins)
    _ROUTES[key] = (now, stamp, [dict(r) for r in routes])
    return routes


def _discover_routes(root, policy, include_builtins=None):
    cfg = policy.get("routes") or {}
    exclude = set(cfg.get("exclude") or []) | {SKILL_NAME, LEGACY_AGENT}
    routes, seen = [], set()

    def add(name, kind, desc):
        if not name or name in seen or name in exclude:
            return
        seen.add(name)
        desc = re.sub(r"\s+", " ", desc or "").strip()
        if len(desc) > 160:
            desc = desc[:157].rstrip() + "..."
        routes.append({"name": name, "kind": kind, "description": desc})

    for extra in cfg.get("extra") or []:
        if isinstance(extra, dict):
            add(extra.get("name"), extra.get("kind", "custom"), extra.get("description", ""))

    for base in (Path(root) / ".claude", home_claude()):
        adir = base / "agents"
        if adir.is_dir():
            for f in sorted(adir.rglob("*.md")):
                meta = frontmatter(f)
                if meta.get("name"):  # Claude Code skips agent files without a name
                    add(meta["name"], "agent", meta.get("description", ""))
        sdir = base / "skills"
        if sdir.is_dir():
            files = sorted(sdir.glob("*/SKILL.md")) + sorted(sdir.glob("synced/*/SKILL.md"))
            for f in files:
                meta = frontmatter(f)
                add(meta.get("name") or f.parent.name, "skill", meta.get("description", ""))

    builtins = cfg.get("builtins", True) if include_builtins is None else include_builtins
    if builtins:
        for name, kind, desc in BUILTIN_ROUTES:
            add(name, kind, desc)
    return routes[:MAX_ROUTES]


# ---------------------------------------------------------------- probability math

def normalize(probs):
    clean = {}
    for k, v in (probs or {}).items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(f) or f < 0:
            f = 0.0
        clean[str(k)] = clean.get(str(k), 0.0) + f
    total = sum(clean.values())
    if total <= 0:
        raise ValueError("probabilities sum to zero")
    return {k: v / total for k, v in clean.items()}


def temper(probs, t):
    """Temperature scaling for a categorical distribution (t > 1 softens, t < 1 sharpens)."""
    if abs(t - 1.0) < 1e-9:
        return dict(probs)
    w = {k: (v ** (1.0 / t) if v > 0 else 0.0) for k, v in probs.items()}
    s = sum(w.values()) or 1.0
    return {k: v / s for k, v in w.items()}


def temper_p(p, t):
    """Temperature scaling for a single probability, on the logit scale."""
    p = min(max(float(p), 1e-6), 1 - 1e-6)
    if abs(t - 1.0) < 1e-9:
        return p
    z = math.log(p / (1 - p)) / t
    return 1.0 / (1.0 + math.exp(-z))


def softmax(scores):
    m = max(scores)
    ex = [math.exp(s - m) for s in scores]
    z = sum(ex) or 1.0
    return [e / z for e in ex]
