"""Bootstrap training data with the teacher.

Two modes:
  synth   the writer model produces diverse examples for chosen targets, then a separate
          blind labeler labels them; examples where they disagree are kept for statistics
          but not used for training (they are ambiguous or off-target)
  label   real inputs you provide (a text file of requests or issues, or commits from a git
          repository) are labeled by the teacher; better distribution than synthetic text
"""
import math
import random
import re
import subprocess
from collections import Counter

from . import prompts as P
from .common import ORDINAL, append_jsonl, log, make_item, read_jsonl, sha, stamp

BATCH = {"route_gen": 40, "route_label": 40, "triage_gen": 20, "triage_label": 25, "pr_gen": 6, "pr_label": 8}
MAX_DIFF_CHARS = 12000


def norm_text(t):
    return re.sub(r"\s+", " ", (t or "").lower()).strip()


def example_key(inp):
    return sha(inp.get("kind"), norm_text(inp.get("text")) + "\n" + norm_text(inp.get("diff")))[:16]


def agree(task, gen_label, teacher_label):
    if gen_label is None:
        return True
    if task in ORDINAL:
        order = ORDINAL[task]
        if gen_label in order and teacher_label in order:
            return abs(order.index(gen_label) - order.index(teacher_label)) <= 1
    return gen_label == teacher_label


def estimate(families, n_routes, sizes):
    """Number of model calls per family, before any are made."""
    out = {}
    if "route" in families:
        classes = n_routes + 1
        per = sizes["route_per_class"]
        out["route"] = classes * math.ceil(per / BATCH["route_gen"]) + math.ceil(classes * per / BATCH["route_label"])
    if "triage" in families:
        n = sizes["triage_n"]
        out["triage"] = math.ceil(n / BATCH["triage_gen"]) + math.ceil(n / BATCH["triage_label"])
    if "pr" in families:
        n = sizes["pr_n"]
        out["pr"] = math.ceil(n / BATCH["pr_gen"]) + math.ceil(n / BATCH["pr_label"])
    return out


class Writer(object):
    """Collects new examples for one family, dropping exact duplicates of anything already stored."""

    def __init__(self, paths, family):
        self.paths, self.family = paths, family
        self.seen = set(e.get("key") for e in read_jsonl(paths.examples(family)))
        self.rows = []
        self.dupes = 0

    def add(self, inp, gen, source, meta):
        key = example_key(inp)
        if key in self.seen:
            self.dupes += 1
            return None
        self.seen.add(key)
        row = {"id": "%s-%s" % (self.family, key[:12]), "key": key, "family": self.family, "source": source,
               "input": inp, "gen": gen, "teacher": {}, "agree": {}, "labels": {}, "meta": meta}
        self.rows.append(row)
        return row

    def save(self):
        append_jsonl(self.paths.examples(self.family), self.rows)
        return len(self.rows)


def _finish_labels(rows, tasks):
    stats = {t: Counter() for t in tasks}
    for r in rows:
        for t in tasks:
            if t not in r["teacher"]:
                stats[t]["unlabeled"] += 1
                continue
            label, conf = r["teacher"][t]
            ok = agree(t, r["gen"].get(t), label)
            r["agree"][t] = ok
            stats[t]["agree" if ok else "disagree"] += 1
            if ok:
                r["labels"][t] = label
    return {t: dict(c) for t, c in stats.items()}


def _label_in_batches(teacher, rows, build, parse, size, rnd, label):
    order = list(range(len(rows)))
    rnd.shuffle(order)  # mixed batches, so the labeler cannot infer labels from batch composition
    batches = [order[i:i + size] for i in range(0, len(order), size)]
    prompts = [build([rows[i] for i in b]) for b in batches]
    replies = teacher.ask_many(prompts, label=label)
    for b, (text, info) in zip(batches, replies):
        for pos, value in parse(text, len(b)).items():
            rows[b[pos - 1]]["teacher"].update(value)


# ---------------------------------------------------------------- synth: route

