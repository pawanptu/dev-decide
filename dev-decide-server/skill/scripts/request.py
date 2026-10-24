"""Parse a dev-decide request into questions, flags and input items.

Text form:   <presets or bundles> [+flags] :: <input>
Ad-hoc:      choose a|b|c :: <input>    score 1-5 <what> :: <input>    verify <statement> :: <input>
JSON form:   {"questions": [...] or {...}, "state": "..."} or {"items": [{"id": "...", "state": "..."}], ...}
"""
import json
import re

import ddcore as dd

FLAGS = {"why", "local", "fast", "llm", "deep", "json", "nocache"}
INPUT_START = re.compile(r"(?<!\S)(DIFF\b|COMMIT\s+\S|@\S)")


class Question(object):
    __slots__ = ("key", "kind", "preset", "spec")

    def __init__(self, key, kind, preset=None, spec=None):
        self.key, self.kind, self.preset, self.spec = key, kind, preset, dict(spec or {})


class Request(object):
    def __init__(self):
        self.questions = {}  # key -> Question, in asked order
        self.bundles = []
        self.flags = set()
        self.items = []      # list of (item_id or None, raw input text)
        self.diffs = {}      # item index -> unified diff sent inline ("diff" in a JSON request)
        self.error = None
        self.warnings = []


def _add_preset(req, cat, key, name, overrides=None):
    kind, spec = dd.find_preset(cat, name)
    if kind is None:
        return False
    if overrides:
        spec.update({k: v for k, v in overrides.items() if k in ("options", "allow_none", "anchors")})
    if key not in req.questions:
        req.questions[key] = Question(key, kind, name, spec)
    return True


def _add_name(req, cat, name):
    if name in cat["bundles"]:
        if name not in req.bundles:
            req.bundles.append(name)
        for member in cat["bundles"][name]:
            _add_preset(req, cat, member, member)
        return True
    return _add_preset(req, cat, name, name)


def _adhoc(req, text, key="q1"):
    m = re.match(r"^(choose|score|verify)\b\s*(.*)$", text, re.S)
    kind, rest = m.group(1), m.group(2).strip()
    if kind == "choose":
        sep = "|" if "|" in rest else ","
        opts = [o.strip() for o in rest.split(sep) if o.strip()]
        opts = [re.sub(r"\s+", "_", o) for o in opts]
        if len(opts) < 2:
            return "choose needs at least two options, like: choose web|api|worker :: <input>"
        spec = {"options": [o for o in opts if o != "none"], "allow_none": "none" in opts}
    elif kind == "score":
        sm = re.match(r"^(-?\d+)\s*-\s*(-?\d+)\s*(.*)$", rest, re.S)
        if not sm:
            return "score needs a range, like: score 1-5 readability :: <input>"
        lo, hi = int(sm.group(1)), int(sm.group(2))
        if hi <= lo or hi - lo > 20:
            return "score range must be ascending and at most 20 steps"
        spec = {"min": lo, "max": hi, "label": sm.group(3).strip()}
    else:
        if not rest:
            return "verify needs a statement, like: verify the fix handles empty input :: <input>"
        spec = {"statement": rest}
    req.questions[key] = Question(key, kind, None, spec)
    return None


def parse_text(text, cat):
    s = (text or "").strip()
    req = Request()
    if not s:
        req.error = "empty request. Example: /dev-decide pr :: DIFF"
        return req
    if s.startswith("{"):
        try:
            spec = json.loads(s)
        except ValueError:
            req.error = "the request looks like JSON but does not parse"
            return req
        return parse_spec(spec, cat)

    if "::" in s:
        head, tail = s.split("::", 1)
    else:
        m = INPUT_START.search(s)
        head, tail = (s[:m.start()], s[m.start():]) if m else (s, "")
    head, tail = head.strip(), tail.strip()

    words = head.split()
    for w in words:
        if w.startswith("+"):
            flag = w[1:].lower()
            if flag in FLAGS:
                req.flags.add(flag)
            else:
                req.error = "unknown flag +%s (known: %s)" % (flag, ", ".join("+" + f for f in sorted(FLAGS)))
                return req
    rest = " ".join(w for w in words if not w.startswith("+"))

    if re.match(r"^(choose|score|verify)\b", rest):
        err = _adhoc(req, rest)
        if err:
            req.error = err
            return req
    else:
        names = [n for n in re.split(r"[\s,]+", rest) if n]
        if not names:
            req.error = "no presets or bundles named. Example: /dev-decide pr :: DIFF"
            return req
        for n in names:
            if not _add_name(req, cat, n):
                req.error = "unknown preset or bundle: %s" % n
                return req
    req.items.append((None, tail))
    return req


