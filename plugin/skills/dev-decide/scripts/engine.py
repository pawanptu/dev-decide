"""dev-decide engine: System 1 (local rules + learned model) first, System 2 (one model call) for the rest.

For every question the engine takes the fastest source it can trust:
  rule     an exact local fact                       ~1-50 ms, free
  learned  the trained fast model, above its validated threshold   ~1 ms, free
  llm      one lean model call for everything still open           ~2-4 s
  hint     local heuristic, used only in +local mode or when no model is available
  cache    an identical earlier request (same input, policy and models)
  memory   your own correction of a near-identical input (learn.py), checked before the learned model
  episodic inputs like ones already labeled (learn.py), once that recall has proven accurate

Every decision also teaches the next one (learn.py): model answers, spot checks and your corrections train a
challenger that replaces the learned model only after it proves better on your own inputs.
"""
import json
import os
import time
from collections import Counter
from pathlib import Path

import ddcore as dd
import fastmodel
import finalize
import inputs
import learn
import request
import router
import signals

CACHE_TTL_HOURS = 24
SWEEP_EVERY_S = 600


class ItemState(object):
    def __init__(self, index, item):
        self.index = index
        self.item = item
        self.facts = signals.Facts()
        self.hints = {}
        self.decided = {}
        self.pending = []
        self.x = None
        self.novel = []
        self.disagree = []
        self.error = item.error


def _mtime(path):
    try:
        return os.path.getmtime(str(path))
    except OSError:
        return 0


def _cache_get(root, key, ttl_hours):
    path = Path(root) / ".claude" / "dev-decide" / "cache" / ("%s.json" % key)
    try:
        if time.time() - path.stat().st_mtime > ttl_hours * 3600:
            return None
        return dd.read_json(path, default=None)
    except OSError:
        return None


def _cache_put(root, key, out, ttl_hours):
    d = dd.state_dir(root) / "cache"
    d.mkdir(exist_ok=True)
    dd.write_json(d / ("%s.json" % key), out)
    # Expired entries are swept at most every SWEEP_EVERY_S, not on every write: a full sweep stats
    # every cached decision, which grows with traffic. Reads already ignore expired entries.
    marker = d / ".swept"
    now = time.time()
    try:
        if now - marker.stat().st_mtime < SWEEP_EVERY_S:
            return
    except OSError:
        pass
    try:
        marker.touch()
        cutoff = now - ttl_hours * 3600
        with os.scandir(str(d)) as entries:
            for e in entries:
                if e.name.endswith(".json") and e.stat().st_mtime < cutoff:
                    os.unlink(e.path)
    except OSError:
        pass


def _fmt_answer(key, a):
    if "error" in a:
        return "%s error" % key
    mark = "?" if a.get("abstain") else ""
    if a["type"] == "choose":
        return "%s %s %.2f%s" % (key, a["choice"], a["confidence"], mark)
    if a["type"] == "score":
        return "%s %s%s" % (key, a["score"], mark)
    return "%s %.2f%s" % (key, a["p_true"], mark)


def _summary_single(out, order, via_counts, total_ms, notes=()):
    parts = []
    answers = out.get("answers", {})
    shown = []
    for name, g in (out.get("gates") or {}).items():
        fired = g.get("fired") or []
        text = "%s gate %s" % (name, g["verdict"].upper())
        if g["verdict"] != "pass" and fired:
            text += " (%s)" % "; ".join(fired[:2])
        if g.get("uncertain"):
            text += " uncertain: %s" % ", ".join(g["uncertain"][:3])
        parts.append(text)
        for rule in fired[:3]:
            k = rule.split(".", 1)[0]
            if k in answers and k not in shown:
                shown.append(k)
    for k in order:
        if k in answers and answers[k].get("abstain") and k not in shown:
            shown.append(k)
    for k in order:
        if len(shown) >= 6:
            break
        if k in answers and k not in shown:
            shown.append(k)
    parts += [_fmt_answer(k, answers[k]) for k in shown[:6] if k in answers]
    if len(answers) > len(shown[:6]):
        parts.append("+%d more" % (len(answers) - len(shown[:6])))
    human = [k for k in order if answers.get(k, {}).get("next") == "ask_human"]
    if human:
        parts.append("needs a human: " + ", ".join(human[:4]))
    local = sum(via_counts.get(v, 0) for v in ("rule", "learned", "cache", "memory", "episodic"))
    model = via_counts.get("llm", 0) + via_counts.get("llm-deep", 0) + via_counts.get("llm-strong", 0)
    hint = via_counts.get("hint", 0)
    strong = via_counts.get("llm-strong", 0)
    src = "%d local" % local + (" + %d model" % model if model else "") + \
        (" (%d by the stronger model)" % strong if strong else "") + (" + %d hint" % hint if hint else "")
    parts.append(src)
    parts += list(notes)
    parts.append("%.2f s" % (total_ms / 1000.0) if total_ms >= 100 else "%d ms" % total_ms)
    return "dev-decide | " + " | ".join(parts)


