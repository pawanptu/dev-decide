"""The learned fast model ("System One"): hashed features + a linear model, pure Python.

One small model per preset. Models come from two places:
  - dev-decide's own train.py, which learns from logged decisions (featurizer f1, JSON format)
  - dev-decide-kit, which trains and calibrates them with scikit-learn on bootstrapped
    data and exports them here (featurizer f1 or f2, compact binary format)

A model only answers above the confidence threshold it proved on held-out data;
below it the engine falls through to the next tier.

Featurizers (identical code at training and at inference, so they cannot drift):
  f1  words, camelCase-split, bigrams, file names, change status, local-analyzer signals
  f2  f1 plus character 3- and 4-grams of each word (catches migrate / migration / migrations)
  f3  f2 plus the text's meaning: a 256-number sentence embedding (semantic.py) at half weight
  f4  f2 plus the meaning at full weight
f3 and f4 need the bundled embedding model; without it, models that use them simply do not answer.

Features are hashed (crc32 into 2^20 buckets), so what is stored for training is a list
of numbers, never your code or prompts.
"""
import functools
import json
import math
import os
import random
import re
import sys
import zlib
from array import array
from collections import Counter
from pathlib import Path

import ddcore as dd

FEATURIZER = "f1"  # the featurizer used when logging decisions for dev-decide's own train.py
BUCKETS = 1 << 20
MAX_TOKENS = 20000
MAX_FEATURES = 1200
F2_MAX_TOKENS = 1500
F2_MAX_FEATURES = 3000
CHAR_WEIGHT = 0.25
BIN_MAGIC = b"DDM1"
WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def _h(feature):
    return zlib.crc32(feature.encode("utf-8", "replace")) % BUCKETS


def text_tokens(text, limit=MAX_TOKENS):
    out = []
    for w in WORD.findall(text or ""):
        w = w.lower()
        if 2 <= len(w) <= 30:
            out.append(w)
            if len(out) >= limit:
                break
    return out


def _word_counts(item, facts):
    """Weighted feature counts shared by f1 and f2, plus the token list."""
    counts = {}

    def add(feature, weight=1.0):
        k = _h(feature)
        counts[k] = counts.get(k, 0.0) + weight

    parts = [item.text or "", item.commit_message or ""]
    for f in item.files:
        parts.append(f.path.replace("/", " ").replace("_", " ").replace(".", " "))
        if not f.binary:
            parts.append("\n".join(f.added[:2000]))
            parts.append("\n".join(f.removed[:1000]))
    toks = text_tokens("\n".join(parts))
    prev = None
    for t in toks:
        add("w:" + t)
        if prev:
            add("b:%s_%s" % (prev, t), 0.5)
        prev = t
    for f in item.files:
        add("p:" + f.path.rsplit("/", 1)[-1].lower(), 2.0)
        add("st:" + f.status)
    for s in facts.sigs:
        add("s:" + s, 3.0)
    add("kind:" + item.kind, 2.0)
    return counts, toks


def _finish(vec, max_features):
    if len(vec) > max_features:
        # Ties are broken by bucket id so every process keeps the same features.
        vec = dict(sorted(vec.items(), key=lambda kv: (-kv[1], kv[0]))[:max_features])
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {k: round(v / norm, 4) for k, v in vec.items()}


def featurize(item, facts):
    """f1: sparse, L2-normalized feature vector {bucket: value} for one item."""
    counts, _ = _word_counts(item, facts)
    return _finish({k: 1.0 + math.log(c) for k, c in counts.items()}, MAX_FEATURES)


@functools.lru_cache(maxsize=1 << 16)
def _char_buckets(tok):
    s = " " + tok + " "
    out = []
    for n in (3, 4):
        for i in range(len(s) - n + 1):
            out.append(zlib.crc32(("c:" + s[i:i + n]).encode("utf-8", "replace")) % BUCKETS)
    return tuple(out)


