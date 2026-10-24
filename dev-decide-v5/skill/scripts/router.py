"""Zero-shot routing: match a request to agents and skills by their names and descriptions.

TF-IDF over route descriptions with light stemming. Runs in a few milliseconds,
needs no training, and gets replaced by the learned route model once one is trained.
"""
import functools
import math
import re
from collections import Counter

STOP = set("""a an the and or of to in on for with without from into onto at by as is are was were be been being it its
this that these those there here i me my we our you your they them their he she his her can could should would will
shall may might must do does did done doing have has had having not no yes please help need want like just also so
then than when where which who whom what why how all any some each every other more most less very really
using use used uses via about over under after before again once only own same too up down out off
agent agents skill skills task tasks work thing things stuff get make let try""".split())
SUFFIXES = ("izations", "ization", "ational", "ations", "ation", "ments", "ment", "ingly", "ings", "ing", "edly",
            "ers", "er", "ies", "ied", "ed", "es", "s")
NONE_SCORE = 0.12
TEMPERATURE = 0.07


def stem(w):
    for suf in SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            base = w[:-len(suf)]
            if suf in ("ies", "ied"):
                base += "y"
            return base
    return w


def tokens(text):
    return list(_tokenize(text or ""))


def _tokenize(text):
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", text):
        w = w.lower()
        if w in STOP or len(w) < 2:
            continue
        out.append(stem(w))
    return tuple(out)


_description_tokens = functools.lru_cache(maxsize=1024)(_tokenize)  # route descriptions repeat on every request


def _vector(toks, idf):
    tf = Counter(toks)
    vec = {t: (1 + math.log(c)) * idf.get(t, 0.0) for t, c in tf.items()}
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {t: v / norm for t, v in vec.items()}


def _name_tokens(name):
    return [stem(p.lower()) for p in re.split(r"[-_:/.\s]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", name)) if p and p.lower() not in STOP]


def route(query, routes, allow_none=True):
    """Return (probs, exact, best_name) or None.

    exact is True when the request names a route explicitly ("use the db-migrate skill").
    """
    if not routes or not (query or "").strip():
        return None
    q_low = query.lower()
    explicit = [r["name"] for r in routes
                if len(r["name"]) >= 4 and re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(r["name"].lower()), q_low)]
    if explicit:
        best = max(explicit, key=len)
        probs = {best: 0.9, "none": 0.02, "_rest": 0.08} if allow_none else {best: 0.9, "_rest": 0.1}
        return probs, True, best

    docs = []
    for r in routes:
        name_toks = _name_tokens(r["name"])
        docs.append(name_toks * 2 + list(_description_tokens(r.get("description") or "")))
    n = len(docs)
    df = Counter(t for d in docs for t in set(d))
    idf = {t: math.log((n + 1.0) / (c + 0.5)) + 1.0 for t, c in df.items()}
    qv = _vector(tokens(query), idf)
    if not qv:
        return None
    sims = []
    for r, d in zip(routes, docs):
        dv = _vector(d, idf)
        sims.append(sum(v * dv.get(t, 0.0) for t, v in qv.items()))
    scored = list(zip([r["name"] for r in routes], sims))
    if allow_none:
        scored.append(("none", NONE_SCORE))
    m = max(s for _, s in scored)
    ex = [(name, math.exp((s - m) / TEMPERATURE)) for name, s in scored]
    z = sum(e for _, e in ex)
    probs = {name: e / z for name, e in ex}
    top = sorted(probs.items(), key=lambda kv: -kv[1])
    keep = dict(top[:5])
    rest = 1.0 - sum(keep.values())
    if rest > 0.001:
        keep["_rest"] = rest
    best = top[0][0]
    return keep, False, best


def best_route(query, routes, min_confidence=0.55, min_margin=0.15):
    """Convenience for the hook: (name, confidence) when the match is clear, else None."""
    res = route(query, routes)
    if res is None:
        return None
    probs, exact, best = res
    if best in ("none", "_rest"):
        return None
    ranked = sorted((v for k, v in probs.items() if k not in ("_rest",)), reverse=True)
    conf = ranked[0]
    margin = conf - (ranked[1] if len(ranked) > 1 else 0.0)
    if exact or (conf >= min_confidence and margin >= min_margin):
        return best, conf
    return None