def _summary_batch(out, order, via_counts, total_ms, notes=()):
    items = out.get("items", [])
    verdicts = Counter()
    frags = []
    for it in items[:10]:
        if it.get("error"):
            frags.append("%s error" % it.get("id"))
            continue
        for g in (it.get("gates") or {}).values():
            verdicts[g["verdict"]] += 1
        ans = it.get("answers", {})
        top = [k for k in order if k in ans][:2]
        frags.append("%s: %s" % (it.get("id"), ", ".join(_fmt_answer(k, ans[k]) for k in top)))
    head = "dev-decide | %d items" % len(items)
    if verdicts:
        head += " (%s)" % ", ".join("%d %s" % (n, v) for v, n in verdicts.most_common())
    tail = "%.2f s" % (total_ms / 1000.0)
    extra = "".join(" | " + n for n in notes)
    return head + " | " + " | ".join(frags) + (" | ..." if len(items) > 10 else "") + extra + " | " + tail


def _error(message, t0, hint=None):
    out = {"error": message, "v": dd.VERSION}
    if hint:
        out["hint"] = hint
    show_hint = hint and "Example" not in message
    out["summary"] = "dev-decide error: %s%s" % (message, (" " + hint) if show_hint else "")
    out["meta"] = {"ms": {"total": int((time.perf_counter() - t0) * 1000)}}
    return out


class _Retry(object):
    """An item re-asked with a stronger model, for some of its keys."""
    __slots__ = ("index", "item", "pending", "facts", "hints")

    def __init__(self, st, keys):
        self.index, self.item, self.pending, self.facts, self.hints = st.index, st.item, list(keys), st.facts, st.hints


def _escalate(work, req, cat, ctx, flags, policy, root, llm_info):
    """Re-ask unsure answers to decisions that matter with a stronger model, within the daily budget."""
    plan = []
    for st in work:
        keys = learn.escalation_keys(policy, cat, req.questions, req.bundles, st.decided)
        if keys:
            plan.append(_Retry(st, keys))
    if not plan or not learn.claim_escalation(root, policy):
        return
    import llm
    results, info = llm.ask(plan, req.questions, ctx, flags, learn.strong_policy(policy), root)
    by_index = {st.index: st for st in work}
    n = 0
    for r in plan:
        got = results.get(r.index, {})
        for key in r.pending:
            a = got.get(key)
            if a and not a.get("error"):
                a["via"] = "llm-strong"
                by_index[r.index].decided[key] = a
                n += 1
    llm_info["calls"] = llm_info.get("calls", 0) + info.get("calls", 0)
    llm_info["ms"] = llm_info.get("ms", 0) + info.get("ms", 0)
    if info.get("cost_usd") is not None:
        llm_info["cost_usd"] = round((llm_info.get("cost_usd") or 0.0) + info["cost_usd"], 5)
    llm_info["escalated"] = {"keys": n, "backend": info.get("backend"), "error": info.get("error")} if info.get("error") \
        else {"keys": n, "backend": info.get("backend")}