def featurize_f2(item, facts):
    """f2: f1 plus character 3- and 4-grams of each word, weighted below whole words."""
    counts, toks = _word_counts(item, facts)
    vec = {k: 1.0 + math.log(c) for k, c in counts.items()}
    chars = {}
    for tok, n in Counter(toks[:F2_MAX_TOKENS]).items():
        for b in _char_buckets(tok):
            chars[b] = chars.get(b, 0) + n
    for b, n in chars.items():
        vec[b] = vec.get(b, 0.0) + CHAR_WEIGHT * (1.0 + math.log(n))
    return _finish(vec, F2_MAX_FEATURES)


SEM_BASE = BUCKETS  # meaning dimensions use ids BUCKETS .. BUCKETS + 255, outside the hashed range
SEM_WEIGHT = {"f3": 0.5, "f4": 1.0}
SEM_MAX_CHARS = 3000


def semantic_text(item):
    """The text whose meaning is embedded: free text and commit message, then file paths and added lines."""
    parts = [item.text or "", item.commit_message or ""]
    for f in item.files[:40]:
        parts.append(f.path.replace("/", " ").replace("_", " "))
        if not f.binary:
            parts.extend(f.added[:60])
        if sum(len(p) for p in parts) > SEM_MAX_CHARS:
            break
    return "\n".join(p for p in parts if p)[:SEM_MAX_CHARS]


def meaning(item):
    """The item's sentence embedding (a tuple of 256 floats), or None without a model or known words."""
    try:
        import semantic
    except ImportError:
        return None
    m = semantic.get()
    if m is None:
        return None
    return m.encode(semantic_text(item))


def with_meaning(base, emb, weight):
    out = dict(base)
    if emb is not None:
        for j, v in enumerate(emb):
            out[SEM_BASE + j] = round(weight * v, 4)
    return out


def featurize_f3(item, facts):
    emb = meaning(item)
    return None if emb is None and _no_semantic() else with_meaning(featurize_f2(item, facts), emb, SEM_WEIGHT["f3"])


def featurize_f4(item, facts):
    emb = meaning(item)
    return None if emb is None and _no_semantic() else with_meaning(featurize_f2(item, facts), emb, SEM_WEIGHT["f4"])


def _no_semantic():
    try:
        import semantic
        return semantic.get() is None
    except ImportError:
        return True


FEATURIZERS = {"f1": featurize, "f2": featurize_f2, "f3": featurize_f3, "f4": featurize_f4}


class Features(object):
    """Per-item lazy cache: each featurizer runs at most once, and only if a model needs it."""

    __slots__ = ("item", "facts", "_cache")

    def __init__(self, item, facts):
        self.item, self.facts, self._cache = item, facts, {}

    def get(self, name="f1"):
        if name not in self._cache:
            if name in SEM_WEIGHT:  # f2 plus meaning: reuse f2 and the embedding
                if "_meaning" not in self._cache:
                    self._cache["_meaning"] = meaning(self.item)
                emb = self._cache["_meaning"]
                self._cache[name] = None if emb is None and _no_semantic() else \
                    with_meaning(self.get("f2"), emb, SEM_WEIGHT[name])
                return self._cache[name]
            fn = FEATURIZERS.get(name)
            if fn is None:
                return None
            self._cache[name] = fn(self.item, self.facts)
        return self._cache[name]

    def meaning(self):
        if "_meaning" not in self._cache:
            self._cache["_meaning"] = meaning(self.item)
        return self._cache["_meaning"]

    def items(self):  # the f1 vector, for decision logging
        return self.get("f1").items()


# ---------------------------------------------------------------- model

def classes_for(kind, spec):
    if kind == "verify":
        return ["false", "true"]
    if kind == "score":
        return [str(i) for i in range(int(spec.get("min", 1)), int(spec.get("max", 5)) + 1)]
    opts = [str(o) for o in (spec.get("options") or [])]
    if spec.get("allow_none") and "none" not in opts:
        opts.append("none")
    return opts


