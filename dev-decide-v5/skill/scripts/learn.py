"""dev-decide learns from use: every labeled decision makes the local tier better, and a new model only
takes over after it beats the current one on your own inputs.

Where labels come from (strongest first). The local model's own answers are never used as labels.
  fix    your corrections and confirmations: /dev-decide fix last issue_type=bug           weight 3
  audit  spot checks: about 1 in 20 local answers is re-asked to Claude (capped per day)  weight 1
  llm    Claude's answers for the inputs the local tier passed on                         weight = its confidence

The loop, per decision type:
  test     before learning from a label, the current model (champion) and its candidate (challenger)
           both predict it, so their running scores are honest predictions on your real inputs
  train    challenger = champion + a learned correction. One AdaGrad step per label, with an L2 pull
           toward the champion, so a handful of labels cannot drag it far
  promote  when the challenger wins on your inputs with enough evidence, and reaches the accuracy target
           at a usable confidence threshold, it becomes the champion; the old one is archived (rollback)
  demote   when spot checks show the champion's local answers below target, they stop until a better
           model proves itself
  memory   a correction also answers near-identical inputs at once, and is shown to Claude as a
           precedent for similar inputs

Nothing runs in the background and no code changes: learning happens inside normal dev-decide calls, in
small time slices, and only touches model files, the model index and the files under learn/.
Everything lives in the repository's gitignored .claude/dev-decide folder.
"""
import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
import threading
import time
from array import array
from pathlib import Path

import ddcore as dd
import fastmodel as fm

try:
    import fcntl  # POSIX advisory locks; elsewhere a lock file is used
except ImportError:  # pragma: no cover
    fcntl = None

DEFAULTS = {
    "enabled": True,           # false: dev-decide behaves exactly like 3.1
    "target": 0.9,             # accuracy that local answers must reach on your inputs
    "min_confidence": 0.6,     # never answer locally below this confidence
    "auto_promote": True,      # false: a better model waits for `dev-decide promote <preset>`
    "audit_rate": 0.05,        # share of local answers spot-checked by Claude (only when model calls are allowed)
    "audits_per_day": 20,      # hard daily cap on spot checks
    "audit_batch": 4,          # spot checks sent along with one model call
    "audit_ttl_days": 7,       # unchecked inputs, and their text, are deleted after this
    "keep_text": True,         # text kept until used: spot-check queue, recent decisions (for fix), corrections
    "recent": 20,              # recent decisions kept so `fix` can learn from them
    "recent_hours": 24,
    "memory": True,            # corrections answer near-identical inputs at once
    "memory_similarity": 0.80,
    "memory_max": 300,         # per decision type
    "window": 400,             # most recent labels used to compare champion and challenger
    "min_window": 40,
    "check_every": 10,         # labels between promotion checks
    "hold_after_rollback": 50,  # labels before a rolled-back decision type may switch again
    "lr": 1.0, "g0": 1.0, "l2": 0.001,  # AdaGrad step, initial accumulator, pull toward the champion
    "episodic": True,          # answer inputs like ones already labeled, once that has proven accurate
    "adapt": True,             # thresholds rise and fall with spot-check results
    "adapt_rate": 0.05,
    "adapt_max": 0.45,
    "novelty_floor": 0.55,     # unfamiliar inputs (less of their features known to the model) go to the model
    "targeted_checks": True,   # spot checks favour borderline and unfamiliar answers, reweighted to stay unbiased
}
SOURCE_WEIGHT = {"fix": 3.0, "audit": 1.0, "llm": 1.0}
STRONG_WEIGHT = 1.5           # labels from the stronger model (escalations) count more
MODEL_VIA = ("llm", "llm-deep", "llm-strong")
LOCAL_VIA = ("learned", "memory", "episodic")
EPISODE_K = 32                # features kept per remembered decision
EPISODE_MAX = 600             # remembered decisions per decision type
EPISODE_FLOOR = 0.5           # neighbours less similar than this do not vote
EPISODE_KNN = 5
EPISODE_WINDOW = 150          # recall is judged on its most recent labels, so it notices change quickly
ALARM_BOOST = 4.0             # spot checks for a decision type run this much more often after recent errors
EPISODIC_CHECKS = 3.0         # recalled answers are spot-checked three times as often (stale labels are their risk)
ADAPT_DECAY = 0.995           # per label: per-class threshold offsets fade unless errors keep them up
MAX_QUEUE = 40
MAX_LABEL_BYTES = 40 << 20
KEEP_VERSIONS = 5
VIEW_CHARS = 6000
RECENT_TEXT = 3000

_THREAD_LOCK = threading.Lock()
_READY = set()  # learn folders this process has already created


# ---------------------------------------------------------------- configuration and files

def config(policy):
    cfg = dict(DEFAULTS)
    user = (policy or {}).get("learning")
    if user is False:
        cfg["enabled"] = False
    elif isinstance(user, dict):
        for k, v in user.items():
            if k in DEFAULTS:
                cfg[k] = v
    if str(os.environ.get("DEV_DECIDE_LEARNING", "")).lower() in ("0", "off", "false", "no"):
        cfg["enabled"] = False
    return cfg


class Paths(object):
    def __init__(self, root):
        self.root = Path(root)
        self.base = self.root / ".claude" / "dev-decide"
        self.dir = self.base / "learn"
        self.models = self.base / "models"
        self.versions = self.models / "versions"
        self.labels = self.dir / "labels.jsonl"
        self.queue = self.dir / "audit_queue.jsonl"
        self.memory = self.dir / "memory.jsonl"
        self.recent = self.dir / "recent.jsonl"
        self.history = self.dir / "history.jsonl"
        self.state = self.dir / "state.json"
        self.auto = self.dir / "auto_presets.json"
        self.lock = self.dir / ".lock"
        self.challengers = self.dir / "challengers"

    def ensure(self):
        dd.state_dir(self.root)  # creates the folder and its "*" .gitignore
        self.dir.mkdir(parents=True, exist_ok=True)
        return self

    def challenger(self, preset):
        return self.challengers / _safe(preset)

    def episodes(self, preset):
        return self.dir / "episodes" / _safe(preset)


def _safe(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))[:80]


def _write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.%d.%d.tmp" % (path.name, os.getpid(), threading.get_ident()))
    with open(str(tmp), "wb") as fh:
        fh.write(data)
    os.replace(str(tmp), str(path))


def _write_text(path, text):
    _write_bytes(path, text.encode("utf-8"))


def _write_json(path, obj, indent=None):
    _write_text(path, json.dumps(obj, indent=indent, separators=None if indent else (",", ":")) + "\n")


def _append(path, rows):
    if not rows:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))


def _read_lines(path):
    return dd.read_jsonl(path)


class Lock(object):
    """Exclusive lock across threads and processes. Gives up after `wait` seconds: learning never blocks a decision."""

    def __init__(self, path, wait=0.25):
        self.path, self.wait, self.fh, self.ok, self.thread = Path(path), wait, None, False, False

    def __enter__(self):
        deadline = time.monotonic() + self.wait
        self.thread = _THREAD_LOCK.acquire(timeout=max(self.wait, 0.001))
        if not self.thread:
            return self
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if fcntl is not None:
                self.fh = open(str(self.path), "a+")
                while True:
                    try:
                        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        self.ok = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.01)
            else:  # pragma: no cover - lock file fallback
                marker = str(self.path) + ".held"
                while True:
                    try:
                        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                        os.close(fd)
                        self.fh = marker
                        self.ok = True
                        break
                    except OSError:
                        try:
                            if time.time() - os.path.getmtime(marker) > 30:
                                os.unlink(marker)
                                continue
                        except OSError:
                            pass
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.01)
        except OSError:
            self.ok = False
        return self

    def __exit__(self, *exc):
        try:
            if self.fh is not None:
                if fcntl is not None:
                    try:
                        fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                    finally:
                        self.fh.close()
                else:  # pragma: no cover
                    try:
                        os.unlink(self.fh)
                    except OSError:
                        pass
        finally:
            if self.thread:
                _THREAD_LOCK.release()
        return False


# ---------------------------------------------------------------- small math

def enc(vec):
    return [[int(b), round(float(v), 4)] for b, v in sorted((vec or {}).items())]


def dec(pairs):
    return {int(b): float(v) for b, v in (pairs or [])}


def softmax(xs):
    m = max(xs)
    e = [math.exp(v - m) for v in xs]
    z = sum(e) or 1.0
    return [v / z for v in e]


def wilson(k, n, z=1.96):
    if n <= 0:
        return 0.0, 1.0
    p = float(k) / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, mid - half), min(1.0, mid + half)


def binom_tail(k, n):
    """P(X >= k) for X ~ Binomial(n, 1/2): the one-sided exact McNemar test."""
    if n <= 0 or k <= 0:
        return 1.0
    return sum(math.comb(n, i) for i in range(k, n + 1)) / float(2 ** n)


def operating_point(pairs, target, min_conf, min_n=15, lb_margin=0.10):
    """Lowest confidence threshold whose accepted answers reach the target, with evidence.

    pairs: (confidence, correct). Returns {threshold, accepted, accuracy, coverage} or None.
    """
    n = len(pairs)
    srt = sorted(pairs, key=lambda t: -t[0])
    best, ok, i = None, 0, 0
    while i < n:
        j = i
        while j < n and srt[j][0] == srt[i][0]:
            ok += 1 if srt[j][1] else 0
            j += 1
        conf = srt[i][0]
        if conf < min_conf:
            break
        if j >= min_n and ok / float(j) >= target and wilson(ok, j)[0] >= target - lb_margin:
            best = {"threshold": round(conf, 4), "accepted": j, "accuracy": round(ok / float(j), 4),
                    "coverage": round(j / float(n), 4)}
        i = j
    return best


# ---------------------------------------------------------------- what the teacher said

def distribution(kind, raw):
    """(label, probabilities) from a raw model answer, before any temperature: the teacher's view."""
    if not isinstance(raw, dict):
        return None, None
    if kind == "verify":
        try:
            p = min(max(float(raw.get("p_true")), 0.0), 1.0)
        except (TypeError, ValueError):
            return None, None
        return ("true" if p >= 0.5 else "false"), {"true": round(p, 4), "false": round(1 - p, 4)}
    probs = raw.get("probs")
    if not probs:
        if kind == "score" and raw.get("score") is not None:
            try:
                probs = {str(int(round(float(raw["score"])))): 1.0}
            except (TypeError, ValueError):
                return None, None
        elif raw.get("choice") is not None:
            probs = {str(raw["choice"]): 1.0}
        else:
            return None, None
    clean = {}
    for k, v in probs.items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0 and not math.isnan(f):
            key = str(k)
            if kind == "score" and key != "_rest":
                try:
                    key = str(int(round(float(key))))
                except ValueError:
                    continue
            clean[key] = clean.get(key, 0.0) + f
    total = sum(clean.values())
    named = {k: v for k, v in clean.items() if k != "_rest"}
    if total <= 0 or not named:
        return None, None
    y = max(named, key=named.get)
    return y, {k: round(v / total, 4) for k, v in clean.items()}


def align(probs, classes):
    """The teacher's probabilities as a target vector over the classes; unassigned mass is spread."""
    idx = {c: i for i, c in enumerate(classes)}
    q = [0.0] * len(classes)
    rest = 0.0
    for k, v in (probs or {}).items():
        if k in idx:
            q[idx[k]] += v
        else:
            rest += v
    if rest > 0:
        empty = [i for i, v in enumerate(q) if v == 0.0] or list(range(len(q)))
        for i in empty:
            q[i] += rest / len(empty)
    s = sum(q)
    return [v / s for v in q] if s > 0 else None


def answer_value(final):
    """The answer a user saw, as a class label string."""
    if not isinstance(final, dict):
        return None
    if "choice" in final:
        return str(final["choice"])
    if "level" in final:
        return str(final["level"])
    if "verdict" in final:
        return "true" if final["verdict"] else "false"
    return None


# ---------------------------------------------------------------- custom questions that recur