def decide(text=None, spec=None, cwd=None, extra_flags=(), use_cache=True):
    t0 = time.perf_counter()
    cwd = str(cwd or os.getcwd())
    root = dd.project_root(cwd)
    dd.seed_policy(root)
    fastmodel.seed_models(root)  # only here: the routing hook must never write to a repository
    policy = dd.load_policy(root)
    cat = dd.load_catalog(policy)
    req = request.parse_spec(spec, cat) if spec is not None else request.parse_text(text, cat)
    req.flags.update(extra_flags)
    if req.error:
        return _error(req.error, t0, "Example: /dev-decide pr :: DIFF   or   /dev-decide route :: <task>")
    flags = req.flags
    mode = "local" if flags & {"local", "fast"} else ("llm" if flags & {"llm", "deep"} else "hybrid")
    need_routes = any(q.preset == "route" or q.spec.get("options") == "auto" for q in req.questions.values())
    routes = dd.discover_routes(root, policy) if need_routes else []
    items = [inputs.resolve(iid, raw, root, cwd) for iid, raw in req.items]
    for i, diff in req.diffs.items():
        inputs.attach_diff(items[i], diff)
    batch = len(items) > 1 or items[0].id is not None
    order = list(req.questions)

    ttl = float(policy.get("cache_ttl_hours", CACHE_TTL_HOURS) or 0)
    cache_key = None
    if use_cache and ttl > 0 and "nocache" not in flags and policy.get("cache", True) is not False:
        models_index = Path(root) / ".claude" / "dev-decide" / "models" / "index.json"
        cache_key = dd.stable_hash(dd.VERSION, json.dumps(spec, sort_keys=True) if spec is not None else text,
                                   sorted(flags), [it.fingerprint() for it in items],
                                   _mtime(dd.SKILL_DIR / "presets.json"), json.dumps(policy, sort_keys=True),
                                   _mtime(models_index), _mtime(dd.SKILL_DIR / "prompts" / "system2.md"),
                                   _mtime(learn.Paths(root).memory))
        hit = _cache_get(root, cache_key, ttl)
        if hit:
            learn.touch_recent(root, policy, hit.get("id"))
            ms = int((time.perf_counter() - t0) * 1000)
            hit.setdefault("meta", {})["cache"] = True
            hit["meta"]["ms"] = {"total": ms}
            summary = hit.get("summary", "")
            hit["summary"] = summary.rsplit(" | ", 1)[0] + " | cached, %d ms" % ms if summary else summary
            return hit

    reg = fastmodel.Registry(root)
    L = learn.Session(root, policy, cat, mode)
    capture = str(policy.get("capture", "hashed")).lower() not in ("false", "off", "none", "0")
    wanted = [q.preset for q in req.questions.values() if q.preset]
    names = {key: L.name(q) for key, q in req.questions.items()}  # preset, or a stable name for a custom question
    warnings = list(req.warnings)
    states = []
    for idx, item in enumerate(items):
        st = ItemState(idx, item)
        states.append(st)
        if item.error:
            continue
        warnings += item.notes
        F, s1 = signals.analyze(item, wanted, routes=routes, router=router)
        st.facts = F
        if capture or L.enabled or reg.any_enabled(wanted):
            st.x = fastmodel.Features(item, F)
        for key, q in req.questions.items():
            ans = s1.get(q.preset) if q.preset else None
            name = names[key]
            mem = L.recall(q, st, name) if mode != "llm" and L.enabled else None
            if mem is not None and mem.get("similarity", 0) >= 0.995:  # you corrected this very input
                st.decided[key] = mem
                continue
            if mode != "llm" and ans is not None and ans.strength == signals.EXACT:
                st.decided[key] = dict(ans.raw(), via="rule", preset=q.preset, evidence=ans.evidence)
                continue
            if mem is not None:
                st.decided[key] = mem
                continue
            epi = L.episodic(q, st, name, routes if need_routes else None) if mode != "llm" and L.enabled else None
            if mode != "llm" and name and st.x is not None:
                learned = reg.answer(name, q.kind, st.x)
                if learned and L.enabled and q.kind == "choose" and learn.unknown_option_hint(reg, name, ans):
                    learned = None  # looks like an option the model has not learned yet: ask, and learn from it
                if learned and L.enabled:
                    fam = L.familiarity(reg, name, st.x)
                    if fam is not None and fam < float(L.cfg["novelty_floor"]) and mode == "hybrid":
                        learned = None  # unlike what the model was trained on: ask, and learn from it
                        st.novel.append(key)
                    elif fam is not None:
                        learned["familiarity"] = round(fam, 3)
                if learned and epi is not None and mode != "local" and \
                        learn.answer_label(q.kind, learned) != learn.answer_label(q.kind, epi):
                    st.disagree.append(key)  # recall and the trained model disagree: ask the model, and learn from it
                    learned = epi = None
                if learned:
                    extra = {"preset": q.preset} if q.preset else learn.adhoc_fields(q)
                    st.decided[key] = dict(learned, via="learned", **extra)
                    continue
            if epi is not None and key not in st.disagree and key not in st.decided:
                st.decided[key] = epi
                continue
            if ans is not None:
                st.hints[key] = ans
            st.pending.append(key)
    local_ms = int((time.perf_counter() - t0) * 1000)

    llm_info = {}
    work = [st for st in states if st.pending and not st.error]
    if work and mode != "local":
        import llm  # imported only when a model call is needed
        ctx = {"routes": routes, "policy": policy}
        if L.enabled:
            ctx["precedents"] = L.precedents(work, req.questions)
            L.start_spot_checks(routes)
        results, llm_info = llm.ask(work, req.questions, ctx, flags, policy, root)
        skipped = []
        for st in work:
            got = results.get(st.index, {})
            for key in list(st.pending):
                a = got.get(key)
                if a and not a.get("error"):
                    st.decided[key] = a
                    st.pending.remove(key)
                elif not llm_info.get("error"):
                    skipped.append(key if not batch else "%s/%s" % (st.item.id, key))
        if not llm_info.get("error"):
            _escalate(work, req, cat, ctx, flags, policy, root, llm_info)
        L.join_spot_checks()
        if llm_info.get("error"):
            warnings.append(llm_info["error"] + "; used local estimates where available")
        if skipped:
            warnings.append("the model did not answer %s; used the local estimate where there is one"
                            % ", ".join(skipped[:6]))
    missing_reason = "no model was asked (+local)" if mode == "local" else \
        ("model unavailable" if llm_info.get("error") else "the model did not answer this key")
    for st in states:
        for key in st.pending:
            q = req.questions[key]
            h = st.hints.get(key)
            if h is not None:
                st.decided[key] = dict(h.raw(), via="hint", preset=q.preset, evidence=h.evidence)
            else:
                st.decided[key] = {"error": "not decided: %s" % missing_reason, "via": "none"}
        st.pending = []

    fctx = {"root": root, "policy": policy, "cat": cat, "routes": routes, "why": "why" in flags}
    did = finalize.new_id()
    rows = []
    finals = []
    for st in states:
        if st.error:
            finals.append({"id": st.item.id, "error": st.error, "answers": {}})
            continue
        obj = {"id": st.item.id if batch else None, "files": [f.path for f in st.item.files],
               "bundles": list(req.bundles), "answers": {k: st.decided[k] for k in order if k in st.decided}}
        finals.append(finalize.finalize_one(obj, fctx, did, rows))
    try:
        finalize.write_log(root, policy, rows)
        if capture and rows:
            logged = set(r.get("item") for r in rows)
            feats = [{"id": did, "item": st.item.id if batch else None, "fv": fastmodel.FEATURIZER,
                      "x": {str(k): v for k, v in st.x.items()}}
                     for st in states if st.x is not None and (st.item.id if batch else None) in logged]
            if feats:
                dd.append_jsonl(dd.state_dir(root) / "features.jsonl", feats)
    except Exception:
        warnings.append("could not write the decision log")
    if L.enabled:
        try:
            L.observe(states, req.questions, did, finals, reg)
            L.step(400 if llm_info else 40, routes=routes if need_routes else None)
        except Exception as exc:
            warnings.append("learning skipped this time: %s" % type(exc).__name__)

    via_counts = Counter()
    for f in finals:
        for a in f.get("answers", {}).values():
            via_counts[a.get("via", "error")] += 1
    total_ms = int((time.perf_counter() - t0) * 1000)
    meta = {"ms": {"total": total_ms, "local": local_ms}, "via": dict(via_counts)}
    if llm_info:
        meta["ms"]["model"] = llm_info.get("ms", 0)
        meta["backend"] = llm_info.get("backend")
        meta["model_calls"] = llm_info.get("calls", 0)
        if llm_info.get("cost_usd") is not None:
            meta["cost_usd"] = llm_info["cost_usd"]
        if llm_info.get("escalated"):
            meta["escalated"] = llm_info["escalated"]
    novel = sorted(set(k for st in states for k in st.novel))
    if novel:
        meta["novel"] = novel  # local answers withheld because the input looked unfamiliar
    disagree = sorted(set(k for st in states for k in st.disagree))
    if disagree:
        meta["disagreed"] = disagree  # recall and the trained model disagreed, so the model was asked
    if mode != "hybrid":
        meta["mode"] = mode
    learning = L.meta() if L.enabled else {}
    if learning:
        meta["learning"] = learning
    if warnings:
        meta["warnings"] = warnings[:10]
    notes = L.notes() if L.enabled else []

    if batch:
        out = {"id": did, "v": dd.VERSION, "items": finals, "meta": meta}
        out["summary"] = _summary_batch(out, order, via_counts, total_ms, notes)
    else:
        single = finals[0]
        if single.get("error") and not single.get("answers"):
            return _error(single["error"], t0)
        out = {"id": did, "v": dd.VERSION}
        out.update({k: v for k, v in single.items() if k not in ("id", "meta")})
        fmeta = single.get("meta") or {}
        if fmeta.get("warnings"):
            meta["warnings"] = (meta.get("warnings") or []) + fmeta.pop("warnings")
        meta.update(fmeta)
        out["meta"] = meta
        out["summary"] = _summary_single(out, order, via_counts, total_ms, notes)
    if cache_key and not llm_info.get("error"):
        try:
            _cache_put(root, cache_key, out, ttl)
        except Exception:
            pass
    return out