def predict_scores(model, x):
    K = len(model["classes"])
    s = list(model["bias"])
    idx = model.get("_idx")
    if idx is not None:  # compact binary format
        w = model["_w"]
        for f, v in x.items():
            r = idx.get(f)
            if r is not None:
                base = r * K
                s = [a + wi * v for a, wi in zip(s, w[base:base + K])]
        return s
    W = model["weights"]  # JSON format
    for f, v in x.items():
        w = W.get(f)
        if w is not None:
            for k in range(K):
                s[k] += w[k] * v
    return s


def predict(model, x):
    s = predict_scores(model, x)
    t = model.get("temperature", 1.0) or 1.0
    p = dd.softmax([v / t for v in s])
    return dict(zip(model["classes"], p))


def write_binary(path, buckets, weights, K):
    """Write a compact model: header, bucket ids (uint32), row-major weights (float32), little-endian."""
    head = array("I", [len(buckets), K])
    b = array("I", buckets)
    w = array("f", weights)
    if len(w) != len(b) * K:
        raise ValueError("weights do not match buckets x classes")
    if sys.byteorder != "little":
        for arr in (head, b, w):
            arr.byteswap()
    with open(path, "wb") as fh:
        fh.write(BIN_MAGIC)
        fh.write(head.tobytes())
        fh.write(b.tobytes())
        fh.write(w.tobytes())


def read_binary(path, K_expected):
    """Return (bucket -> row index, flat float32 weights). Loading 30k buckets takes a few ms."""
    data = Path(path).read_bytes()
    if data[:4] != BIN_MAGIC:
        raise ValueError("not a dev-decide model file")
    head = array("I")
    head.frombytes(data[4:12])
    if sys.byteorder != "little":
        head.byteswap()
    n, K = head[0], head[1]
    if K != K_expected or len(data) != 12 + 4 * n + 4 * n * K:
        raise ValueError("model file does not match its classes")
    b = array("I")
    b.frombytes(data[12:12 + 4 * n])
    w = array("f")
    w.frombytes(data[12 + 4 * n:])
    if sys.byteorder != "little":
        b.byteswap()
        w.byteswap()
    return dict(zip(b, range(n))), w


def train_softmax(X, Q, weights, K, epochs=8, lr=0.6, l2=1e-5, seed=7):
    W, b = {}, [0.0] * K
    order = list(range(len(X)))
    rnd = random.Random(seed)
    step = 0
    for _ in range(epochs):
        rnd.shuffle(order)
        for i in order:
            x, q, w8 = X[i], Q[i], weights[i]
            s = list(b)
            for f, v in x.items():
                wf = W.get(f)
                if wf is not None:
                    for k in range(K):
                        s[k] += wf[k] * v
            p = dd.softmax(s)
            eta = lr / (1.0 + 0.002 * step)
            step += 1
            g = [(p[k] - q[k]) * w8 for k in range(K)]
            for k in range(K):
                b[k] -= eta * g[k]
            for f, v in x.items():
                wf = W.get(f)
                if wf is None:
                    wf = W[f] = [0.0] * K
                for k in range(K):
                    wf[k] -= eta * (g[k] * v + l2 * wf[k])
    return W, b


# ---------------------------------------------------------------- registry used by the engine

# Loaded models and indexes are kept for the life of the process (the HTTP API, a test run) and
# reused while their files are unchanged, so a warm process never re-reads a model per request.
_LOADED = {}  # file path -> (file signature, parsed index or model); treated as read-only


def _signature(*paths):
    sig = []
    for p in paths:
        try:
            st = os.stat(str(p))
            sig.append((st.st_mtime_ns, st.st_size, st.st_ino))
        except OSError:
            sig.append(None)
    return tuple(sig)