def synth_route(gen_t, label_t, routes, per_class, seed, paths):
    rnd = random.Random("route-%d" % seed)
    names = [r["name"] for r in routes]
    targets = names + ["none"]
    prompts, owners = [], []
    for t in targets:
        for b in range(math.ceil(per_class / BATCH["route_gen"])):
            n = min(BATCH["route_gen"], per_class - b * BATCH["route_gen"])
            prompts.append(P.route_gen(routes, t, n, b + targets.index(t) * 10, seed))
            owners.append(t)
    log("route: writing %d requests across %d routes (+none) in %d calls" % (per_class * len(targets), len(names),
                                                                            len(prompts)))
    replies = gen_t.ask_many(prompts, label="route writing")
    w = Writer(paths, "route")
    meta = {"seed": seed, "gen_model": gen_t.model, "label_model": label_t.model, "created": stamp()}
    for t, (text, info) in zip(owners, replies):
        for line in P.parse_route_gen(text)[:BATCH["route_gen"]]:
            w.add({"kind": "text", "text": line}, {"route": t}, "llm-synth", dict(meta))
    valid = set(targets)
    log("route: labeling %d new requests blind" % len(w.rows))
    _label_in_batches(label_t, w.rows, lambda rs: P.route_label(routes, [r["input"]["text"] for r in rs]),
                      lambda text, n: {i: {"route": v} for i, v in P.parse_route_label(text, n, valid).items()},
                      BATCH["route_label"], rnd, "route labeling")
    stats = _finish_labels(w.rows, ["route"])
    return {"family": "route", "new": w.save(), "duplicates": w.dupes, "agreement": stats,
            "classes": targets}


# ---------------------------------------------------------------- synth: triage