def _norm(text):
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def auto_key(q):
    """A stable name for a custom (ad-hoc) question, so repeated ones can get their own local model."""
    s = q.spec or {}
    if q.kind == "choose":
        canon = ["choose", sorted(str(o) for o in (s.get("options") or [])), bool(s.get("allow_none"))]
    elif q.kind == "score":
        canon = ["score", int(s.get("min", 1)), int(s.get("max", 5)), _norm(s.get("label"))]
    else:
        canon = ["verify", _norm(s.get("statement"))]
    return "auto_" + hashlib.sha1(json.dumps(canon).encode("utf-8")).hexdigest()[:10]


def auto_spec(q):
    s = q.spec or {}
    if q.kind == "choose":
        spec = {"options": sorted(str(o) for o in (s.get("options") or [])), "allow_none": bool(s.get("allow_none"))}
        title = "choose " + "|".join(spec["options"] + (["none"] if spec["allow_none"] else []))
    elif q.kind == "score":
        spec = {"min": int(s.get("min", 1)), "max": int(s.get("max", 5)), "label": str(s.get("label") or "")}
        title = "score %d-%d %s" % (spec["min"], spec["max"], spec["label"])
    else:
        spec = {"statement": str(s.get("statement") or "")}
        title = "verify " + spec["statement"][:80]
    return {"kind": q.kind, "spec": spec, "title": title.strip()}


def adhoc_fields(q):
    """Fields a learned answer to a custom question needs so the finalizer can read it."""
    out = {"type": q.kind}
    for f in ("options", "allow_none", "min", "max", "statement"):
        if f in q.spec:
            out[f] = q.spec[f]
    return out


def unknown_option_hint(reg, name, hint, floor=0.35):
    """True when the local hint points at an option the trained model has never seen (a newly added agent or
    skill, say). The learned answer is then not trusted, so the request goes to the model and teaches it."""
    if hint is None or getattr(hint, "kind", None) != "choose" or not getattr(hint, "probs", None):
        return False
    model = reg.load(name)
    if not model:
        return False
    named = {k: v for k, v in hint.probs.items() if k not in ("none", "_rest")}
    if not named:
        return False
    top = max(named, key=named.get)
    return top not in model["classes"] and named[top] >= floor


def display(name, autos):
    """Readable name: custom questions show as the question itself (choose dispute|other|refund)."""
    rec = (autos or {}).get(name)
    return '"%s"' % rec["title"] if rec and rec.get("title") else name


def preset_spec(cat, autos, name):
    kind, spec = dd.find_preset(cat, name)
    if kind is not None:
        return kind, spec
    a = (autos or {}).get(name)
    if a:
        return a["kind"], dict(a["spec"])
    return None, None


# ---------------------------------------------------------------- the challenger

class Challenger(object):
    """The champion plus a learned correction (delta), trained online with AdaGrad.

    scores(x) = champion_scores(x) + bias_delta + sum_f x_f * delta_f, and p = softmax(scores / T).
    Classes the champion does not know (a new agent, say) start from `new_bias` and are learned from labels.
    """
    MAGIC = b"DDC1"

    def __init__(self, preset, kind, classes, fv="f1", temperature=1.0, base="none", new_bias=0.0, growable=False):
        self.preset, self.kind, self.classes = preset, kind, list(classes)
        self.fv, self.T, self.base, self.new_bias = fv, float(temperature or 1.0), base, float(new_bias)
        self.growable = bool(growable)
        self.delta, self.G = {}, {}
        self.db = [0.0] * len(self.classes)
        self.Gb = 0.0
        self.updates = 0
        self.created = int(time.time())
        self._cmap = None

    def add_class(self, c):
        self.classes.append(c)
        for d in self.delta.values():
            d.append(0.0)
        self.db.append(0.0)
        self._cmap = None

    def base_scores(self, model, s0):
        if model is None:
            return [0.0] * len(self.classes)
        if self._cmap is None or len(self._cmap) != len(self.classes):
            idx = {c: i for i, c in enumerate(model["classes"])}
            self._cmap = [idx.get(c) for c in self.classes]
        return [s0[j] if j is not None else self.new_bias for j in self._cmap]

    def scores(self, base, x):
        K = len(self.classes)
        s = [base[k] + self.db[k] for k in range(K)]
        delta = self.delta
        for f, v in x.items():
            d = delta.get(f)
            if d is not None:
                for k in range(K):
                    s[k] += d[k] * v
        return s

    def probs(self, s):
        return softmax([v / self.T for v in s])

    def update(self, x, p, q, w, lr, g0, l2):
        K = len(self.classes)
        g = [w * (p[k] - q[k]) / self.T for k in range(K)]
        gb = [g[k] + l2 * self.db[k] for k in range(K)]
        self.Gb += sum(v * v for v in gb)
        step = lr / math.sqrt(g0 + self.Gb)
        for k in range(K):
            self.db[k] -= step * gb[k]
        delta, G = self.delta, self.G
        for f, v in x.items():
            d = delta.get(f)
            if d is None:
                d = delta[f] = [0.0] * K
            grad = [g[k] * v + l2 * d[k] for k in range(K)]
            acc = G.get(f, g0) + sum(gr * gr for gr in grad)
            G[f] = acc
            st = lr / math.sqrt(acc)
            for k in range(K):
                d[k] -= st * grad[k]
        self.updates += 1

    # -- persistence: a small JSON header and a binary body (bucket ids, float32 deltas, float32 accumulators)
    def save(self, prefix):
        K = len(self.classes)
        buckets = sorted(self.delta)
        b = array("I", buckets)
        w = array("f", [v for f in buckets for v in self.delta[f]])
        g = array("f", [self.G.get(f, 0.0) for f in buckets])
        head = array("I", [len(buckets), K])
        if sys.byteorder != "little":
            for arr in (b, w, g, head):
                arr.byteswap()
        _write_bytes(Path(str(prefix) + ".bin"), self.MAGIC + head.tobytes() + b.tobytes() + w.tobytes() + g.tobytes())
        meta = {"v": 1, "preset": self.preset, "kind": self.kind, "classes": self.classes, "fv": self.fv,
                "T": self.T, "base": self.base, "new_bias": self.new_bias, "growable": self.growable,
                "db": [round(v, 6) for v in self.db], "Gb": self.Gb, "updates": self.updates, "created": self.created}
        _write_json(Path(str(prefix) + ".json"), meta)

    @classmethod
    def load(cls, prefix):
        meta = dd.read_json(Path(str(prefix) + ".json"), default=None)
        if not meta or not isinstance(meta, dict) or meta.get("v") != 1:
            return None
        try:
            data = Path(str(prefix) + ".bin").read_bytes()
        except OSError:
            return None
        if data[:4] != cls.MAGIC:
            return None
        head = array("I")
        head.frombytes(data[4:12])
        if sys.byteorder != "little":
            head.byteswap()
        n, K = head[0], head[1]
        if K != len(meta["classes"]) or len(data) != 12 + 4 * n + 4 * n * K + 4 * n:
            return None
        b, w, g = array("I"), array("f"), array("f")
        o = 12
        b.frombytes(data[o:o + 4 * n])
        o += 4 * n
        w.frombytes(data[o:o + 4 * n * K])
        o += 4 * n * K
        g.frombytes(data[o:o + 4 * n])
        if sys.byteorder != "little":
            for arr in (b, w, g):
                arr.byteswap()
        ch = cls(meta["preset"], meta["kind"], meta["classes"], meta.get("fv", "f1"), meta.get("T", 1.0),
                 meta.get("base", "none"), meta.get("new_bias", 0.0), meta.get("growable", False))
        ch.db = [float(v) for v in meta.get("db") or [0.0] * K]
        ch.Gb = float(meta.get("Gb", 0.0))
        ch.updates = int(meta.get("updates", 0))
        ch.created = int(meta.get("created", time.time()))
        wl = w.tolist()
        for i, f in enumerate(b):
            ch.delta[f] = wl[i * K:(i + 1) * K]
            ch.G[f] = g[i]
        return ch


def champion_id(meta, model):
    if model is None:
        return "none"
    if meta.get("version_id"):
        return str(meta["version_id"])
    return "%s@%s" % (meta.get("source") or "model", meta.get("trained") or meta.get("version") or "0")


def fresh_challenger(preset, kind, spec, model, meta, routes):
    growable = kind == "choose" and (spec or {}).get("options") == "auto"
    if model is not None:
        T = float(model.get("temperature") or meta.get("temperature") or 1.0)
        fv = model.get("featurizer") or meta.get("featurizer") or fm.FEATURIZER
        bias = model.get("bias") or [0.0]
        ch = Challenger(preset, kind, list(model["classes"]), fv, T, champion_id(meta, model),
                        min(bias) - 2.0 * T, growable)
    else:
        if kind == "choose" and (spec or {}).get("options") == "auto":
            classes = sorted(r["name"] for r in (routes or []))
            if spec.get("allow_none", True):
                classes.append("none")
        else:
            classes = fm.classes_for(kind, spec or {})
        if not classes:
            return None
        ch = Challenger(preset, kind, classes, fm.FEATURIZER, 1.0, "none", 0.0, growable)
    return ch


def materialize(model, ch):
    """Champion weights plus the challenger's correction, as a new champion (bucket ids, flat weights, bias)."""
    K = len(ch.classes)
    rows = {}
    if model is not None:
        mc = list(model["classes"])
        K0 = len(mc)
        cmap = [mc.index(c) if c in mc else None for c in ch.classes]
        if model.get("_idx") is not None:
            w = model["_w"]
            for bkt, r in model["_idx"].items():
                base = r * K0
                rows[bkt] = [float(w[base + j]) if j is not None else 0.0 for j in cmap]
        else:
            for bkt, ws in (model.get("weights") or {}).items():
                rows[int(bkt)] = [float(ws[j]) if j is not None else 0.0 for j in cmap]
        bias = [float(model["bias"][j]) if j is not None else ch.new_bias for j in cmap]
    else:
        bias = [0.0] * K
    for bkt, d in ch.delta.items():
        r = rows.get(bkt)
        rows[bkt] = [d[k] + (r[k] if r else 0.0) for k in range(K)]
    bias = [bias[k] + ch.db[k] for k in range(K)]
    keep = sorted(bkt for bkt, r in rows.items() if max(abs(v) for v in r) >= 1e-4)
    flat = [v for bkt in keep for v in rows[bkt]]
    return keep, flat, bias


# ---------------------------------------------------------------- state, versions and the model index

def load_state(p):
    s = dd.read_json(p.state, default={})
    if not isinstance(s, dict):
        s = {}
    s.setdefault("v", 1)
    s.setdefault("cursor", 0)
    s.setdefault("presets", {})
    s.setdefault("audit", {"day": "", "sent": 0})
    return s


def preset_state(state, preset):
    ps = state["presets"].setdefault(preset, {})
    ps.setdefault("window", [])
    ps.setdefault("audits", [])
    ps.setdefault("labels", {"fix": 0, "audit": 0, "llm": 0})
    ps.setdefault("since_check", 0)
    ps.setdefault("hold", 0)
    ps.setdefault("version", 0)
    return ps


def _event(p, ev):
    ev = dict(ev, t=int(time.time()))
    _append(p.history, [ev])
    return ev


def _index(p):
    return dd.read_json(p.models / "index.json", default={}) or {}


def _save_index(p, index):
    p.models.mkdir(parents=True, exist_ok=True)
    _write_json(p.models / "index.json", index, indent=2)


def archive(p, preset, meta):
    """Copy the current champion files into models/versions/<preset>/<n>/ so a switch can be undone."""
    src_json, src_bin = p.models / ("%s.json" % preset), p.models / ("%s.bin" % preset)
    if not src_json.exists():
        return None
    d = p.versions / _safe(preset)
    d.mkdir(parents=True, exist_ok=True)
    nums = [int(x.name) for x in d.iterdir() if x.is_dir() and x.name.isdigit()]
    slot = d / str(max(nums or [0]) + 1)
    slot.mkdir()
    shutil.copy2(str(src_json), str(slot / "model.json"))
    if src_bin.exists():
        shutil.copy2(str(src_bin), str(slot / "model.bin"))
    _write_json(slot / "meta.json", dict(meta or {}, archived=int(time.time())), indent=2)
    for old in sorted((x for x in d.iterdir() if x.is_dir() and x.name.isdigit()), key=lambda x: int(x.name))[:-KEEP_VERSIONS]:
        shutil.rmtree(str(old), ignore_errors=True)
    return slot


