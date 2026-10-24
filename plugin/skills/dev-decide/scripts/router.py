"""Zero-shot routing: match a request to agents and skills by their names and descriptions.

TF-IDF over route descriptions with light stemming, blended with two signals that need no shared words:
  meaning  the bundled sentence-embedding model (semantic.py) compares the request with each "name: description";
  areas    both are mapped to technical areas (postgres, column, schema -> database; css, button -> frontend) and
           a strong match of areas counts, so "rename a column in postgres" reaches "database schema changes".
Without the meaning model it is TF-IDF alone, as before. Runs in a few milliseconds, needs no training, and gets
replaced by the learned route model once one is trained. The weights were tuned on half of the kit's 351 labeled
routing requests and checked on the other half (tools/eval_router.py), with false hints for a single unrelated
skill kept under 5%.
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
# With meaning: score = LEX_WEIGHT * tfidf + SEM_WEIGHT * max(0, cosine - SEM_FLOOR)
#                      + AREA_WEIGHT * max(0, area_cosine - AREA_FLOOR) / (1 - AREA_FLOOR), against SEM_NONE_SCORE.
LEX_WEIGHT = 0.25
SEM_WEIGHT = 1.0
SEM_FLOOR = 0.1
AREA_WEIGHT = 0.4
AREA_FLOOR = 0.8
SEM_NONE_SCORE = 0.3
SEM_TEMPERATURE = 0.12
SEM_MAX_CHARS = 3000
# Everyday words for the areas agents and skills usually cover, added to the domain patterns in signals.py.
AREA_WORDS = {
    "database": r"\b(databases?|db|columns?|tables?|index(es)?|queries|query|rows?|postgresql|alembic|knex|typeorm|"
                r"sequelize|rollbacks?)\b",
    "frontend": r"\b(ui|pages?|buttons?|layout|styl\w+|css|modal|forms?|responsive|accessib\w+|a11y)\b",
    "testing_qa": r"\b(tests?|unit tests?|integration tests?|coverage|flaky|fixtures?|mocks?)\b",
    "docs": r"\b(readme|docs?|documentation|docstrings?|changelog|guide|tutorial)\b",
    "devops_infra": r"\b(ci|cd|pipelines?|deploy\w*|releases?|containers?|github actions|rollouts?)\b",
    "security": r"\b(auth\w*|vulnerab\w*|secrets?|tokens?|permissions?|injection|owasp|pen ?test)\b",
    "debugging": r"\b(crash\w*|errors?|exceptions?|stack ?traces?|tracebacks?|bugs?|failing|fails?|root cause|"
                 r"segfault|panic)\b",
    "backend": r"\b(api|endpoints?|services?|handlers?|requests?|latency|n\+1)\b",
}
_AREAS = None


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


def _meaning_model():
    try:
        import semantic
        return semantic.get()
    except Exception:
        return None


@functools.lru_cache(maxsize=1024)
def _route_meaning(name, description):
    """The embedding of one route's name and description (routes repeat on every request)."""
    m = _meaning_model()
    if m is None:
        return None
    return m.encode("%s: %s" % (re.sub(r"[-_:/.]+", " ", name), description or ""))


def _cosine(a, b):
    return sum(x * y for x, y in zip(a, b)) if a and b else 0.0  # both vectors are already unit length


def _area_patterns():
    global _AREAS
    if _AREAS is None:
        import signals
        pats = dict(signals.DOMAIN_SIGS)
        for area, extra in AREA_WORDS.items():
            pats[area] = (pats[area] + "|" + extra) if area in pats else extra
        _AREAS = {area: re.compile(p, re.I | re.M) for area, p in pats.items()}
    return _AREAS


def areas(text):
    """Technical areas a text is about, as a unit-length vector {area: weight} (matches per area capped at 3)."""
    v = {}
    for area, rx in _area_patterns().items():
        n = min(len(rx.findall(text or "")), 3)
        if n:
            v[area] = float(n)
    norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {k: x / norm for k, x in v.items()}


@functools.lru_cache(maxsize=1024)
def _route_areas(name, description):
    return areas("%s %s" % (re.sub(r"[-_:/.]+", " ", name), description or ""))


def _area_match(q_areas, r_areas):
    sim = sum(v * r_areas.get(k, 0.0) for k, v in q_areas.items())
    return max(0.0, sim - AREA_FLOOR) / (1.0 - AREA_FLOOR)


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
    none_score, temperature = NONE_SCORE, TEMPERATURE
    model = _meaning_model()
    qe = model.encode(query[:SEM_MAX_CHARS]) if model is not None else None
    if qe is not None:
        qa = areas(query[:SEM_MAX_CHARS])
        sims = [LEX_WEIGHT * s
                + SEM_WEIGHT * max(0.0, _cosine(qe, _route_meaning(r["name"], r.get("description") or "")) - SEM_FLOOR)
                + AREA_WEIGHT * _area_match(qa, _route_areas(r["name"], r.get("description") or ""))
                for s, r in zip(sims, routes)]
        none_score, temperature = SEM_NONE_SCORE, SEM_TEMPERATURE
    scored = list(zip([r["name"] for r in routes], sims))
    if allow_none:
        scored.append(("none", none_score))
    m = max(s for _, s in scored)
    ex = [(name, math.exp((s - m) / temperature)) for name, s in scored]
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