def seed_models(root):
    """First use in a repository: copy the starter models shipped with the skill (skill/starter-models/).

    Only when the repository has no models yet, so what it has learned or trained is never overwritten.
    """
    src = dd.SKILL_DIR / "starter-models"
    if not (src / "index.json").is_file():
        return
    try:
        import shutil
        dest = dd.state_dir(root) / "models"  # state_dir also writes the folder's .gitignore
        dest.mkdir(parents=True, exist_ok=True)
        if (dest / "index.json").exists():
            return
        for f in sorted(src.iterdir()):
            if f.is_file() and f.name != "index.json":
                shutil.copy2(str(f), str(dest / f.name))
        shutil.copy2(str(src / "index.json"), str(dest / "index.json"))  # last: its presence means "complete"
    except OSError:
        pass


class Registry(object):
    """Loads only the enabled models for the presets a request asks about."""

    def __init__(self, root):
        self.dir = Path(root) / ".claude" / "dev-decide" / "models"
        ipath = self.dir / "index.json"
        sig = _signature(ipath)
        hit = _LOADED.get(str(ipath))
        if hit and hit[0] == sig:
            self.index = hit[1]
        else:
            self.index = dd.read_json(ipath) if self.dir.is_dir() else {}
            if sig[0] is not None:
                _LOADED[str(ipath)] = (sig, self.index)
        self._cache = {}

    def featurizer(self, preset):
        return (self.index.get(preset) or {}).get("featurizer") or FEATURIZER

    def enabled(self, preset):
        meta = self.index.get(preset) or {}
        return bool(meta.get("enabled")) and meta.get("featurizer", FEATURIZER) in FEATURIZERS

    def any_enabled(self, presets):
        return any(self.enabled(p) for p in presets)

    def load(self, preset):
        if preset not in self._cache:
            jpath, bpath = self.dir / ("%s.json" % preset), self.dir / ("%s.bin" % preset)
            sig = _signature(jpath, bpath)
            hit = _LOADED.get(str(jpath))
            if hit and hit[0] == sig:
                self._cache[preset] = hit[1]
                return hit[1]
            data = dd.read_json(jpath, default=None)
            try:
                if data and data.get("format") == "bin1":
                    data["_idx"], data["_w"] = read_binary(bpath, len(data["classes"]))
                elif data and data.get("weights") is not None:
                    data["weights"] = {int(k): v for k, v in data["weights"].items()}
                else:
                    data = None
            except (OSError, ValueError, KeyError):
                data = None
            self._cache[preset] = data
            if data is not None:
                _LOADED[str(jpath)] = (sig, data)
        return self._cache[preset]

    def answer(self, preset, kind, x):
        """Return a raw answer dict if the model is confident enough, else None.

        x is a Features object (preferred) or an f1 vector.
        """
        if not self.enabled(preset):
            return None
        model = self.load(preset)
        if not model:
            return None
        name = model.get("featurizer") or self.featurizer(preset)
        vec = x.get(name) if isinstance(x, Features) else (x if name == "f1" else None)
        if vec is None:
            return None
        probs = predict(model, vec)
        best = max(probs, key=probs.get)
        adapt = (self.index.get(preset) or {}).get("adapt")  # per-class offsets raised by spot-check errors (learn.py)
        if isinstance(adapt, dict):
            adapt = adapt.get(str(best), 0.0)
        try:
            adapt = float(adapt or 0.0)
        except (TypeError, ValueError):
            adapt = 0.0
        if probs[best] < float(model.get("threshold", 0.95)) + adapt:
            return None
        if kind == "verify":
            return {"type": "verify", "p_true": round(probs.get("true", 0.0), 4)}
        return {"type": kind, "probs": {k: round(v, 4) for k, v in probs.items() if v >= 0.005}}


def save_model(root, preset, model, meta):
    d = Path(root) / ".claude" / "dev-decide" / "models"
    d.mkdir(parents=True, exist_ok=True)
    data = dict(model)
    data["weights"] = {str(k): v for k, v in model["weights"].items()}
    dd.write_json(d / ("%s.json" % preset), data)
    index = dd.read_json(d / "index.json")
    index[preset] = meta
    dd.write_json(d / "index.json", index, indent=2)