def versions(p, preset):
    d = p.versions / _safe(preset)
    if not d.is_dir():
        return []
    out = []
    for x in sorted((x for x in d.iterdir() if x.is_dir() and x.name.isdigit()), key=lambda x: int(x.name)):
        out.append((int(x.name), dd.read_json(x / "meta.json", default={}), x))
    return out


def promote(p, preset, ch, model, meta, verdict, ps, why):
    """Make the challenger the champion. The previous champion is archived first."""
    archive(p, preset, meta)
    keep, flat, bias = materialize(model, ch)
    K = len(ch.classes)
    ps["version"] = int(ps.get("version", 0)) + 1
    version = ps["version"]
    op = verdict["op"]
    tmp_bin = p.models / ("%s.bin.new" % preset)
    p.models.mkdir(parents=True, exist_ok=True)
    fm.write_binary(str(tmp_bin), keep, flat, K)
    os.replace(str(tmp_bin), str(p.models / ("%s.bin" % preset)))
    body = {"format": "bin1", "kind": ch.kind, "classes": ch.classes, "bias": [round(v, 6) for v in bias],
            "temperature": ch.T, "featurizer": ch.fv, "threshold": op["threshold"], "version": version}
    _write_json(p.models / ("%s.json" % preset), body)
    index = _index(p)
    index[preset] = {"enabled": True, "featurizer": ch.fv, "kind": ch.kind, "classes": ch.classes,
                     "threshold": op["threshold"], "adapt": {}, "temperature": ch.T, "source": "dev-decide-learn",
                     "version": version, "version_id": "learn:%d:%d" % (version, int(time.time())),
                     "trained": time.strftime("%Y-%m-%d %H:%M"),
                     "live": {"labels": verdict["n"], "accepted_accuracy": op["accuracy"], "coverage": op["coverage"],
                              "accepted": op["accepted"]},
                     "reason": why, "test_source": "your decisions (test-then-train)"}
    _save_index(p, index)
    return version


def rollback(p, preset, which=None):
    vs = versions(p, preset)
    if not vs:
        return None, "no earlier version of %s is archived" % preset
    pick = vs[-1]
    if which is not None:
        found = [v for v in vs if v[0] == int(which)]
        if not found:
            return None, "version %s of %s is not archived (have: %s)" % (which, preset, ", ".join(str(v[0]) for v in vs))
        pick = found[0]
    n, meta, slot = pick
    # Read the chosen version first: archiving the current model below prunes the oldest slot, which may be this one.
    body = (slot / "model.json").read_bytes()
    binary = (slot / "model.bin").read_bytes() if (slot / "model.bin").exists() else None
    index = _index(p)
    cur = index.get(preset) or {}
    archive(p, preset, cur)  # the undone model stays available too
    _write_bytes(p.models / ("%s.json" % preset), body)
    if binary is not None:
        _write_bytes(p.models / ("%s.bin" % preset), binary)
    restored = {k: v for k, v in meta.items() if k != "archived"}
    restored["restored"] = time.strftime("%Y-%m-%d %H:%M")
    index[preset] = restored
    _save_index(p, index)
    return n, None


# ---------------------------------------------------------------- learning from labels

class _Open(object):
    __slots__ = ("ch", "model", "meta", "cid", "dirty", "ps", "kind", "spec", "ep", "ep_dirty", "adapt", "adapt_dirty",
                 "breakers")


def _open_preset(p, reg, state, preset, cat, autos, routes, events):
    kind, spec = preset_spec(cat, autos, preset)
    if kind is None:
        return None
    meta = reg.index.get(preset) or {}
    model = reg.load(preset)
    cid = champion_id(meta, model)
    ps = preset_state(state, preset)
    ch = Challenger.load(p.challenger(preset))
    if ch is None or ch.base != cid:
        if ps.get("base") not in (None, cid):
            events.append(_event(p, {"preset": preset, "event": "reset",
                                     "why": "a different model was installed, so learning restarted from it"}))
        ch = fresh_challenger(preset, kind, spec, model, meta, routes)
        if ch is None:
            return None
        ps["window"], ps["audits"], ps["since_check"], ps["base"] = [], [], 0, cid
    o = _Open()
    o.ch, o.model, o.meta, o.cid, o.dirty, o.ps, o.kind, o.spec = ch, model, meta, cid, False, ps, kind, spec
    o.ep = Episodes.load(p.episodes(preset)) if config_episodic(p) else None
    o.ep_dirty = False
    o.adapt, o.adapt_dirty = meta.get("adapt") or {}, False
    o.breakers = []
    return o


def config_episodic(p):
    return True


def _learn_one(o, lab, cfg):
    ch, model = o.ch, o.model
    xs = lab.get("x") or {}
    y = str(lab.get("y"))
    if not y or y == "None":
        return False
    t = int(lab.get("t") or 0)
    if o.ep is not None and cfg.get("episodic", True):  # fast memory: test, then remember
        x1 = dec(xs.get("f1"))
        if x1:
            pred = o.ep.predict(x1)
            if pred:
                win = o.ps.setdefault("ep_window", [])
                win.append([t, y, pred[0], pred[1]])
                if len(win) > int(cfg["window"]):
                    del win[:len(win) - int(cfg["window"])]
                # shadow test: would recall have answered this one locally, and was it right?
                if lab.get("src") != "audit" and o.ep.threshold is not None and \
                        pred[1] >= float(o.ep.threshold) + adapt_for(o.ep.adapt, pred[0]):
                    if _breaker(o.ps, "episodic", pred[0], pred[0] == y):
                        _trip(o, cfg, pred[0])
            o.ep.add(x1, y, t)
            o.ep_dirty = True
    x = dec(xs.get(ch.fv))
    if not x:
        return False
    if y not in ch.classes:
        if ch.growable and y != "_rest":
            ch.add_class(y)
        else:
            return False
    q = align(lab.get("q") or {y: 1.0}, ch.classes)
    if q is None:
        return False
    if model is not None:
        s0 = fm.predict_scores(model, x)
        T0 = float(model.get("temperature") or 1.0)
        p0 = softmax([v / T0 for v in s0])
        j = max(range(len(p0)), key=p0.__getitem__)
        c0, conf0 = str(model["classes"][j]), p0[j]
        # shadow test: would the current model have answered this one locally, and was it right?
        if lab.get("src") != "audit" and o.meta.get("enabled") and \
                conf0 >= float(o.meta.get("threshold", 1.01)) + adapt_for(o.adapt, c0):
            if _breaker(o.ps, "learned", c0, c0 == y):
                _trip(o, cfg, c0)
    else:
        s0, c0, conf0 = None, None, 0.0
    base = ch.base_scores(model, s0)
    pr = ch.probs(ch.scores(base, x))
    k = max(range(len(pr)), key=pr.__getitem__)
    c1, conf1 = ch.classes[k], pr[k]
    src = lab.get("src") if lab.get("src") in SOURCE_WEIGHT else "llm"
    ps = o.ps
    ps["window"].append([int(lab.get("t") or 0), src, y, c0, round(conf0, 4), c1, round(conf1, 4)])
    if len(ps["window"]) > cfg["window"]:
        del ps["window"][:len(ps["window"]) - cfg["window"]]
    if src == "audit":
        loc = lab.get("local") or {}
        wis = float(lab.get("wis") or 1.0)  # importance weight of a targeted spot check
        ok = str(loc.get("answer")) == y
        ps["audits"].append([int(lab.get("t") or 0), loc.get("via"), ok, loc.get("conf"), loc.get("cid"), round(wis, 4)])
        if len(ps["audits"]) > 200:
            del ps["audits"][:len(ps["audits"]) - 200]
        if cfg.get("adapt", True):  # thresholds follow spot-check errors toward the target error rate
            step_ = float(cfg["adapt_rate"]) * wis * ((0.0 if ok else 1.0) - (1.0 - float(cfg["target"])))
            said = str(loc.get("answer"))
            tier = loc.get("via")
            if tier in ("learned", "episodic") and _breaker(ps, tier, said, ok):
                _trip(o, cfg, said)  # two confirmed errors in one class: that class has changed
            elif tier == "learned" and loc.get("cid") == o.cid and o.meta:
                o.adapt = _bump(cfg, o.adapt, said, step_)
                o.adapt_dirty = True
            elif tier == "episodic" and o.ep is not None:
                o.ep.adapt = _bump(cfg, o.ep.adapt, said, step_)
                o.ep_dirty = True
    conf = lab.get("conf")
    try:
        conf = min(max(float(conf), 0.3), 1.0)
    except (TypeError, ValueError):
        conf = 1.0
    w = SOURCE_WEIGHT[src] * (1.0 if src == "fix" else conf)
    if lab.get("strong"):
        w *= STRONG_WEIGHT
    if src == "audit" and str((lab.get("local") or {}).get("answer")) != y:
        w *= 2.0  # a spot check that caught a wrong local answer is the most useful label there is
    ch.update(x, pr, q, w, float(cfg["lr"]), float(cfg["g0"]), float(cfg["l2"]))
    ps["labels"][src] = ps["labels"].get(src, 0) + 1
    ps["since_check"] = ps.get("since_check", 0) + 1
    if ps.get("hold", 0) > 0:
        ps["hold"] -= 1
    o.dirty = True
    if o.adapt:  # offsets fade (half-life about 140 labels), so a false alarm does not silence a class for good
        o.adapt = {c: round(v * ADAPT_DECAY, 4) for c, v in o.adapt.items() if v * ADAPT_DECAY > 1e-3}
        o.adapt_dirty = True
    if o.ep is not None and o.ep.adapt:
        o.ep.adapt = {c: round(v * ADAPT_DECAY, 4) for c, v in o.ep.adapt.items() if v * ADAPT_DECAY > 1e-3}
    return True


def compare(window, meta, cfg):
    """Champion versus challenger on the same labels, each predicted before it was learned from."""
    W = window[-int(cfg["window"]):]
    n = len(W)
    target = float(cfg["target"])
    ok0 = [e[3] == e[2] for e in W]
    ok1 = [e[5] == e[2] for e in W]
    b = sum(1 for a, c in zip(ok0, ok1) if a and not c)
    c = sum(1 for a, c_ in zip(ok0, ok1) if c_ and not a)
    op = operating_point([(e[6], ok) for e, ok in zip(W, ok1)], target, float(cfg["min_confidence"]))
    thr0 = float(meta.get("threshold", 1.01)) if meta.get("enabled") else None
    u0, acc0 = 0.0, None
    if thr0 is not None:
        accepted = [ok for e, ok in zip(W, ok0) if e[4] >= thr0]
        if accepted:
            acc0 = sum(accepted) / float(len(accepted))
            if acc0 >= target - 0.05:
                u0 = len(accepted) / float(n)
    u1 = op["coverage"] if op else 0.0
    better = c > b and binom_tail(c, b + c) < 0.05
    worse = b > c and binom_tail(b, b + c) < 0.10
    protect = True
    if thr0 is not None:
        region = [(a, z) for e, a, z in zip(W, ok0, ok1) if e[4] >= thr0]
        if region:
            e0 = sum(1 for a, _ in region if not a)
            e1 = sum(1 for _, z in region if not z)
            protect = e1 <= e0 + max(1, int(0.02 * len(region)))
    promote_ok = bool(op) and protect and not worse and (u1 >= u0 + 0.03 or better)
    return {"n": n, "acc_champion": round(sum(ok0) / float(n), 4) if n else 0.0,
            "acc_challenger": round(sum(ok1) / float(n), 4) if n else 0.0, "champion_wins": b,
            "challenger_wins": c, "op": op, "coverage_champion": round(u0, 4), "coverage_challenger": round(u1, 4),
            "protect": protect, "worse": worse, "better": better, "promote": promote_ok,
            "champion_accepted_accuracy": round(acc0, 4) if acc0 is not None else None}


