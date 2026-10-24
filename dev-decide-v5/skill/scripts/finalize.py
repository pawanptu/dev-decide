#!/usr/bin/env python3
"""Turn raw judgments into the final decision object.

Used as a library by engine.py, and still runnable as a script (v2 compatible):
reads the line format or a JSON object on stdin and prints the final JSON.

Everything that must be exact happens here: option validation, normalization,
calibration of model answers, project floors, confidence, abstain, next step,
gates and the decision log (predictions only, never the input text).
"""
import json
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ddcore as dd  # noqa: E402

ESCALATE_BAND = 0.15
MODEL_VIA = ("llm", "llm-deep")
ANSWER_KINDS = ("choose", "score", "verify", "error")


class AnswerError(Exception):
    pass


# ---------------------------------------------------------------- parsing

def extract_json(text):
    t = text.strip()
    t = re.sub(r"^```[A-Za-z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(t[start:end + 1])


def _num(s):
    """A probability written as 0.8, .8, 80% or 80 (percent), or None if it is not a number."""
    s = (s or "").strip().rstrip(",;)")
    pct = s.endswith("%")
    try:
        v = float(s.rstrip("%"))
    except ValueError:
        return None
    if pct or 2.0 <= v <= 100.0:
        v = v / 100.0
    elif 1.0 < v < 2.0:
        v = 1.0  # a slightly-over-1 probability is a rounding slip, not 1.5%
    return v


def parse_lines(text, known_keys=None, strict=True, kinds=None):
    """Parse the line format. With known_keys, lines for other keys and chatter are skipped.

    kinds maps key -> choose/score/verify, so a line that leaves out the kind word
    ("route db-migrate") can still be read.
    """
    root = {"answers": {}}
    cur, items = root, []
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("`").strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        head, _, rest = line.partition(" ")
        rest = rest.strip()
        if head == "item":
            item_id, _, tail = rest.partition(" ")
            cur = {"id": item_id, "answers": {}}
            items.append(cur)
            if tail.strip().startswith("error"):
                cur["error"] = tail.strip()[5:].strip() or "error"
            continue
        if head == "files":
            cur["files"] = rest.split()
            continue
        if head == "bundles":
            cur["bundles"] = [b for b in re.split(r"[\s,]+", rest) if b]
            continue
        if head == "error":
            cur["error"] = rest or "error"
            continue
        if head == "why":
            key, _, txt = rest.partition(" ")
            if key in cur["answers"] and txt.strip():
                cur["answers"][key].setdefault("evidence", []).append(txt.strip())
            continue
        key = head.rstrip(":")
        kind, _, args = rest.partition(" ")
        kind = kind.rstrip(":").lower()
        if known_keys is not None and key not in known_keys and key.lower() in known_keys:
            key = key.lower()
        if kind not in ANSWER_KINDS and kinds and key in kinds:
            kind, args = kinds[key], rest  # the model left out the kind word
        if kind not in ANSWER_KINDS or (known_keys is not None and key not in known_keys):
            if strict:
                raise ValueError("unreadable line: %s" % line[:80])
            continue
        if kind == "error":
            cur["answers"][key] = {"error": args.strip() or "error"}
            continue
        ans, probs, bad = {"type": kind}, {}, 0
        for tok in args.split():
            tok = tok.strip(",;()")
            if not tok:
                continue
            if tok.startswith("@"):
                name, _, val = tok[1:].partition("=")
                try:
                    if name == "options":
                        ans["options"] = [o for o in val.split(",") if o]
                    elif name in ("min", "max"):
                        ans[name] = int(val)
                    elif name == "none":
                        ans["allow_none"] = True
                    elif name == "preset":
                        ans["preset"] = val
                except ValueError:
                    bad += 1
                continue
            if kind == "verify":
                if tok.lower() in ("true", "yes", "false", "no") and "p_true" not in ans:
                    ans["p_true"] = 0.8 if tok.lower() in ("true", "yes") else 0.2  # bare verdict, no probability
                    continue
                name, eq, val = tok.partition("=")
                if eq and name.lower() not in ("p", "p_true", "true", "yes", "prob", "probability"):
                    continue
                v = _num(val if eq else tok)
                if v is not None and "p_true" not in ans:
                    ans["p_true"] = v
                elif v is None:
                    bad += 1
                continue
            option, eq, value = tok.rpartition("=")
            if option:
                v = _num(value)
                if v is None:
                    bad += 1
                else:
                    probs[option] = probs.get(option, 0.0) + v
            elif not probs and not eq and re.match(r"^[\w.-]+$", tok):
                probs[tok] = 1.0  # a bare choice such as "task_type choose feature"
                ans["bare"] = True
        if strict and bad and not probs and "p_true" not in ans:
            raise ValueError("unreadable values in line: %s" % line[:80])
        if kind != "verify":
            if not probs:
                continue
            ans["probs"] = probs
        elif "p_true" not in ans:
            continue
        cur["answers"][key] = ans
    if items:
        for it in items:
            it.setdefault("files", root.get("files", []))
            it.setdefault("bundles", root.get("bundles", []))
        return {"items": items}
    return root


def parse_input(text):
    t = text.strip()
    if t.startswith("{") or t.startswith("```"):
        return extract_json(t)
    return parse_lines(t)


# ---------------------------------------------------------------- answer types

def round_probs(probs, drop_zero=False):
    r = {k: round(v, 2) for k, v in probs.items()}
    diff = round(1.0 - sum(r.values()), 2)
    if r and abs(diff) >= 0.01:
        top = max(r, key=r.get)
        r[top] = round(r[top] + diff, 2)
    if drop_zero:
        r = {k: v for k, v in r.items() if v > 0}
    return dict(sorted(r.items(), key=lambda kv: -kv[1]))


def norm(probs):
    try:
        return dd.normalize(probs)
    except ValueError as exc:
        raise AnswerError(str(exc))


def do_choose(key, a, spec, ctx, temp, warnings):
    opts = spec.get("options")
    if opts == "auto":
        opts = a.get("options") or [r["name"] for r in ctx.get("routes", [])]
    elif opts is None:
        opts = a.get("options")
    if not opts or not isinstance(opts, list):
        raise AnswerError("no options to choose from")
    allow_none = bool(spec.get("allow_none", a.get("allow_none", False)))
    valid = set(str(o) for o in opts)
    if allow_none:
        valid.add("none")
    probs_in = a.get("probs")
    if not probs_in and a.get("choice") is not None:
        probs_in = {str(a["choice"]): 1.0}
    raw = norm(probs_in)
    kept, dropped = {}, 0.0
    for k, v in raw.items():
        if k in valid or k == "_rest":
            kept[k] = kept.get(k, 0.0) + v
        else:
            dropped += v
            warnings.append("%s: dropped unknown option '%s'" % (key, k))
    if dropped:
        kept["_rest"] = kept.get("_rest", 0.0) + dropped
    if not any(v > 0 for k, v in kept.items() if k != "_rest"):
        raise AnswerError("no valid option chosen")
    if a.get("bare") and len([k for k in kept if k != "_rest"]) == 1:
        only = next(k for k in kept if k != "_rest")
        kept = {only: 0.75, "_rest": 0.25}  # a bare answer carries no probability: assume moderate confidence
    cal = dd.temper(kept, temp)
    choice = max((k for k in cal if k != "_rest"), key=cal.get)
    conf = cal[choice]
    second = max([v for k, v in cal.items() if k != choice] or [0.0])
    res = {"choice": choice, "probs": round_probs(cal, drop_zero=True),
           "confidence": round(conf, 2), "margin": round(conf - second, 2)}
    pred_raw = max((k for k in kept if k != "_rest"), key=kept.get)
    return res, kept, pred_raw


def do_score(key, a, spec, ctx, temp, warnings, floor):
    try:
        lo = int(spec.get("min", a.get("min", 1)))
        hi = int(spec.get("max", a.get("max", 5)))
    except (TypeError, ValueError):
        raise AnswerError("invalid score range")
    if hi <= lo or hi - lo > 20:
        raise AnswerError("invalid score range")
    probs_in = a.get("probs") or {}
    if not probs_in and a.get("score") is not None:
        try:
            probs_in = {str(int(round(min(max(float(a["score"]), lo), hi)))): 1.0}
        except (TypeError, ValueError):
            raise AnswerError("invalid score")
    grid = {str(i): 0.0 for i in range(lo, hi + 1)}
    for k, v in probs_in.items():
        try:
            i, f = int(round(float(k))), float(v)
        except (TypeError, ValueError):
            warnings.append("%s: ignored score level '%s'" % (key, k))
            continue
        if lo <= i <= hi:
            grid[str(i)] += max(f, 0.0)
        else:
            warnings.append("%s: ignored out-of-range level %s" % (key, k))
    raw = norm(grid)
    if a.get("bare"):  # a bare level carries no probability: spread it around the level
        lvl = int(max(raw, key=raw.get))
        nbrs = [i for i in (lvl - 1, lvl + 1) if lo <= i <= hi]
        raw = {str(i): 0.0 for i in range(lo, hi + 1)}
        raw[str(lvl)] = 0.7 if nbrs else 1.0
        for i in nbrs:
            raw[str(i)] = 0.3 / len(nbrs)
    pred_raw = max(raw, key=raw.get)
    cal = dd.temper(raw, temp)
    res = {}
    if floor and floor > lo:
        floor = min(int(floor), hi)
        for i in range(lo, floor):
            cal[str(i)] = 0.0
        if sum(cal.values()) <= 0:
            cal[str(floor)] = 1.0
        cal = norm(cal)
        res["floor"] = floor
    level = int(max(cal, key=cal.get))
    expected = sum(int(k) * v for k, v in cal.items())
    conf = cal[str(level)]
    second = max([v for k, v in cal.items() if k != str(level)] or [0.0])
    probs = round_probs(cal)
    res.update({"score": round(expected, 1), "level": level,
                "probs": {str(i): probs[str(i)] for i in range(lo, hi + 1)},
                "confidence": round(conf, 2), "margin": round(conf - second, 2)})
    return res, raw, pred_raw


def do_verify(key, a, spec, ctx, temp, warnings):
    try:
        p = float(a["p_true"])
    except (KeyError, TypeError, ValueError):
        raise AnswerError("missing or invalid p_true")
    if not 0.0 <= p <= 1.0:
        raise AnswerError("p_true must be between 0 and 1")
    raw = min(max(p, 0.02), 0.98)
    cal = min(max(dd.temper_p(raw, temp), 0.02), 0.98)
    res = {"p_true": round(cal, 2), "verdict": cal >= 0.5,
           "confidence": round(max(cal, 1 - cal), 2), "margin": round(abs(2 * cal - 1), 2)}
    return res, raw, raw >= 0.5


def finalize_answer(key, a, ctx):
    if not isinstance(a, dict):
        raise AnswerError("answer is not an object")
    if a.get("error"):
        return {"error": str(a["error"]), "via": a.get("via", "llm")}, None
    cat, policy, warnings = ctx["cat"], ctx["policy"], ctx["warnings"]
    via = a.get("via", "llm")

    preset = a.get("preset") or (key if dd.find_preset(cat, key)[0] else None)
    kind, spec = dd.find_preset(cat, preset)
    if preset and kind is None:
        raise AnswerError("unknown preset: %s" % preset)
    if kind is None:
        kind, spec = a.get("type"), {}
    if kind not in ("choose", "score", "verify"):
        raise AnswerError("missing or invalid type")
    if a.get("options") and spec.get("options") == "auto":
        spec = dict(spec, options=a["options"])
    name = preset or key
    temp = dd.temperature_for(policy, name) if via in MODEL_VIA else 1.0

    if kind == "choose":
        body, raw, pred = do_choose(key, a, spec, ctx, temp, warnings)
    elif kind == "score":
        body, raw, pred = do_score(key, a, spec, ctx, temp, warnings, ctx["floors"].get(name))
    else:
        body, raw, pred = do_verify(key, a, spec, ctx, temp, warnings)

    res = {"type": kind, "via": via}
    if preset and preset != key:
        res["preset"] = preset
    res.update(body)
    threshold = dd.threshold_for(policy, key, preset)
    conf = res["confidence"]
    res["abstain"] = conf < threshold
    if conf >= threshold:
        res["next"] = "accept"
    elif conf >= threshold - ESCALATE_BAND:
        res["next"] = "escalate"
    else:
        res["next"] = "ask_human"
    if abs(temp - 1.0) > 1e-9:
        res["calibrated"] = True
    evidence = a.get("evidence")
    if isinstance(evidence, list) and evidence and ctx.get("why", True):
        res["evidence"] = [str(e)[:140] for e in evidence[:3]]

    if not preset:  # ad-hoc questions differ every time, so they cannot be calibrated or learned
        return res, None
    if isinstance(raw, dict):
        raw_logged = {k: round(v, 4) for k, v in raw.items() if v > 0}
    else:
        raw_logged = round(raw, 4)
    row = {"key": key, "preset": name, "type": kind, "via": via, "pred_raw": pred, "raw": raw_logged,
           "final": res.get("choice", res.get("level", res.get("p_true"))), "confidence": conf}
    return res, row


# ---------------------------------------------------------------- floors and gates

def compute_floors(files, policy):
    floors, matched = {}, []
    for pat, val in (policy.get("critical_paths") or {}).items():
        rx = dd.glob_regex(pat)
        hits = [f for f in files if rx.match(f)]
        if not hits:
            continue
        spec = val if isinstance(val, dict) else {"risk": val}
        applied = {}
        for preset, level in spec.items():
            try:
                level = int(level)
            except (TypeError, ValueError):
                continue
            floors[preset] = max(floors.get(preset, 0), level)
            applied[preset] = level
        matched.append({"pattern": pat, "files": hits[:5], "min": applied})
    return floors, matched


ATOM = re.compile(r"^\s*([A-Za-z0-9_\-]+)\.(score|level|p_true|confidence|margin|choice)\s*"
                  r"(>=|<=|==|!=|>|<)\s*(.+?)\s*$")
OPS = {
    ">=": lambda x, y: x >= y, "<=": lambda x, y: x <= y, ">": lambda x, y: x > y,
    "<": lambda x, y: x < y, "==": lambda x, y: x == y, "!=": lambda x, y: x != y,
}


def eval_expr(expr, answers):
    """Tiny, safe rule language: `key.field op value` joined by `and` / `or` (and binds tighter)."""
    for disjunct in re.split(r"\s+or\s+", expr.strip()):
        ok = True
        for atom in re.split(r"\s+and\s+", disjunct):
            m = ATOM.match(atom)
            if not m:
                raise ValueError("bad rule: %s" % atom)
            key, field, op, rhs = m.groups()
            ans = answers.get(key)
            if not isinstance(ans, dict) or "error" in ans or field not in ans:
                ok = False
                break
            lhs = ans[field]
            if field == "choice":
                rhs = rhs.strip("'\"")
                if op not in ("==", "!="):
                    raise ValueError("choice only supports == and !=")
            else:
                rhs = float(rhs)
            if not OPS[op](lhs, rhs):
                ok = False
                break
        if ok:
            return True
    return False


def eval_gate(gate, answers, warnings):
    fired_block, fired_review = [], []
    for expr in gate.get("block") or []:
        try:
            if eval_expr(expr, answers):
                fired_block.append(expr)
        except ValueError as exc:
            warnings.append("gate: %s" % exc)
    for expr in gate.get("review") or []:
        try:
            if eval_expr(expr, answers):
                fired_review.append(expr)
        except ValueError as exc:
            warnings.append("gate: %s" % exc)
    required = gate.get("requires") or []
    missing = [k for k in required if not isinstance(answers.get(k), dict) or "error" in answers[k]]
    used = set(re.findall(r"([A-Za-z0-9_\-]+)\.(?:score|level|p_true|confidence|margin|choice)",
                          " ".join((gate.get("block") or []) + (gate.get("review") or []))))
    uncertain = [k for k in list(required) + sorted(used - set(required))
                 if isinstance(answers.get(k), dict) and answers[k].get("abstain")]
    if fired_block:
        verdict = "block"
    elif fired_review or missing or uncertain:
        verdict = "review"
    else:
        verdict = "pass"
    out = {"verdict": verdict, "fired": fired_block + fired_review}
    if missing:
        out["missing"] = missing
    if uncertain:
        out["uncertain"] = uncertain
    return out


# ---------------------------------------------------------------- driver

def finalize_one(obj, ctx, did, rows):
    files = [dd.norm_path(f, ctx["root"]) for f in (obj.get("files") or [])
             if isinstance(f, str) and f.strip()]
    floors, matched = compute_floors(files, ctx["policy"])
    local = dict(ctx, floors=floors, warnings=[])
    answers = {}
    raw_answers = obj.get("answers") or {}
    if not isinstance(raw_answers, dict):
        raw_answers = {}
    for key, a in raw_answers.items():
        try:
            res, row = finalize_answer(key, a, local)
        except AnswerError as exc:
            answers[key] = {"error": str(exc)}
            continue
        except Exception as exc:
            answers[key] = {"error": "finalizer error: %s" % type(exc).__name__}
            continue
        answers[key] = res
        if row:
            row.update({"id": did, "item": obj.get("id")})
            rows.append(row)
    out = {}
    if obj.get("id") is not None:
        out["id"] = obj["id"]
    if obj.get("error") and not answers:
        out["error"] = str(obj["error"])
    out["answers"] = answers
    gates = {}
    for bundle in (obj.get("bundles") or []) if answers else []:
        gate = ctx["cat"]["gates"].get(bundle)
        if gate:
            gates[bundle] = eval_gate(gate, answers, local["warnings"])
    if gates:
        out["gates"] = gates
    meta = {}
    if files:
        meta["files"] = files[:20]
    if matched:
        meta["critical_paths"] = matched
    if local["warnings"]:
        meta["warnings"] = local["warnings"][:10]
    if meta:
        out["meta"] = meta
    return out


def new_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


def write_log(root, policy, rows):
    if rows and policy.get("log", True):
        stamp = int(time.time())
        dd.append_jsonl(dd.state_dir(root) / "log.jsonl", [dict(r, ts=stamp) for r in rows])


def main():
    text = sys.stdin.read()
    try:
        raw = parse_input(text)
    except Exception:
        print(json.dumps({"error": "unreadable decision", "v": dd.VERSION}))
        return
    if not isinstance(raw, dict) or (not raw.get("answers") and not raw.get("items")):
        print(json.dumps({"error": str((raw or {}).get("error") or "empty decision"), "v": dd.VERSION}))
        return
    root = dd.project_root()
    policy = dd.load_policy(root)
    ctx = {"root": root, "policy": policy, "cat": dd.load_catalog(policy),
           "routes": dd.discover_routes(root, policy)}
    did, rows = new_id(), []
    out = {"id": did, "v": dd.VERSION}
    if isinstance(raw.get("items"), list):
        out["items"] = [finalize_one(it, ctx, did, rows) for it in raw["items"] if isinstance(it, dict)]
    else:
        out.update(finalize_one(raw, ctx, did, rows))
    try:
        write_log(root, policy, rows)
    except Exception:
        pass
    print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": "finalizer crashed: %s" % type(exc).__name__, "v": dd.VERSION}))
    sys.exit(0)