def synth_triage(gen_t, label_t, n, seed, paths):
    rnd = random.Random("triage-%d" % seed)
    types = list(P.ISSUE_TYPES)
    domains = list(P.DOMAINS)
    tl = (types * (n // len(types) + 1))[:n]
    dl = (domains * (n // len(domains) + 1))[:n]
    rnd.shuffle(tl)
    rnd.shuffle(dl)
    targets = list(zip(tl, dl))
    batches = [targets[i:i + BATCH["triage_gen"]] for i in range(0, n, BATCH["triage_gen"])]
    log("triage: writing %d issues in %d calls" % (n, len(batches)))
    replies = gen_t.ask_many([P.triage_gen(b, i, seed) for i, b in enumerate(batches)], label="triage writing")
    w = Writer(paths, "triage")
    meta = {"seed": seed, "gen_model": gen_t.model, "label_model": label_t.model, "created": stamp()}
    for b, (text, info) in zip(batches, replies):
        for idx, (sev, body) in P.parse_triage_gen(text, len(b)).items():
            t, d = b[idx - 1]
            w.add({"kind": "text", "text": body}, {"issue_type": t, "domain": d, "severity": sev}, "llm-synth",
                  dict(meta))
    log("triage: labeling %d new issues blind" % len(w.rows))
    _label_in_batches(label_t, w.rows, lambda rs: P.triage_label([r["input"]["text"] for r in rs]),
                      P.parse_triage_label, BATCH["triage_label"], rnd, "triage labeling")
    stats = _finish_labels(w.rows, ["issue_type", "severity", "domain"])
    return {"family": "triage", "new": w.save(), "duplicates": w.dupes, "agreement": stats}


# ---------------------------------------------------------------- synth: pull requests

def _valid_diff(diff):
    if not diff or len(diff) > MAX_DIFF_CHARS:
        return False
    item = make_item({"input": {"kind": "diff", "diff": diff}})
    changed = sum(len(f.added) + len(f.removed) for f in item.files)
    return bool(item.files) and changed >= 2


def synth_pr(gen_t, label_t, n, seed, paths):
    rnd = random.Random("pr-%d" % seed)
    targets = P.pr_targets(n, rnd)
    batches = [targets[i:i + BATCH["pr_gen"]] for i in range(0, n, BATCH["pr_gen"])]
    log("pr: writing %d diffs in %d calls" % (n, len(batches)))
    replies = gen_t.ask_many([P.pr_gen(b, i, seed) for i, b in enumerate(batches)], label="pr writing")
    w = Writer(paths, "pr")
    meta = {"seed": seed, "gen_model": gen_t.model, "label_model": label_t.model, "created": stamp()}
    invalid = 0
    for b, (text, info) in zip(batches, replies):
        for idx, diff in P.parse_pr_gen(text, len(b)).items():
            if not _valid_diff(diff):
                invalid += 1
                continue
            risk, breaking, kind, stack = b[idx - 1]
            m = dict(meta, change=kind, stack=stack)
            w.add({"kind": "diff", "diff": diff}, {"risk": str(risk), "breaking_change": "true" if breaking else "false"},
                  "llm-synth", m)
    log("pr: labeling %d new diffs blind (%d unparseable dropped)" % (len(w.rows), invalid))
    _label_in_batches(label_t, w.rows, lambda rs: P.pr_label([r["input"]["diff"] for r in rs]),
                      P.parse_pr_label, BATCH["pr_label"], rnd, "pr labeling")
    stats = _finish_labels(w.rows, ["risk", "breaking_change"])
    return {"family": "pr", "new": w.save(), "duplicates": w.dupes, "invalid": invalid, "agreement": stats}


# ---------------------------------------------------------------- label mode: your real data

def label_texts(label_t, family, texts, routes, seed, paths):
    rnd = random.Random("label-%s-%d" % (family, seed))
    w = Writer(paths, family)
    meta = {"seed": seed, "label_model": label_t.model, "created": stamp()}
    for t in texts:
        t = t.strip()
        if 3 <= len(t) <= 4000:
            w.add({"kind": "text", "text": t}, {}, "llm-label", dict(meta))
    log("%s: labeling %d of your texts" % (family, len(w.rows)))
    if family == "route":
        valid = set(r["name"] for r in routes) | {"none"}
        _label_in_batches(label_t, w.rows, lambda rs: P.route_label(routes, [r["input"]["text"] for r in rs]),
                          lambda text, n: {i: {"route": v} for i, v in P.parse_route_label(text, n, valid).items()},
                          BATCH["route_label"], rnd, "labeling")
        tasks = ["route"]
    else:
        _label_in_batches(label_t, w.rows, lambda rs: P.triage_label([r["input"]["text"] for r in rs]),
                          P.parse_triage_label, BATCH["triage_label"], rnd, "labeling")
        tasks = ["issue_type", "severity", "domain"]
    stats = _finish_labels(w.rows, tasks)
    return {"family": family, "new": w.save(), "duplicates": w.dupes, "agreement": stats, "mode": "label"}


def commits_from_git(repo, n, max_changed=400):
    """Recent non-merge commits as diffs, skipping huge or binary-only ones."""
    shas = subprocess.run(["git", "-C", str(repo), "log", "--no-merges", "--format=%H", "-n", str(n * 3)],
                          capture_output=True, text=True, timeout=60).stdout.split()
    out = []
    for s in shas:
        if len(out) >= n:
            break
        diff = subprocess.run(["git", "-C", str(repo), "show", "--format=", "--patch", "--no-color", "--no-ext-diff",
                               s], capture_output=True, text=True, errors="replace", timeout=60).stdout
        changed = sum(1 for line in diff.splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---")))
        if 2 <= changed <= max_changed and len(diff) <= MAX_DIFF_CHARS and _valid_diff(diff):
            out.append(diff)
    return out


def label_commits(label_t, repo, n, seed, paths):
    rnd = random.Random("label-pr-%d" % seed)
    diffs = commits_from_git(repo, n)
    w = Writer(paths, "pr")
    meta = {"seed": seed, "label_model": label_t.model, "created": stamp(), "repo": str(repo)}
    for d in diffs:
        w.add({"kind": "diff", "diff": d}, {}, "llm-label", dict(meta))
    log("pr: labeling %d commits from %s" % (len(w.rows), repo))
    _label_in_batches(label_t, w.rows, lambda rs: P.pr_label([r["input"]["diff"] for r in rs]),
                      P.parse_pr_label, BATCH["pr_label"], rnd, "labeling")
    stats = _finish_labels(w.rows, ["risk", "breaking_change"])
    return {"family": "pr", "new": w.save(), "duplicates": w.dupes, "agreement": stats, "mode": "label"}