def parse_spec(spec, cat):
    req = Request()
    if not isinstance(spec, dict):
        req.error = "the JSON request must be an object"
        return req
    qs = spec.get("questions")
    if isinstance(qs, str):
        qs = [q for q in re.split(r"[\s,]+", qs) if q]
    if isinstance(qs, list):
        for name in qs:
            if not isinstance(name, str) or not _add_name(req, cat, name):
                req.error = "unknown preset or bundle: %s" % name
                return req
    elif isinstance(qs, dict):
        for key, q in qs.items():
            if isinstance(q, str):
                if q in cat["bundles"]:
                    _add_name(req, cat, q)
                elif not _add_preset(req, cat, key, q):
                    req.error = "unknown preset: %s" % q
                    return req
            elif isinstance(q, dict):
                if q.get("preset"):
                    if not _add_preset(req, cat, key, q["preset"], overrides=q):
                        req.error = "unknown preset: %s" % q["preset"]
                        return req
                elif q.get("type") in ("choose", "score", "verify"):
                    kind = q["type"]
                    if kind == "choose":
                        opts = [str(o) for o in (q.get("options") or [])]
                        if len(opts) < 2:
                            req.error = "question %s needs at least two options" % key
                            return req
                        qspec = {"options": [o for o in opts if o != "none"],
                                 "allow_none": bool(q.get("allow_none")) or "none" in opts}
                    elif kind == "score":
                        try:
                            lo, hi = int(q.get("min", 1)), int(q.get("max", 5))
                        except (TypeError, ValueError):
                            req.error = "question %s has an invalid score range" % key
                            return req
                        if hi <= lo or hi - lo > 20:
                            req.error = "question %s has an invalid score range" % key
                            return req
                        qspec = {"min": lo, "max": hi, "anchors": q.get("anchors") or {},
                                 "label": q.get("label", "")}
                    else:
                        if not q.get("statement"):
                            req.error = "question %s needs a statement" % key
                            return req
                        qspec = {"statement": str(q["statement"])}
                    req.questions[key] = Question(key, kind, None, qspec)
                else:
                    req.error = "question %s needs a preset or a type" % key
                    return req
    else:
        req.error = "the JSON request needs \"questions\""
        return req
    if not req.questions:
        req.error = "no questions in the request"
        return req

    for f in spec.get("flags") or []:
        if str(f).lstrip("+") in FLAGS:
            req.flags.add(str(f).lstrip("+"))
    if spec.get("why"):
        req.flags.add("why")
    mode = str(spec.get("mode") or "").lower()
    if mode in ("local", "llm", "deep"):
        req.flags.add(mode)

    items = spec.get("items")
    if isinstance(items, list) and items:
        for i, it in enumerate(items, 1):
            if isinstance(it, dict):
                iid = str(it.get("id", i))
                req.items.append((iid, str(it.get("input") or it.get("state") or "")))
                _take_diff(req, it)
            else:
                req.items.append((str(i), str(it)))
    else:
        req.items.append((None, str(spec.get("input") or spec.get("state") or "")))
        _take_diff(req, spec)
    return req


def _take_diff(req, obj):
    """A unified diff sent inline, for callers that do not share this machine's git working tree."""
    diff = obj.get("diff")
    if isinstance(diff, str) and diff.strip():
        req.diffs[len(req.items) - 1] = diff
    elif diff not in (None, ""):
        req.warnings.append("ignored \"diff\": it must be a string holding a unified diff")