def _why(preset, v, prev_enabled, label=None):
    op = v["op"]
    gain = ("answers %d%% of these inputs locally at %d%% accuracy (was %d%%)"
            % (round(op["coverage"] * 100), round(op["accuracy"] * 100), round(v["coverage_champion"] * 100)))
    if v["better"]:
        gain += "; right where the old model was wrong %d times, the reverse %d times" % (v["challenger_wins"], v["champion_wins"])
    return "%s: %s, over your last %d labeled decisions" % (label or preset, gain, v["n"])


def _checks(p, o, preset, cfg, events, autos=None):
    ps, meta = o.ps, o.meta
    label = display(preset, autos)
    # demotion: spot checks of this champion's local answers fell clearly below target
    if meta.get("enabled"):
        aud = [a for a in ps["audits"] if a[1] == "learned" and a[4] == o.cid]
        n = len(aud)
        k = sum(1 for a in aud if a[2])
        ws = [float(a[5]) if len(a) > 5 and a[5] else 1.0 for a in aud]
        sw, sw2 = sum(ws), sum(w * w for w in ws)
        n_eff = (sw * sw / sw2) if sw2 else 0.0
        acc_w = (sum(w for a, w in zip(aud, ws) if a[2]) / sw) if sw else 0.0
        if n >= 12 and wilson(acc_w * n_eff, n_eff, 1.645)[1] < float(cfg["target"]) - 0.05:
            index = _index(p)
            if (index.get(preset) or {}).get("enabled"):
                index[preset]["enabled"] = False
                index[preset]["reason"] = ("paused by spot checks: %d of %d local answers were right (%d%%), below the "
                                           "%d%% target" % (k, n, round(100.0 * k / n), round(100 * float(cfg["target"]))))
                _save_index(p, index)
                meta["enabled"] = False
                events.append(_event(p, {"preset": preset, "event": "paused", "why": index[preset]["reason"],
                                         "metrics": {"audits": n, "correct": k}}))
    recent = ps["audits"][-10:]  # drift alarm: on at two errors in the last ten checks, off after 20 near-clean ones
    if meta.get("alarm"):
        last = ps["audits"][-20:]
        alarm = not (len(last) >= 20 and sum(1 for a in last if not a[2]) <= 1)
    else:
        alarm = sum(1 for a in recent if not a[2]) >= 2
    if bool(meta.get("alarm")) != alarm and meta:
        index = _index(p)
        if preset in index:
            index[preset]["alarm"] = alarm
            _save_index(p, index)
            meta["alarm"] = alarm
            events.append(_event(p, {"preset": preset, "label": label, "event": "alarm_on" if alarm else "alarm_off",
                                     "why": "spot checks found %d errors in the last %d; checking more often until they "
                                            "settle" % (sum(1 for a in recent if not a[2]), len(recent)) if alarm else
                                            "recent spot checks are clean again"}))
    for cls_ in o.breakers:
        events.append(_event(p, {"preset": preset, "label": label, "event": "class_reset",
                                 "why": "local '%s' answers were found wrong twice: stopped answering that class locally "
                                        "and forgot its recalled labels until fresh labels rebuild it" % cls_}))
    o.breakers = []
    if o.ep is not None and o.ep_dirty:  # the fast memory answers only where its record reaches the target
        win = (ps.get("ep_window") or [])[-EPISODE_WINDOW:]
        op = operating_point([(e[3], e[2] == e[1]) for e in win], float(cfg["target"]), float(cfg["min_confidence"]))
        was = o.ep.threshold
        o.ep.threshold = op["threshold"] if op else None
        o.ep.stats = {"window": len(win), "accuracy": op["accuracy"] if op else None,
                      "coverage": op["coverage"] if op else None}
        if (was is None) != (o.ep.threshold is None):
            events.append(_event(p, {"preset": preset, "label": label, "event": "memory_on" if o.ep.threshold is not None
                                     else "memory_off", "why": ("answers like earlier ones are now trusted: %d%% right on "
                                     "%d of your labels" % (round(op["accuracy"] * 100), op["accepted"])) if op else
                                     "recall paused: its recent record fell below the target"}))
    if ps.get("hold", 0) > 0 or ps.get("since_check", 0) < int(cfg["check_every"]) or \
            len(ps["window"]) < int(cfg["min_window"]):
        return
    ps["since_check"] = 0
    v = compare(ps["window"], meta, cfg)
    ps["last_check"] = {k: v[k] for k in ("n", "acc_champion", "acc_challenger", "coverage_champion",
                                          "coverage_challenger", "promote")}
    ps["last_check"]["t"] = int(time.time())
    if not v["promote"]:
        ps.pop("ready", None)
        return
    why = _why(preset, v, bool(meta.get("enabled")), label)
    if not cfg["auto_promote"]:
        if not ps.get("ready"):
            events.append(_event(p, {"preset": preset, "event": "ready", "why": why}))
        ps["ready"] = {"t": int(time.time()), "why": why}
        return
    version = promote(p, preset, o.ch, o.model, meta, v, ps, why)
    ps.pop("ready", None)
    ps["window"], ps["audits"], ps["since_check"] = [], [], 0
    events.append(_event(p, {"preset": preset, "label": label, "event": "promoted", "version": version, "why": why,
                             "metrics": {k: v[k] for k in ("n", "acc_champion", "acc_challenger", "coverage_champion",
                                                           "coverage_challenger")}}))
    o.dirty = False  # the challenger is reset from the new champion on next use
    try:
        for ext in (".json", ".bin"):
            os.unlink(str(p.challenger(preset)) + ext)
    except OSError:
        pass
    ps["base"] = None


_SEEN = {}  # labels file -> (size, day) when this process last found nothing left to learn


def step(root, policy, budget_ms=None, cat=None, routes=None, force_check=False):
    """Learn from labels not seen yet, then check for promotions and pauses. Returns the events."""
    cfg = config(policy)
    if not cfg["enabled"]:
        return []
    p = Paths(root)
    key = str(p.labels)
    if not force_check:
        try:
            size_now = os.stat(key).st_size
        except OSError:
            size_now = 0
        if _SEEN.get(key) == (size_now, _today()):
            return []  # nothing new since this process last looked: no lock, no state read
    if not p.dir.exists():
        return []
    events = []
    with Lock(p.lock, wait=0.05 if budget_ms else 2.0) as lk:
        if not lk.ok:
            return []
        state = load_state(p)
        try:
            size = p.labels.stat().st_size
        except OSError:
            size = 0
        if state["cursor"] > size:
            state["cursor"] = 0
        if state["cursor"] == size and not force_check:
            if state.get("maintained") != _today():
                _maintain(p, cfg, state)
                _write_json(p.state, state)
            _SEEN[key] = (size, _today())
            return []
        deadline = time.perf_counter() + budget_ms / 1000.0 if budget_ms else None
        cat = cat or dd.load_catalog(policy)
        autos = dd.read_json(p.auto, default={}) or {}
        if routes is None:
            routes = dd.discover_routes(p.root, policy)
        reg = fm.Registry(str(p.root))
        opened = {}
        with open(str(p.labels), "ab+") as fh:
            fh.seek(state["cursor"])
            while True:
                if deadline is not None and time.perf_counter() > deadline:
                    break
                line = fh.readline()
                if not line or not line.endswith(b"\n"):
                    break
                state["cursor"] = fh.tell()
                try:
                    lab = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                preset = lab.get("preset")
                if not preset:
                    continue
                if preset not in opened:
                    opened[preset] = _open_preset(p, reg, state, preset, cat, autos, routes, events)
                o = opened[preset]
                if o is not None:
                    _learn_one(o, lab, cfg)
        if force_check:
            for preset in list(state["presets"]):
                if preset not in opened:
                    opened[preset] = _open_preset(p, reg, state, preset, cat, autos, routes, events)
        for preset, o in opened.items():
            if o is None:
                continue
            if force_check:
                o.ps["since_check"] = max(o.ps.get("since_check", 0), int(cfg["check_every"]))
            if o.dirty:
                o.ch.save(p.challenger(preset))
            _checks(p, o, preset, cfg, events, autos)
            if o.ep is not None and o.ep_dirty:
                o.ep.save(p.episodes(preset))
            if o.adapt_dirty:
                index = _index(p)
                if preset in index and index[preset].get("version_id", "x") == o.meta.get("version_id", "x"):
                    index[preset]["adapt"] = o.adapt
                    _save_index(p, index)
        _maintain(p, cfg, state)
        _write_json(p.state, state)
        if size > MAX_LABEL_BYTES and state["cursor"] >= size:
            _compact_labels(p, state)
        try:
            if state["cursor"] == os.stat(key).st_size:
                _SEEN[key] = (state["cursor"], _today())
        except OSError:
            pass
    return events


def _maintain(p, cfg, state):
    """Once a day: delete spot-check inputs older than audit_ttl_days and recent decisions older than recent_hours."""
    if state.get("maintained") == _today():
        return
    state["maintained"] = _today()
    try:
        if p.queue.exists():
            ttl = float(cfg["audit_ttl_days"]) * 86400
            rows = [r for r in _queue(p) if time.time() - r.get("t", 0) <= ttl]
            _save_queue(p, rows)
        if p.recent.exists():
            _trim_recent(p, cfg)
    except OSError:
        pass


def _compact_labels(p, state):
    """Keep the newest half of a very large label file (they are only needed again for status and replay)."""
    try:
        with open(str(p.labels), "rb") as fh:
            fh.seek(-MAX_LABEL_BYTES // 2, os.SEEK_END)
            fh.readline()
            tail = fh.read()
        _write_bytes(p.labels, tail)
        state["cursor"] = len(tail)
        _write_json(p.state, state)
    except OSError:
        pass


# ---------------------------------------------------------------- episodic memory: learning from each answer

_EP_CACHE = {}


def _sig(path):
    try:
        st = os.stat(str(path))
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return None


class Episodes(object):
    """Every labeled decision of one type, recalled by similarity: the fast memory that learns from each answer.

    Each decision keeps its 32 strongest word features. A new input's nearest earlier decisions vote, weighted by
    similarity. The tier answers only once its test-then-store record on your labels reaches the accuracy target,
    at the confidence threshold that record supports.
    """
    MAGIC = b"DDE1"
    NONE = 0xFFFFFFFF

    def __init__(self, preset):
        self.preset = preset
        self.classes, self.labels, self.times, self.vecs = [], [], [], []
        self.threshold, self.adapt, self.stats = None, {}, {}
        self._post = None

    @staticmethod
    def compact(vec):
        top = sorted(((k, v) for k, v in vec.items() if k < fm.SEM_BASE), key=lambda kv: (-abs(kv[1]), kv[0]))[:EPISODE_K]
        norm = math.sqrt(sum(v * v for _, v in top)) or 1.0
        return {int(k): v / norm for k, v in top}

    def add(self, vec, label, t):
        """Remember a labeled decision. Near-identical earlier decisions with a different label are forgotten (the
        newest label wins), and exact repeats are refreshed rather than stored twice."""
        if not vec:
            return
        if label not in self.classes:
            self.classes.append(label)
        li = self.classes.index(label)
        q = self.compact(vec)
        if self.vecs:
            post, acc = self._postings(), {}
            for b, v in q.items():
                for i, w in post.get(b, ()):
                    acc[i] = acc.get(i, 0.0) + v * w
            drop = set(i for i, sim in acc.items()
                       if (sim >= 0.9 and self.labels[i] != li) or (sim >= 0.98 and self.labels[i] == li))
            if drop:
                keep = [i for i in range(len(self.labels)) if i not in drop]
                self.labels = [self.labels[i] for i in keep]
                self.times = [self.times[i] for i in keep]
                self.vecs = [self.vecs[i] for i in keep]
        self.labels.append(li)
        self.times.append(int(t))
        self.vecs.append(q)
        if len(self.labels) > EPISODE_MAX:
            drop = len(self.labels) - EPISODE_MAX
            del self.labels[:drop], self.times[:drop], self.vecs[:drop]
        self._post = None

    def forget_class(self, label):
        """Drop every remembered decision of one class (its convention changed); fresh labels rebuild it."""
        if label not in self.classes:
            return 0
        li = self.classes.index(label)
        keep = [i for i in range(len(self.labels)) if self.labels[i] != li]
        gone = len(self.labels) - len(keep)
        self.labels = [self.labels[i] for i in keep]
        self.times = [self.times[i] for i in keep]
        self.vecs = [self.vecs[i] for i in keep]
        self.adapt = {k: v for k, v in (self.adapt or {}).items() if k != label}
        self._post = None
        return gone

    def _postings(self):
        if self._post is None:
            post = {}
            for i, v in enumerate(self.vecs):
                for b, w in v.items():
                    post.setdefault(b, []).append((i, w))
            self._post = post
        return self._post

    def predict(self, vec):
        """(label, confidence, best similarity, voters), or None when nothing similar has been labeled."""
        if not self.vecs or not vec:
            return None
        q = self.compact(vec)
        post, acc = self._postings(), {}
        for b, v in q.items():
            for i, w in post.get(b, ()):
                acc[i] = acc.get(i, 0.0) + v * w
        near = sorted(((sim, i) for i, sim in acc.items() if sim >= EPISODE_FLOOR), reverse=True)[:EPISODE_KNN]
        if not near:
            return None
        votes = {}
        for sim, i in near:
            votes[self.labels[i]] = votes.get(self.labels[i], 0.0) + sim ** 4
        top = max(votes, key=votes.get)
        share = votes[top] / sum(votes.values())
        best = max(sim for sim, i in near if self.labels[i] == top)
        return self.classes[top], round(share * min(1.0, best), 4), round(best, 4), len(near)

    def save(self, prefix):
        n = len(self.labels)
        b, w = array("I"), array("f")
        for v in self.vecs:
            items = sorted(v.items())
            for k, x in items:
                b.append(k)
                w.append(x)
            for _ in range(EPISODE_K - len(items)):
                b.append(self.NONE)
                w.append(0.0)
        lab, tim = array("H", self.labels), array("I", self.times)
        if sys.byteorder != "little":
            for arr in (b, w, lab, tim):
                arr.byteswap()
        _write_bytes(Path(str(prefix) + ".bin"), self.MAGIC + lab.tobytes() + tim.tobytes() + b.tobytes() + w.tobytes())
        _write_json(Path(str(prefix) + ".json"), {"v": 1, "preset": self.preset, "classes": self.classes, "n": n,
                                                  "k": EPISODE_K, "threshold": self.threshold,
                                                  "adapt": self.adapt, "stats": self.stats})

    @classmethod
    def load(cls, prefix):
        head = dd.read_json(Path(str(prefix) + ".json"), default=None)
        ep = cls(Path(str(prefix)).name)
        if not head or head.get("v") != 1:
            return ep
        try:
            data = Path(str(prefix) + ".bin").read_bytes()
        except OSError:
            return ep
        n, K = int(head["n"]), int(head.get("k", EPISODE_K))
        if data[:4] != cls.MAGIC or len(data) != 4 + 2 * n + 4 * n + 8 * n * K:
            return ep
        lab, tim, b, w = array("H"), array("I"), array("I"), array("f")
        o = 4
        lab.frombytes(data[o:o + 2 * n])
        o += 2 * n
        tim.frombytes(data[o:o + 4 * n])
        o += 4 * n
        b.frombytes(data[o:o + 4 * n * K])
        o += 4 * n * K
        w.frombytes(data[o:o + 4 * n * K])
        if sys.byteorder != "little":
            for arr in (lab, tim, b, w):
                arr.byteswap()
        bl, wl = b.tolist(), w.tolist()
        ep.classes, ep.labels, ep.times = list(head["classes"]), lab.tolist(), tim.tolist()
        ep.vecs = [{bl[j]: wl[j] for j in range(i * K, (i + 1) * K) if bl[j] != cls.NONE} for i in range(n)]
        ep.threshold, ep.adapt, ep.stats = head.get("threshold"), head.get("adapt") or {}, head.get("stats") or {}
        return ep

    @classmethod
    def head(cls, prefix):
        """The header alone (classes, threshold), cached by file signature: cheap enough for every request."""
        path = str(prefix) + ".json"
        sig = _sig(path)
        hit = _EP_CACHE.get(path)
        if hit and hit[0] == sig:
            return hit[1]
        head = dd.read_json(path, default=None) if sig else None
        _EP_CACHE[path] = (sig, head)
        return head

    @classmethod
    def get(cls, prefix):
        key = str(prefix) + ".bin"
        sig = _sig(key)
        hit = _EP_CACHE.get(key)
        if hit and hit[0] == sig:
            return hit[1]
        ep = cls.load(prefix)
        _EP_CACHE[key] = (sig, ep)
        return ep


def answer_label(kind, raw):
    """The label a raw answer points to (learned, recalled or from the model)."""
    y, _ = distribution(kind, raw)
    return y


def local_answer(kind, spec, y, conf, evidence):
    """A raw answer for one label at a given confidence, in the shape the finalizer reads."""
    conf = min(0.99, max(0.0, float(conf)))
    if kind == "verify":
        return {"type": "verify", "p_true": round(conf if y == "true" else 1.0 - conf, 4), "evidence": evidence}
    if kind == "score":
        lo, hi = int(spec.get("min", 1)), int(spec.get("max", 5))
        others = [str(i) for i in range(lo, hi + 1) if str(i) != y]
        probs = {y: conf}
        for o in others:
            probs[o] = (1.0 - conf) / max(1, len(others))
        return {"type": "score", "probs": probs, "evidence": evidence}
    return {"type": "choose", "probs": {y: conf, "_rest": round(1.0 - conf, 4)}, "evidence": evidence}


def familiarity(model, vec):
    """Share of the input's (non-meaning) feature weight that the model has learned weights for; 0 when there is
    too little text to judge. Low values mean the input is unlike anything the model was trained on."""
    if not model or not vec:
        return None
    known = model.get("_idx") if model.get("_idx") is not None else (model.get("weights") or {})
    tot = kn = 0.0
    n = 0
    for k, v in vec.items():
        if k >= fm.SEM_BASE:
            continue
        n += 1
        v2 = v * v
        tot += v2
        if k in known:
            kn += v2
    if n < 4 or tot <= 0:
        return 0.0
    return kn / tot


def check_probability(cfg, conf, threshold, fam=None):
    """Chance of spot-checking one local answer: about audit_rate on average, up to 2x near the threshold and 1.5x
    more for unfamiliar inputs. Each check is weighted by audit_rate / probability, so accuracy estimates stay
    unbiased."""
    rate = float(cfg["audit_rate"])
    if rate <= 0 or not cfg.get("targeted_checks", True):
        return rate
    try:
        thr = float(threshold)
        m = 1.0 if thr >= 1.0 else min(1.0, max(0.0, (float(conf) - thr) / max(1e-6, 1.0 - thr)))
    except (TypeError, ValueError):
        m = 0.5
    f = 2.0 - 1.5 * m
    if fam is not None and fam < 0.8:
        f *= 1.5
    return min(1.0, max(rate / 2.0, min(4.0 * rate, rate * f)))


def _breaker(ps, tier, label, ok):
    """Record a check of one local answer (a spot check, or a shadow test against a label); True when this makes
    two errors among the last five checks of that class, from either local tier. Both tiers then distrust the class:
    its recalled labels are forgotten and the model stops answering it locally until a better model switches in."""
    key = str(label)
    hist = ps.setdefault("class_checks", {}).setdefault(key, [])
    hist.append(1 if ok else 0)
    del hist[:-5]
    if not ok and hist.count(0) >= 2:
        hist[:] = []
        return True
    return False


def _trip(o, cfg, label):
    """A class's convention has changed: forget its recalled labels and stop the model answering it locally."""
    if o.meta:
        o.adapt = _bump(cfg, o.adapt, label, float(cfg["adapt_max"]))
        o.adapt_dirty = True
    if o.ep is not None:
        o.ep.forget_class(label)
        o.ps.setdefault("ep_window", [])[:] = [e for e in o.ps.get("ep_window", []) if e[2] != label]
        o.ep_dirty = True
    o.breakers.append(label)


def _clip_adapt(cfg, v):
    return min(float(cfg["adapt_max"]), max(0.0, v))


def adapt_for(adapt, label):
    """The threshold offset for answers of one class (offsets are kept per class: errors in one class should not
    silence the others). Older files may hold one number for all classes."""
    if isinstance(adapt, dict):
        return float(adapt.get(str(label), 0.0))
    try:
        return float(adapt or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _bump(cfg, adapt, label, step_):
    d = dict(adapt) if isinstance(adapt, dict) else {}
    v = _clip_adapt(cfg, adapt_for(adapt, label) + step_)
    if v > 1e-4:
        d[str(label)] = round(v, 4)
    else:
        d.pop(str(label), None)
    return d


# ---------------------------------------------------------------- escalation: a stronger model for hard cases

ESCALATE_DEFAULTS = {
    "enabled": True,
    "below": 0.6,             # escalate an answer whose confidence is below this
    "per_day": 30,            # stronger-model calls per day, at most one per request
    "cli": "sonnet", "api": "claude-sonnet-5-5",
    "presets": ["route", "risk", "breaking_change", "security_relevant", "secret_leak", "review_verdict", "severity",
                "issue_type", "production_impact", "excessive_agency", "prompt_injection_exposure",
                "agent_autonomy_risk", "unvalidated_model_output", "pii_in_ai_data", "ml_data_leakage",
                "migration_required", "rollback_safe", "error_category", "ai_failure", "needs_evals"],
}
_GATE_KEY = re.compile(r"([A-Za-z0-9_\-]+)\.(?:score|level|p_true|confidence|margin|choice)")


def escalate_config(policy):
    cfg = dict(ESCALATE_DEFAULTS)
    user = (policy or {}).get("escalate")
    if user is False:
        cfg["enabled"] = False
    elif isinstance(user, dict):
        cfg.update({k: v for k, v in user.items() if k in ESCALATE_DEFAULTS})
    if str(os.environ.get("DEV_DECIDE_ESCALATE", "")).lower() in ("0", "off", "false", "no"):
        cfg["enabled"] = False
    return cfg


def top_confidence(kind, raw):
    y, q = distribution(kind, raw)
    if y is None:
        return None
    return q.get(y, 0.0)


def escalation_keys(policy, cat, questions, bundles, decided):
    """Keys of one item worth a stronger model: the model answered, unsure, and the decision matters."""
    cfg = escalate_config(policy)
    if not cfg["enabled"]:
        return []
    important = set(cfg["presets"])
    for b in bundles or []:
        important |= set(_GATE_KEY.findall(json.dumps((cat.get("gates") or {}).get(b) or {})))
    keys = []
    for key, q in questions.items():
        a = decided.get(key)
        if not a or a.get("via") not in ("llm", "llm-deep"):
            continue
        if not (q.preset in important or key in important or not q.preset):
            continue
        conf = top_confidence(q.kind, a)
        if conf is not None and conf < float(cfg["below"]):
            keys.append(key)
    return keys


def claim_escalation(root, policy):
    """True when today's budget of stronger-model calls allows one more (and counts it)."""
    cfg = escalate_config(policy)
    p = Paths(root).ensure()
    with Lock(p.lock, wait=0.2) as lk:
        if not lk.ok:
            return False
        state = load_state(p)
        e = state.get("escalate") or {}
        if e.get("day") != _today():
            e = {"day": _today(), "n": 0}
        if int(e.get("n", 0)) >= int(cfg["per_day"]):
            return False
        e["n"] = int(e.get("n", 0)) + 1
        state["escalate"] = e
        _write_json(p.state, state)
        return True


def strong_policy(policy):
    cfg = escalate_config(policy)
    out = dict(policy or {})
    out["_force_model"] = {"cli": cfg["cli"], "api": cfg["api"]}
    return out


# ---------------------------------------------------------------- memory of corrections

_MEM_CACHE = {}


class Memory(object):
    """Your corrections, recalled for near-identical inputs by cosine similarity of hashed f2 vectors."""

    def __init__(self, path):
        self.path = Path(path)
        self.entries = [e for e in _read_lines(self.path) if e.get("preset") and e.get("x")]
        self._index = {}

    @classmethod
    def get(cls, path):
        try:
            st = os.stat(str(path))
            sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            sig = None
        hit = _MEM_CACHE.get(str(path))
        if hit and hit[0] == sig:
            return hit[1]
        mem = cls(path)
        _MEM_CACHE[str(path)] = (sig, mem)
        return mem

    def has(self, preset):
        return any(e["preset"] == preset for e in self.entries)

    def _postings(self, preset):
        if preset not in self._index:
            post, ents = {}, []
            for e in self.entries:
                if e["preset"] != preset:
                    continue
                vec = dec(e["x"])
                i = len(ents)
                ents.append(e)
                for b, v in vec.items():
                    post.setdefault(b, []).append((i, v))
            self._index[preset] = (ents, post)
        return self._index[preset]

    def nearest(self, preset, x, k=1, floor=0.0):
        ents, post = self._postings(preset)
        if not ents or not x:
            return []
        acc = {}
        for b, v in x.items():
            for i, w in post.get(b, ()):
                acc[i] = acc.get(i, 0.0) + v * w
        best = sorted(((s, i) for i, s in acc.items() if s >= floor), reverse=True)[:k]
        return [(ents[i], s) for s, i in best]

    def add(self, entry, cap):
        x = dec(entry["x"])
        same = self.nearest(entry["preset"], x, k=5, floor=0.995)
        drop = set(id(e) for e, _ in same if e.get("key") == entry.get("key"))
        keep = [e for e in self.entries if id(e) not in drop]
        keep.append(entry)
        mine = [e for e in keep if e["preset"] == entry["preset"]]
        if len(mine) > cap:
            gone = set(id(e) for e in mine[:len(mine) - cap])
            keep = [e for e in keep if id(e) not in gone]
        self.entries = keep
        self._index = {}
        _write_text(self.path, "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in keep))


def memory_answer(kind, spec, y, sim, entry, cfg):
    """A memory hit as a raw answer: confidence grows from 0.86 at the similarity floor to 0.97."""
    floor = float(cfg["memory_similarity"])
    conf = min(0.97, 0.86 + 0.7 * max(0.0, sim - floor))
    ev = ["matches your correction %s (similarity %.2f)" % (entry.get("id") or "", sim)]
    if kind == "verify":
        p = conf if y == "true" else 1.0 - conf
        return {"type": "verify", "p_true": round(p, 4), "evidence": ev}
    if kind == "score":
        lo, hi = int(spec.get("min", 1)), int(spec.get("max", 5))
        others = [str(i) for i in range(lo, hi + 1) if str(i) != y]
        probs = {y: conf}
        for o in others:
            probs[o] = (1.0 - conf) / max(1, len(others))
        return {"type": "score", "probs": probs, "evidence": ev}
    return {"type": "choose", "probs": {y: conf, "_rest": round(1.0 - conf, 4)}, "evidence": ev}


# ---------------------------------------------------------------- recent decisions (so `fix` can learn)

def _remember_recent(p, cfg, did, items):
    if not items or int(cfg["recent"]) <= 0:
        return
    _append(p.recent, [{"t": int(time.time()), "id": did, "items": items}])
    try:  # cheap checks first: size, then the age of the oldest entry; count lines only now and then
        trim = p.recent.stat().st_size > 1_500_000
        if not trim:
            with open(str(p.recent), "rb") as fh:
                m = re.match(rb'\{"t":(\d+)', fh.read(32))
            trim = bool(m) and time.time() - int(m.group(1)) > float(cfg["recent_hours"]) * 3600
        if not trim and random.random() < 0.1:
            with open(str(p.recent), "rb") as fh:
                trim = sum(1 for _ in fh) > 2 * int(cfg["recent"])
        if trim:
            _trim_recent(p, cfg)
    except (OSError, ValueError):
        pass


def _trim_recent(p, cfg):
    cutoff = time.time() - float(cfg["recent_hours"]) * 3600
    rows = [r for r in _read_lines(p.recent) if r.get("t", 0) >= cutoff][-int(cfg["recent"]):]
    rows = [dict([("t", r.get("t", 0))] + [(k, v) for k, v in r.items() if k != "t"]) for r in rows]
    _write_text(p.recent, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))


def touch_recent(root, policy, did):
    """A cached answer counts as the latest decision, so `fix last` refers to what was just shown."""
    try:
        cfg = config(policy)
        if not cfg["enabled"] or not did:
            return
        p = Paths(root)
        data = p.recent.read_bytes()
        at = data.rfind(('"id":"%s",' % did).encode("utf-8"))
        if at < 0:
            return
        start = data.rfind(b"\n", 0, at) + 1
        end = data.find(b"\n", at)
        if end == len(data) - 1 or end < 0:
            return  # already the latest
        rec = json.loads(data[start:end].decode("utf-8"))
        rec["t"] = int(time.time())
        _append(p.recent, [rec])
    except Exception:
        pass


def find_recent(p, cfg, did):
    cutoff = time.time() - float(cfg["recent_hours"]) * 3600
    rows = [r for r in _read_lines(p.recent) if r.get("t", 0) >= cutoff]
    if not rows:
        return None
    if did in (None, "", "last"):
        return rows[-1]
    for r in reversed(rows):
        if r.get("id") == did:
            return r
    return None


# ---------------------------------------------------------------- spot checks

def _today():
    return time.strftime("%Y-%m-%d")


def _queue(p):
    return _read_lines(p.queue)


def _save_queue(p, rows):
    _write_text(p.queue, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))


def enqueue(p, cfg, entries):
    if not entries:
        return 0
    with Lock(p.lock, wait=0.05) as lk:
        if not lk.ok:
            return 0
        rows = _queue(p)
        room = MAX_QUEUE - len(rows)
        if room <= 0:
            return 0
        _append(p.queue, entries[:room])
        return min(room, len(entries))


def claim(p, cfg, n=None):
    """Take up to n queued inputs to spot-check now, within today's cap. Expired inputs are deleted."""
    with Lock(p.lock, wait=0.05) as lk:
        if not lk.ok:
            return []
        state = load_state(p)
        if state["audit"].get("day") != _today():
            state["audit"] = {"day": _today(), "sent": 0}
        left = int(cfg["audits_per_day"]) - int(state["audit"].get("sent", 0))
        n = min(left, int(cfg["audit_batch"]) if n is None else n)
        rows = _queue(p)
        now = time.time()
        ttl = float(cfg["audit_ttl_days"]) * 86400
        rows = [r for r in rows if now - r.get("t", 0) <= ttl]
        picked = []
        if n > 0:
            for r in rows:
                if len(picked) >= n:
                    break
                if now - r.get("claimed", 0) > 300:
                    r["claimed"] = int(now)
                    picked.append(r)
        _save_queue(p, rows)
        if picked:
            state["audit"]["sent"] = int(state["audit"].get("sent", 0)) + len(picked)
            _write_json(p.state, state)
        return picked


def finish(p, done, failed):
    with Lock(p.lock, wait=1.0) as lk:
        if not lk.ok:
            return
        rows = _queue(p)
        done = set(done)
        failed = set(failed)
        out = []
        for r in rows:
            if r.get("qid") in done:
                continue
            if r.get("qid") in failed:
                r["claimed"] = 0
            out.append(r)
        _save_queue(p, out)


class _AuditFacts(object):
    def __init__(self, facts):
        self.facts = [tuple(f) for f in (facts or [])][:14]


class _AuditState(object):
    def __init__(self, index, item, key, facts):
        self.index, self.item, self.pending, self.facts, self.hints = index, item, [key], _AuditFacts(facts), {}


def run_spot_checks(root, policy, entries, routes=None, cat=None):
    """Ask Claude about queued inputs that were answered locally. Returns (labels written, info)."""
    import inputs
    import llm
    import request
    p = Paths(root)
    cat = cat or dd.load_catalog(policy)
    autos = dd.read_json(p.auto, default={}) or {}
    if routes is None:
        routes = dd.discover_routes(p.root, policy)
    states, questions, used = [], {}, []
    for i, e in enumerate(entries):
        kind, spec = preset_spec(cat, autos, e["preset"])
        if kind is None or not e.get("view"):
            continue
        key = e["preset"]
        if key not in questions:
            preset = e["preset"] if dd.find_preset(cat, e["preset"])[0] else None
            questions[key] = request.Question(key, kind, preset, spec)
        item = inputs.Item(str(len(states) + 1))
        item.kind, item.text = "text", e["view"]
        states.append(_AuditState(900000 + i, item, key, e.get("facts")))
        used.append(e)
    if not states:
        finish(p, [e.get("qid") for e in entries], [])
        return 0, {}
    ctx = {"routes": routes, "policy": policy}
    results, info = llm.ask(states, questions, ctx, set(), policy, str(p.root))
    labels, done, failed = [], [], []
    now = int(time.time())
    for st, e in zip(states, used):
        a = results.get(st.index, {}).get(st.pending[0])
        y, qd = distribution(questions[st.pending[0]].kind, a) if a else (None, None)
        if y is None:
            failed.append(e.get("qid"))
            continue
        labels.append({"t": now, "id": e.get("id"), "item": e.get("item"), "key": e.get("key"), "preset": e["preset"],
                       "kind": e.get("kind"), "src": "audit", "y": y, "q": qd, "conf": max(qd.values()),
                       "x": e.get("x"), "local": e.get("local"), "wis": e.get("wis", 1.0)})
        done.append(e.get("qid"))
    p.ensure()
    _append(p.labels, labels)
    finish(p, done + [e.get("qid") for e in entries if e not in used], failed)
    return len(labels), info


# ---------------------------------------------------------------- the engine's view

class Session(object):
    """Used by engine.decide for one request."""

    def __init__(self, root, policy, cat, mode):
        self.cfg = config(policy)
        self.enabled = bool(self.cfg["enabled"])
        self.root, self.policy, self.cat, self.mode = Path(root), policy, cat, mode
        self.p = Paths(root)
        self.mem = Memory.get(self.p.memory) if self.enabled and self.cfg["memory"] else None
        self._autos = None
        self.audit_job = None
        self.events = []
        self.new_labels = 0
        self.queued = 0
        self.spot = None

    # -- names
    def autos(self):
        if self._autos is None:
            self._autos = dd.read_json(self.p.auto, default={}) or {}
        return self._autos

    def name(self, q):
        """The model name for a question: its preset, or a stable name for a custom question."""
        if q.preset:
            return q.preset
        return auto_key(q) if self.enabled else None

    def _register_auto(self, q, name):
        autos = self.autos()
        rec = autos.get(name)
        if rec is None:
            rec = dict(auto_spec(q), first_seen=int(time.time()), count=0)
            autos[name] = rec
        rec["count"] = int(rec.get("count", 0)) + 1
        rec["last_seen"] = int(time.time())
        return True

    # -- memory tier
    def recall(self, q, st, name):
        if self.mem is None or st.x is None or st.item.kind != "text" or not name or not self.mem.has(name):
            return None
        x2 = st.x.get("f2")
        hit = self.mem.nearest(name, x2, k=1, floor=float(self.cfg["memory_similarity"]))
        if not hit:
            return None
        entry, sim = hit[0]
        if entry.get("kind") and entry["kind"] != q.kind:
            return None
        ans = memory_answer(q.kind, q.spec, str(entry["y"]), sim, entry, self.cfg)
        ans["via"] = "memory"
        ans["similarity"] = round(sim, 4)
        if q.preset:
            ans["preset"] = q.preset
        else:
            ans.update(adhoc_fields(q))
        return ans

    def episodic(self, q, st, name, routes=None):
        """Answer from labeled decisions like this one, if that has proven accurate for this decision type."""
        if not self.enabled or not self.cfg.get("episodic", True) or st.x is None or not name:
            return None
        prefix = self.p.episodes(name)
        head = Episodes.head(prefix)
        if not head or head.get("threshold") is None:
            return None
        pred = Episodes.get(prefix).predict(st.x.get("f1"))
        if not pred:
            return None
        y, conf, best, voters = pred
        if conf < float(head["threshold"]) + adapt_for(head.get("adapt"), y):
            return None
        if q.kind == "choose" and q.spec.get("options") == "auto" and routes is not None and \
                y not in set(r["name"] for r in routes) | {"none"}:
            return None
        ev = ["like %d earlier decision%s (closest similarity %.2f)" % (voters, "" if voters == 1 else "s", best)]
        ans = local_answer(q.kind, q.spec, y, conf, ev)
        ans["via"] = "episodic"
        ans["similarity"] = best
        if q.preset:
            ans["preset"] = q.preset
        else:
            ans.update(adhoc_fields(q))
        return ans

    def familiarity(self, reg, name, x):
        if not self.enabled or x is None:
            return None
        model = reg.load(name)
        return familiarity(model, x.get(reg.featurizer(name))) if model else None

    def precedents(self, work, questions):
        if self.mem is None or not self.mem.entries:
            return []
        lines, seen = [], set()
        for st in work:
            if st.x is None or st.item.kind != "text":
                continue
            x2 = None
            for key in st.pending:
                q = questions.get(key)
                name = self.name(q) if q else None
                if not name or not self.mem.has(name):
                    continue
                x2 = x2 or st.x.get("f2")
                for e, sim in self.mem.nearest(name, x2, k=2, floor=0.35):
                    if not e.get("text") or (name, e.get("text")) in seen:
                        continue
                    seen.add((name, e.get("text")))
                    text = re.sub(r"\s+", " ", e["text"])[:240]
                    lines.append('%s: "%s" -> %s' % (key, text, e["y"]))
                    if len(lines) >= 6:
                        return lines
        return lines

    # -- spot checks that ride along with a model call this request is making anyway
    def start_spot_checks(self, routes):
        if not self.enabled or self.mode != "hybrid" or float(self.cfg["audit_rate"]) <= 0:
            return
        try:
            if not self.p.queue.exists() or self.p.queue.stat().st_size == 0:
                return
        except OSError:
            return
        picked = claim(self.p, self.cfg)
        if not picked:
            return
        box = {}

        def run():
            try:
                box["n"], box["info"] = run_spot_checks(self.root, self.policy, picked, routes=routes, cat=self.cat)
            except Exception as exc:  # a spot check must never break the decision
                box["error"] = type(exc).__name__
                finish(self.p, [], [e.get("qid") for e in picked])

        t = threading.Thread(target=run, name="dev-decide-spot-check", daemon=True)
        t.start()
        self.audit_job = (t, box, len(picked))

    def join_spot_checks(self, timeout=12.0):
        if not self.audit_job:
            return
        t, box, n = self.audit_job
        t.join(timeout)
        info = box.get("info") or {}
        self.spot = {"sent": n, "labels": box.get("n", 0)}
        if info.get("cost_usd") is not None:
            self.spot["cost_usd"] = info["cost_usd"]
        if box.get("error") or info.get("error"):
            self.spot["error"] = box.get("error") or info.get("error")
        self.new_labels += box.get("n", 0)
        self.audit_job = None

    # -- after the decision
    def observe(self, states, questions, did, finals, reg):
        if not self.enabled:
            return
        if str(self.p.dir) not in _READY:
            self.p.ensure()
            _READY.add(str(self.p.dir))
        cfg = self.cfg
        now = int(time.time())
        labels, audits, recent = [], [], []
        autos_changed = False
        for st, fin in zip(states, finals):
            if st.error or st.x is None or not isinstance(fin, dict):
                continue
            answers = fin.get("answers") or {}
            keys = {}
            for key, q in questions.items():
                a = st.decided.get(key)
                if not a or a.get("error"):
                    continue
                name = self.name(q)
                if not name:
                    continue
                if not q.preset:
                    autos_changed = self._register_auto(q, name) or autos_changed
                final = answers.get(key) or {}
                via = a.get("via")
                keys[key] = {"p": name, "k": q.kind, "via": via, "a": answer_value(final),
                             "c": final.get("confidence")}
                if via in MODEL_VIA:
                    y, qd = distribution(q.kind, a)
                    if y is not None:
                        lab = {"t": now, "id": did, "item": st.item.id, "key": key, "preset": name,
                               "kind": q.kind, "src": "llm", "y": y, "q": qd,
                               "conf": final.get("confidence") or max(qd.values()),
                               "x": self._vectors(st, name, reg)}
                        if via == "llm-strong":
                            lab["strong"] = True
                        labels.append(lab)
                elif via in LOCAL_VIA and self.mode == "hybrid" and cfg["keep_text"]:
                    said = answer_value(final)
                    pc = check_probability(cfg, final.get("confidence"), self._threshold(reg, name, via, said),
                                           a.get("familiarity"))
                    if via == "episodic":
                        pc = min(1.0, pc * EPISODIC_CHECKS)  # recall is the tier most exposed to stale labels
                    if (reg.index.get(name) or {}).get("alarm"):
                        pc = min(0.5, pc * ALARM_BOOST)
                    if pc > 0 and random.random() < pc:
                        audits.append({"qid": "%s-%s-%s" % (did, st.item.id or "", key), "t": now, "id": did,
                                       "item": st.item.id, "key": key, "preset": name, "kind": q.kind,
                                       "view": self._view(st), "facts": [list(f) for f in st.facts.facts[:14]],
                                       "x": self._vectors(st, name, reg), "p": round(pc, 4),
                                       "wis": round(float(cfg["audit_rate"]) / pc, 4) if pc else 1.0,
                                       "local": {"via": via, "answer": answer_value(final),
                                                 "conf": final.get("confidence"), "cid": self._cid(reg, name)}})
            if keys and len(recent) < 3:
                rec = {"item": st.item.id, "kind": st.item.kind, "keys": keys,
                       "x": {"f1": enc(st.x.get("f1"))}}
                if st.item.kind == "text":
                    rec["x"]["f2"] = enc(st.x.get("f2"))
                elif any(reg.featurizer(v["p"]) == "f2" for v in keys.values()):
                    rec["x"]["f2"] = enc(st.x.get("f2"))
                if cfg["keep_text"]:
                    rec["text"] = self._view(st, RECENT_TEXT)
                recent.append(rec)
        if autos_changed:
            _write_json(self.p.auto, self.autos(), indent=1)
        _append(self.p.labels, labels)
        self.new_labels += len(labels)
        if audits:
            self.queued = enqueue(self.p, cfg, audits)
        _remember_recent(self.p, cfg, did, recent)

    def _vectors(self, st, name, reg):
        out = {"f1": enc(st.x.get("f1"))}
        fv = reg.featurizer(name)
        if st.item.kind == "text" or fv != "f1" or name == "route":
            out["f2"] = enc(st.x.get("f2"))
        if fv not in ("f1", "f2"):
            vec = st.x.get(fv)
            if vec is not None:
                out[fv] = enc(vec)
        return out

    def _threshold(self, reg, name, via, label=None):
        if via == "episodic":
            head = Episodes.head(self.p.episodes(name)) or {}
            return float(head.get("threshold") or 1.0) + adapt_for(head.get("adapt"), label)
        if via == "memory":
            return float(self.cfg["memory_similarity"])
        meta = reg.index.get(name) or {}
        return float(meta.get("threshold", 1.0)) + adapt_for(meta.get("adapt"), label)

    @staticmethod
    def _view(st, limit=VIEW_CHARS):
        import inputs
        try:
            return inputs.llm_view(st.item, max_chars=limit)[:limit]
        except Exception:
            return (st.item.text or "")[:limit]

    @staticmethod
    def _cid(reg, name):
        return champion_id(reg.index.get(name) or {}, reg.load(name))

    def step(self, budget_ms, routes=None):
        if not self.enabled:
            return []
        try:
            ev = step(self.root, self.policy, budget_ms=budget_ms, cat=self.cat, routes=routes)
        except Exception:
            ev = []
        self.events += ev
        return ev

    def notes(self):
        """Short summary-line notes about what changed in this request."""
        out = []
        for ev in self.events:
            if ev["event"] == "promoted":
                out.append("learned: %s v%s now answers locally" % (ev.get("label") or ev["preset"], ev.get("version")))
            elif ev["event"] == "paused":
                out.append("learned: %s local answers paused by spot checks" % ev["preset"])
            elif ev["event"] == "memory_on":
                out.append("learned: %s now recalls answers to inputs like earlier ones" % (ev.get("label") or ev["preset"]))
            elif ev["event"] == "ready":
                out.append("learned: a better %s model is ready (dev-decide promote %s)" % (ev["preset"], ev["preset"]))
        return out

    def meta(self):
        out = {}
        if self.new_labels:
            out["labels"] = self.new_labels
        if self.queued:
            out["spot_checks_queued"] = self.queued
        if self.spot:
            out["spot_checks"] = self.spot
        if self.events:
            out["events"] = [{k: v for k, v in ev.items() if k in ("preset", "label", "event", "version", "why")}
                             for ev in self.events]
        return out


# ---------------------------------------------------------------- commands: fix, status, improve, rollback, promote, forget

USAGE = ("dev-decide learning commands:\n"
         "  fix <decision-id|last> key=value [key=value ...] [item=ID]   correct (or confirm with key=ok) an answer\n"
         "  status [preset]                                           what has been learned and how well it does\n"
         "  improve [checks=N]                                        learn from everything pending; run spot checks\n"
         "  rollback <preset> [version]                               undo the last switch for a decision type\n"
         "  promote <preset>                                          switch to a better model that is waiting\n"
         "  forget recent|memory [preset]|queue|all                   delete kept text and corrections")

COMMANDS = ("fix", "status", "improve", "rollback", "promote", "forget", "learning")


def is_command(text):
    head = (text or "").strip().split(None, 1)
    return bool(head) and head[0].lower() in COMMANDS and "::" not in text


def command(text, cwd=None):
    root = dd.project_root(cwd)
    policy = dd.load_policy(root)
    cfg = config(policy)
    parts = (text or "").strip().split()
    cmd, args = parts[0].lower(), parts[1:]
    p = Paths(root)
    if not cfg["enabled"] and cmd not in ("status", "learning", "forget"):
        return _result("learning is turned off in .claude/dev-decide.json (\"learning\": {\"enabled\": false})")
    try:
        if cmd == "fix":
            return cmd_fix(p, policy, cfg, args)
        if cmd in ("status", "learning"):
            return cmd_status(p, policy, cfg, args)
        if cmd == "improve":
            return cmd_improve(p, policy, cfg, args)
        if cmd == "rollback":
            return cmd_rollback(p, policy, cfg, args)
        if cmd == "promote":
            return cmd_promote(p, policy, cfg, args)
        if cmd == "forget":
            return cmd_forget(p, cfg, args)
    except LearnError as exc:
        return _result(str(exc), error=True)
    return _result(USAGE)


class LearnError(Exception):
    pass


def feedback(cwd, did, answers, item=None):
    """Corrections from an app (the HTTP API): the same as `fix`, without command-line quoting."""
    root = dd.project_root(cwd)
    policy = dd.load_policy(root)
    cfg = config(policy)
    if not cfg["enabled"]:
        return _result("learning is turned off in .claude/dev-decide.json", error=True)
    args = [str(did or "last")] + ["%s=%s" % (k, v) for k, v in answers.items()]
    if item is not None:
        args.append("item=%s" % item)
    try:
        return cmd_fix(Paths(root), policy, cfg, args)
    except LearnError as exc:
        return _result(str(exc), error=True)


def _result(summary, error=False, **extra):
    out = {"summary": "dev-decide learning | " + summary, "v": dd.VERSION}
    if error:
        out["error"] = summary
    out.update(extra)
    return out


def _parse_value(v):
    low = v.strip().lower()
    if low in ("true", "yes", "y"):
        return "true"
    if low in ("false", "no", "n"):
        return "false"
    return v.strip()


def cmd_fix(p, policy, cfg, args):
    if not args:
        raise LearnError("usage: fix last issue_type=bug   (or key=ok to confirm an answer)")
    did, pairs, item = args[0], [], None
    for a in args[1:]:
        if "=" not in a:
            raise LearnError("expected key=value, got %r" % a)
        k, _, v = a.partition("=")
        if k in ("item", "--item"):
            item = v
        else:
            pairs.append((k.strip(), _parse_value(v)))
    if not pairs:
        raise LearnError("nothing to fix: give key=value pairs, for example issue_type=bug")
    p.ensure()
    rec = find_recent(p, cfg, did)
    if rec is None:
        raise LearnError("decision %s is not among the last %d decisions of the past %d hours, so its input is gone; "
                         "nothing was learned" % (did, int(cfg["recent"]), int(cfg["recent_hours"])))
    items = rec.get("items") or []
    it = next((i for i in items if str(i.get("item")) == str(item)), None) if item is not None else (items[0] if items else None)
    if it is None:
        raise LearnError("item %s is not in decision %s" % (item, rec.get("id")))
    cat = dd.load_catalog(policy)
    autos = dd.read_json(p.auto, default={}) or {}
    mem = Memory.get(p.memory)
    now = int(time.time())
    labels, saved, notes = [], [], []
    for key, val in pairs:
        info = (it.get("keys") or {}).get(key)
        if info is None:
            raise LearnError("decision %s has no answer %r (it has: %s)" % (rec.get("id"), key, ", ".join(sorted(it.get("keys") or {}))))
        name, kind = info["p"], info["k"]
        kind2, spec = preset_spec(cat, autos, name)
        if kind2 is None:
            raise LearnError("unknown decision type %s" % name)
        y = info.get("a") if val.lower() in ("ok", "right", "correct") else val
        if y is None:
            raise LearnError("%s had no answer to confirm" % key)
        y = str(y)
        valid = _valid_labels(kind2, spec, name, p)
        if valid is not None and y not in valid:
            raise LearnError("%s is not a valid %s (choose from: %s)" % (y, key, ", ".join(valid)))
        labels.append({"t": now, "id": rec.get("id"), "item": it.get("item"), "key": key, "preset": name, "kind": kind2,
                       "src": "fix", "y": y, "q": {y: 1.0}, "conf": 1.0, "x": it.get("x"),
                       "was": {"via": info.get("via"), "answer": info.get("a")}})
        changed = y != str(info.get("a"))
        saved.append("%s %s %s" % (key, "->" if changed else "=", y))
        if cfg["memory"] and it.get("kind") == "text" and (it.get("x") or {}).get("f2") and changed:
            mem.add({"t": now, "id": rec.get("id"), "preset": name, "key": key, "kind": kind2, "y": y,
                     "x": it["x"]["f2"], "text": it.get("text") if cfg["keep_text"] else None}, int(cfg["memory_max"]))
            notes.append("remembered for similar %s inputs" % key)
    _append(p.labels, labels)
    _append(p.base / "labels.jsonl", [{"id": rec.get("id"), "item": it.get("item"), "key": l["key"], "truth": l["y"],
                                       "ts": now} for l in labels])  # also read by calibrate.py and train.py
    events = step(p.root, policy, budget_ms=None)
    msg = "saved %s for %s" % (", ".join(saved), rec.get("id"))
    if notes:
        msg += "; " + "; ".join(sorted(set(notes)))
    if events:
        msg += "; " + "; ".join("%s %s" % (e["preset"], e["event"]) for e in events)
    return _result(msg, labels=len(labels), events=events)


def _valid_labels(kind, spec, name, p):
    if kind == "verify":
        return ["false", "true"]
    if kind == "score":
        return [str(i) for i in range(int(spec.get("min", 1)), int(spec.get("max", 5)) + 1)]
    opts = spec.get("options")
    if opts == "auto":
        return None  # any agent or skill name
    out = [str(o) for o in opts or []]
    if spec.get("allow_none"):
        out.append("none")
    return out


def cmd_improve(p, policy, cfg, args):
    n_checks = None
    for a in args:
        if a.startswith("checks=") or a.startswith("audits="):
            try:
                n_checks = max(0, int(a.split("=", 1)[1]))
            except ValueError:
                raise LearnError("checks must be a number")
    p.ensure()
    msgs = []
    spent = {}
    if (n_checks is None or n_checks > 0) and float(cfg["audit_rate"]) > 0:
        total = 0
        while True:
            picked = claim(p, cfg, n=min(8, n_checks - total) if n_checks is not None else 8)
            if not picked:
                break
            got, info = run_spot_checks(p.root, policy, picked)
            total += len(picked)
            spent["calls"] = spent.get("calls", 0) + int(info.get("calls", 0) or 0)
            if info.get("cost_usd") is not None:
                spent["cost_usd"] = round(spent.get("cost_usd", 0.0) + info["cost_usd"], 5)
            if info.get("error"):
                msgs.append("spot checks stopped: %s" % info["error"])
                break
            if n_checks is not None and total >= n_checks:
                break
        if total:
            msgs.append("%d spot check(s)" % total + (" ($%.4f)" % spent["cost_usd"] if "cost_usd" in spent else ""))
    events = step(p.root, policy, budget_ms=None, force_check=True)
    for e in events:
        msgs.append("%s %s" % (e["preset"], e["event"]))
    st = cmd_status(p, policy, cfg, [])
    msgs.append(st["summary"].split(" | ", 1)[1])
    return _result("; ".join(msgs), events=events, spot_checks=spent, learning=st.get("learning"))


def cmd_rollback(p, policy, cfg, args):
    if not args:
        raise LearnError("usage: rollback <preset> [version]")
    preset = args[0]
    with Lock(p.lock, wait=2.0) as lk:
        if not lk.ok:
            raise LearnError("busy: another dev-decide process is learning; try again")
        n, err = rollback(p, preset, args[1] if len(args) > 1 else None)
        if err:
            raise LearnError(err)
        state = load_state(p)
        ps = preset_state(state, preset)
        ps["window"], ps["audits"], ps["since_check"], ps["base"] = [], [], 0, None
        ps["hold"] = int(cfg["hold_after_rollback"])
        ps.pop("ready", None)
        _write_json(p.state, state)
        for ext in (".json", ".bin"):
            try:
                os.unlink(str(p.challenger(preset)) + ext)
            except OSError:
                pass
        _event(p, {"preset": preset, "event": "rolled_back", "version": n,
                   "why": "restored archived version %s; switching again needs %d new labels" % (n, ps["hold"])})
    return _result("%s restored to archived version %s; it can switch again after %d new labels"
                   % (preset, n, int(cfg["hold_after_rollback"])))


def cmd_promote(p, policy, cfg, args):
    if not args:
        raise LearnError("usage: promote <preset>")
    preset = args[0]
    local = dict(policy, learning=dict(cfg, auto_promote=True))
    state = load_state(p)
    ps = state["presets"].get(preset) or {}
    if not ps.get("ready") and cfg["auto_promote"]:
        raise LearnError("no better %s model is waiting; switches happen on their own when one wins" % preset)
    events = step(p.root, local, budget_ms=None, force_check=True)
    done = [e for e in events if e["preset"] == preset and e["event"] == "promoted"]
    if not done:
        raise LearnError("%s: the candidate no longer beats the current model on your latest decisions" % preset)
    return _result(done[0]["why"], events=events)


def cmd_forget(p, cfg, args):
    what = (args[0] if args else "").lower()
    gone = []
    if what in ("recent", "text", "all"):
        if p.recent.exists():
            p.recent.unlink()
            gone.append("recent decisions")
    if what in ("queue", "text", "all"):
        if p.queue.exists():
            p.queue.unlink()
            gone.append("the spot-check queue")
    if what in ("memory", "all"):
        preset = args[1] if len(args) > 1 and what == "memory" else None
        if p.memory.exists():
            if preset:
                keep = [e for e in _read_lines(p.memory) if e.get("preset") != preset]
                _write_text(p.memory, "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in keep))
                gone.append("remembered corrections for %s" % preset)
            else:
                p.memory.unlink()
                gone.append("all remembered corrections")
    if what == "text" and p.memory.exists():
        rows = _read_lines(p.memory)
        for r in rows:
            r["text"] = None
        _write_text(p.memory, "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in rows))
        gone.append("the text of remembered corrections (their hashed form stays)")
    if not gone:
        raise LearnError("usage: forget recent | queue | text | memory [preset] | all")
    return _result("deleted " + ", ".join(gone))


def cmd_status(p, policy, cfg, args):
    state = load_state(p)
    index = _index(p)
    autos = dd.read_json(p.auto, default={}) or {}
    mem = Memory.get(p.memory) if p.memory.exists() else None
    only = args[0] if args else None
    rows = []
    for preset in sorted(set(list(state["presets"]) + [k for k, v in index.items() if v.get("enabled")])):
        if only and preset != only:
            continue
        ps = state["presets"].get(preset) or {}
        meta = index.get(preset) or {}
        aud = [a for a in ps.get("audits") or [] if a[1] == "learned"]
        last = ps.get("last_check") or {}
        rows.append({
            "preset": preset, "title": (autos.get(preset) or {}).get("title"),
            "local": bool(meta.get("enabled")), "model": "%s v%s" % (meta.get("source", "-"), meta.get("version", "-")) if meta else "none",
            "threshold": meta.get("threshold"), "labels": ps.get("labels", {}),
            "spot_checks": {"n": len(aud), "correct": sum(1 for a in aud if a[2])},
            "challenger": {"window": len(ps.get("window") or []), "acc_challenger": last.get("acc_challenger"),
                           "acc_champion": last.get("acc_champion"), "coverage_challenger": last.get("coverage_challenger"),
                           "coverage_champion": last.get("coverage_champion")},
            "ready": bool(ps.get("ready")), "hold": ps.get("hold", 0), "reason": meta.get("reason"),
            "memory": sum(1 for e in (mem.entries if mem else []) if e.get("preset") == preset),
            "recall": {k: v for k, v in (Episodes.head(p.episodes(preset)) or {}).items()
                       if k in ("n", "threshold", "adapt", "stats")},
            "featurizer": meta.get("featurizer"), "adapt": meta.get("adapt") or {}, "alarm": bool(meta.get("alarm")),
        })
    history = _read_lines(p.history)[-10:]
    q = _queue(p)
    total = {"fix": 0, "audit": 0, "llm": 0}
    for r in rows:
        for k, v in (r["labels"] or {}).items():
            total[k] = total.get(k, 0) + v
    local = [display(r["preset"], autos) for r in rows if r["local"]]
    recalling = [display(r["preset"], autos) for r in rows if (r.get("recall") or {}).get("threshold") is not None]
    alarms = [display(r["preset"], autos) for r in rows if r.get("alarm")]
    spot = (state.get("audit") or {})
    checks = sum(r["spot_checks"]["n"] for r in rows)
    right = sum(r["spot_checks"]["correct"] for r in rows)
    shown = ", ".join(local[:6]) + (", +%d more" % (len(local) - 6) if len(local) > 6 else "")
    parts = ["%d local model%s%s" % (len(local), "" if len(local) == 1 else "s", (" (%s)" % shown) if local else ""),
             "%d labels (%d yours, %d spot checks, %d from Claude)" % (sum(total.values()), total.get("fix", 0),
                                                                    total.get("audit", 0), total.get("llm", 0))]
    if recalling:
        parts.append("recall on for %s" % ", ".join(recalling[:4]))
    if checks:
        parts.append("spot checks %d/%d right" % (right, checks))
    if alarms:
        parts.append("checking more often: %s" % ", ".join(alarms[:3]))
    esc = state.get("escalate") or {}
    if esc.get("day") == _today() and esc.get("n"):
        parts.append("%d stronger-model call(s) today" % esc["n"])
    parts.append("%d spot check(s) today, %d queued" % (spot.get("sent", 0) if spot.get("day") == _today() else 0, len(q)))
    if mem and mem.entries:
        parts.append("%d remembered correction(s)" % len(mem.entries))
    switched = [h for h in history if h.get("event") in ("promoted", "paused", "rolled_back")]
    if switched:
        h = switched[-1]
        parts.append("last change: %s %s" % (h.get("label") or display(h.get("preset"), autos), h.get("event").replace("_", " ")))
    return _result(" | ".join(parts), learning={"presets": rows, "history": history, "enabled": cfg["enabled"],
                                                 "spot_checks_today": spot, "queued": len(q)})
