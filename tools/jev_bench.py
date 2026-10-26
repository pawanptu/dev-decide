#!/usr/bin/env python3
"""Benchmark dev-decide on the public datasets that independent tests of Jev used, next to the published numbers.

  python3 tools/jev_bench.py download            fetch Banking77, TweetEval (offensive), HelpSteer2 and RAGTruth
  python3 tools/jev_bench.py local               train dev-decide's local tier on each training split, test it
  python3 tools/jev_bench.py report              write dev-decide-kit/bench/report.md

Options for local: --n 300 (test items per task, sampled with seed 42 as the independent Jev test did),
--train-max 8000 (training examples per task), --tasks banking77,offensive,helpsteer2,ragtruth.

The local tier is trained the way dev-decide's own learned tier is: dev-decide's featurizers (words, and words plus
the bundled meaning model), logistic regression, temperature scaling on a held-out part of the training split, and
the pure-Python predictor dev-decide runs, so the timings are what a user gets. It needs numpy, scipy and
scikit-learn (as dev-decide-kit does); dev-decide itself stays standard-library only.

Fairness notes, also written into the report: the test items are our own seed-42 sample of each test split, not the
exact items the published tests drew; Jev and the LLMs answered zero-shot (no training), while this local tier is
trained on each dataset's training split, which is how dev-decide is meant to be used (it learns your decisions).
"""
import argparse
import csv
import gzip
import io
import json
import math
import random
import statistics
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "dev-decide-kit" / "bench"
DATA = BENCH / "data"
sys.path.insert(0, str(ROOT / "dev-decide-kit"))

SOURCES = {
    "banking77": {
        "train": "https://raw.githubusercontent.com/PolyAI-LDN/task-specific-datasets/master/banking_data/train.csv",
        "test": "https://raw.githubusercontent.com/PolyAI-LDN/task-specific-datasets/master/banking_data/test.csv"},
    "offensive": {
        k: "https://raw.githubusercontent.com/cardiffnlp/tweeteval/main/datasets/offensive/%s.txt" % k
        for k in ("train_text", "train_labels", "test_text", "test_labels")},
    "helpsteer2": {
        "train": "https://huggingface.co/datasets/nvidia/HelpSteer2/resolve/main/train.jsonl.gz",
        "test": "https://huggingface.co/datasets/nvidia/HelpSteer2/resolve/main/validation.jsonl.gz"},
    "ragtruth": {
        "response": "https://raw.githubusercontent.com/ParticleMedia/RAGTruth/main/dataset/response.jsonl",
        "source": "https://raw.githubusercontent.com/ParticleMedia/RAGTruth/main/dataset/source_info.jsonl"},
}
TASKS = {  # kind, what the published tests measured
    "banking77": {"kind": "choose", "title": "Intent routing (Banking77, 77 intents)"},
    "offensive": {"kind": "verify", "title": "Moderation (TweetEval offensive)", "positive": "true"},
    "helpsteer2": {"kind": "score", "title": "Answer rating (HelpSteer2 helpfulness 0-4)"},
    "ragtruth": {"kind": "verify", "title": "Hallucination detection (RAGTruth)", "positive": "true"},
}
# Published results (independent tests, not ours). AIMLAPI: 300 items per task, seed 42, 2026-09-25.
PUBLISHED = {
    "banking77": [("Jev 1.13", {"accuracy": 0.783, "ece": 0.096, "acc_at_0.9": 0.939, "cov_at_0.9": 0.66,
                                "latency_ms": 840, "usd_per_1m": 98}),
                  ("Claude Opus 5.5", {"accuracy": 0.857, "latency_ms": 2970, "usd_per_1m": 8243}),
                  ("GPT-6 Sol", {"accuracy": 0.853, "latency_ms": 1810, "usd_per_1m": 2321})],
    "offensive": [("Jev 1.13", {"accuracy": 0.813, "f1_positive": 0.696, "ece": 0.032, "acc_at_0.9": 0.955,
                                "cov_at_0.9": 0.52, "usd_per_1m": 19}),
                  ("Claude Opus 5.5", {"accuracy": 0.840, "f1_positive": 0.684, "usd_per_1m": 1621})],
    "helpsteer2": [("Jev 1.13", {"spearman": 0.394, "ece": 0.284, "usd_per_1m": 47}),
                   ("Gemini 3.5 Flash", {"spearman": 0.486, "usd_per_1m": 358}),
                   ("Claude Opus 5.5", {"spearman": 0.483, "usd_per_1m": 7028})],
    "ragtruth": [("Jev 1.13 (Arize, 2,675 rows)", {"accuracy": 0.855, "latency_ms": 80}),
                 ("Claude Opus 5 judge (Arize, balanced accuracy)", {"balanced_accuracy": 0.866})],
}
PUBLISHED_SOURCES = [
    ("AIMLAPI: What is Jev (Banking77, TweetEval, HelpSteer2)", "https://aimlapi.com/blog/what-is-jev"),
    ("Arize: decision model benchmark (RAGTruth)", "https://arize.com/?p=32807"),
]


# ---------------------------------------------------------------- download

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "dev-decide-bench"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def download(_args):
    DATA.mkdir(parents=True, exist_ok=True)
    (BENCH / ".gitignore").write_text("data/\n", encoding="utf-8")
    for task, files in SOURCES.items():
        for name, url in files.items():
            out = DATA / task / (name + (".gz" if url.endswith(".gz") else ""))
            if out.exists() and out.stat().st_size > 0:
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            print("downloading %s/%s" % (task, name))
            out.write_bytes(_get(url))
    print("data in %s" % DATA)


def load(task):
    """(train, test) lists of {"text": str, "label": str}."""
    d = DATA / task
    if task == "banking77":
        def rows(name):
            with open(d / name, encoding="utf-8") as fh:
                return [{"text": r["text"], "label": r["category"]} for r in csv.DictReader(fh)]
        return rows("train"), rows("test")
    if task == "offensive":
        def rows(split):
            texts = (d / ("%s_text" % split)).read_text(encoding="utf-8").splitlines()
            labels = (d / ("%s_labels" % split)).read_text(encoding="utf-8").split()
            return [{"text": t, "label": "true" if l.strip() == "1" else "false"} for t, l in zip(texts, labels)]
        return rows("train"), rows("test")
    if task == "helpsteer2":
        def rows(name):
            with gzip.open(d / (name + ".gz"), "rt", encoding="utf-8") as fh:
                return [{"text": "PROMPT:\n%s\n\nRESPONSE:\n%s" % (r["prompt"], r["response"]),
                         "label": str(int(r["helpfulness"]))} for r in map(json.loads, fh)]
        return rows("train"), rows("test")
    if task == "ragtruth":
        sources = {}
        with open(d / "source", encoding="utf-8") as fh:
            for r in map(json.loads, fh):
                info = r.get("source_info")
                info = info if isinstance(info, str) else json.dumps(info, ensure_ascii=False)
                sources[r["source_id"]] = "%s\n%s" % (r.get("prompt") or "", info)
        train, test = [], []
        with open(d / "response", encoding="utf-8") as fh:
            for r in map(json.loads, fh):
                row = {"text": "SOURCE:\n%s\n\nRESPONSE:\n%s" % (sources.get(r["source_id"], "")[:6000], r["response"]),
                       "label": "true" if r.get("labels") else "false"}
                (test if r.get("split") == "test" else train).append(row)
        return train, test
    raise ValueError(task)


# ---------------------------------------------------------------- local tier

def _kit():
    from kitlib import common, metrics, train
    D = common.devdecide()
    return common, metrics, train, D


def _items(rows, common):
    out = []
    for r in rows:
        item = common.make_item({"input": {"text": r["text"]}})
        out.append((item, common.facts_for(item)))
    return out


def _spearman(a, b):
    from scipy.stats import spearmanr
    v = spearmanr(a, b).correlation
    return None if v != v else float(v)


def run_task(task, args):
    import numpy as np
    common, M, T, D = _kit()
    fm = D["fm"]
    kind = TASKS[task]["kind"]
    train_rows, test_rows = load(task)
    rnd = random.Random(42)
    test = rnd.sample(test_rows, min(args.n, len(test_rows)))
    rnd2 = random.Random(7)
    train_rows = rnd2.sample(train_rows, min(args.train_max, len(train_rows)))
    cut = int(len(train_rows) * 0.85)
    fit_rows, val_rows = train_rows[:cut], train_rows[cut:]
    classes = sorted(set(r["label"] for r in train_rows))  # scikit-learn's own order (0-4 sorts the same as text)
    print("%s: %d train, %d validation, %d test, %d classes" % (task, len(fit_rows), len(val_rows), len(test), len(classes)))
    t0 = time.time()
    fit_it, val_it, test_it = _items(fit_rows, common), _items(val_rows, common), _items(test, common)
    names = [n for n in T.featurizer_names() if n in ("f2", "f4")] or ["f2"]
    best = None
    for fz in names:
        f = fm.FEATURIZERS[fz]
        X = {k: [f(i, fa) for i, fa in its] for k, its in (("fit", fit_it), ("val", val_it))}
        cols = {}
        for v in X["fit"]:
            for b in v:
                if b not in cols:
                    cols[b] = len(cols)
        inv = np.empty(len(cols), dtype=np.int64)
        for b, c in cols.items():
            inv[c] = b
        from scipy import sparse

        def mat(vs):
            data, idx, ptr = [], [], [0]
            for v in vs:
                for b, x in v.items():
                    c = cols.get(b)
                    if c is not None:
                        data.append(x)
                        idx.append(c)
                ptr.append(len(idx))
            return sparse.csr_matrix((data, idx, ptr), shape=(len(vs), len(cols)))
        Xf, Xv = mat(X["fit"]), mat(X["val"])
        yf = [r["label"] for r in fit_rows]
        yv = np.array([classes.index(r["label"]) for r in val_rows])
        for C in (0.25, 1.0, 4.0):
            for cw in (None, "balanced"):
                clf = T._fit(Xf, yf, np.ones(len(yf)), C, cw)
                z = T._logits(clf, Xv, classes)
                temp = M.fit_temperature(z, yv)
                p = M.softmax(z / temp)
                acc = float((p.argmax(1) == yv).mean())
                ll = float(-np.log(np.clip(p[np.arange(len(yv)), yv], 1e-12, 1)).mean())
                cand = (acc, -ll, fz, C, cw, clf, temp, inv)
                if best is None or cand[:2] > best[:2]:
                    best = cand
        print("  %s done (%.0fs), best so far %s C=%s cw=%s acc %.3f" % (fz, time.time() - t0, best[2], best[3], best[4], best[0]))
    acc_v, _, fz, C, cw, clf, temp, inv = best
    buckets, rows, bias = T.export_rows(clf, inv, classes)
    model = T.devdecide_model(classes, "choose" if kind != "score" else "score", buckets, rows, bias, temp, fz)
    f = fm.FEATURIZERS[fz]
    probs, times = [], []
    for item, facts in test_it:  # what a user's process does per decision: featurize, then the pure-Python model
        s = time.perf_counter()
        p = fm.predict(model, f(item, facts))
        times.append((time.perf_counter() - s) * 1000)
        probs.append([p[c] for c in classes])
    P = np.array(probs)
    y = np.array([classes.index(r["label"]) for r in test])
    pred = P.argmax(1)
    conf = P.max(1)
    correct = pred == y
    out = {"task": task, "n_test": len(test), "n_train": len(fit_rows), "featurizer": fz, "C": C,
           "class_weight": cw or "none", "temperature": round(float(temp), 4), "classes": len(classes),
           "accuracy": round(float(correct.mean()), 4), "ece": round(float(M.ece(conf, correct)), 4),
           "latency_ms": round(statistics.median(times), 3), "latency_p90_ms": round(sorted(times)[int(0.9 * len(times))], 3),
           "majority_baseline": round(Counter(r["label"] for r in test).most_common(1)[0][1] / float(len(test)), 4)}
    for tau in (0.9, 0.6):
        m = conf >= tau
        out["cov_at_%s" % tau] = round(float(m.mean()), 4)
        out["acc_at_%s" % tau] = round(float(correct[m].mean()), 4) if m.any() else None
    if kind == "verify":
        pos = classes.index(TASKS[task]["positive"])
        tp = int(((pred == pos) & (y == pos)).sum())
        fp = int(((pred == pos) & (y != pos)).sum())
        fn = int(((pred != pos) & (y == pos)).sum())
        prec = tp / float(tp + fp) if tp + fp else 0.0
        rec = tp / float(tp + fn) if tp + fn else 0.0
        out["f1_positive"] = round(2 * prec * rec / (prec + rec), 4) if prec + rec else 0.0
        tpr = rec
        tn = int(((pred != pos) & (y != pos)).sum())
        tnr = tn / float(tn + fp) if tn + fp else 0.0
        out["balanced_accuracy"] = round((tpr + tnr) / 2, 4)
    if kind == "choose":
        out["macro_f1"] = round(float(M.macro_f1(y, pred, list(range(len(classes))))), 4)
    if kind == "score":
        levels = np.array([int(c) for c in classes])
        out["spearman"] = round(_spearman(levels[pred], levels[y]), 4)
        out["spearman_expected"] = round(_spearman(P @ levels, levels[y]), 4)
        out["mae"] = round(float(np.abs(levels[pred] - levels[y]).mean()), 4)
    out["seconds"] = round(time.time() - t0, 1)
    return out


def local(args):
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    results_path = BENCH / "results-local.json"
    results = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else {}
    for t in tasks:
        r = run_task(t, args)
        print(json.dumps(r))
        results[t] = r
        results_path.write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    print("results in %s" % results_path)


# ---------------------------------------------------------------- report

def _fmt(v, kind):
    if v is None:
        return "–"
    if kind in ("latency_ms",):
        return ("%.2f ms" % v) if v < 10 else ("%.0f ms" % v)
    if kind == "usd_per_1m":
        return "$%s" % format(int(round(v)), ",")
    if kind in ("spearman", "ece"):
        return "%.3f" % v
    return "%.1f%%" % (100 * v)


COLS = [("accuracy", "Accuracy"), ("balanced_accuracy", "Balanced acc."), ("f1_positive", "F1 (positive)"),
        ("spearman", "Spearman"), ("ece", "Calibration error (ECE)"), ("acc_at_0.9", "Right when ≥ 0.9 sure"),
        ("cov_at_0.9", "Share ≥ 0.9 sure"), ("latency_ms", "Time per decision"), ("usd_per_1m", "Cost per 1M")]


def report(_args):
    res = json.loads((BENCH / "results-local.json").read_text(encoding="utf-8"))
    out = ["# dev-decide vs. published Jev results", "",
           "Generated by `python3 tools/jev_bench.py report` on %s." % time.strftime("%Y-%m-%d"), "",
           "**Read this first.** The Jev and LLM rows are other people's published results; the dev-decide rows are ours, "
           "on our own seed-42 sample of the same public test sets (not the identical items). Jev and the LLMs answered "
           "zero-shot; dev-decide's local tier was trained on each dataset's training split, which is how it is meant to "
           "be used, so this compares a trained specialist with general models, not like with like. dev-decide's time is "
           "measured on this machine with no network; the others include their API round trip.", ""]
    for task, spec in TASKS.items():
        r = res.get(task)
        if not r:
            continue
        rows = [("**dev-decide local tier** (trained on %s examples, %s)" % (format(r["n_train"], ","), r["featurizer"]),
                 dict(r, usd_per_1m=0))] + PUBLISHED.get(task, [])
        cols = [(k, h) for k, h in COLS if any(k in d for _, d in rows)]
        out += ["## %s" % spec["title"], "",
                "| | " + " | ".join(h for _, h in cols) + " |", "|---|" + "---|" * len(cols)]
        for name, d in rows:
            out.append("| %s | %s |" % (name, " | ".join(_fmt(d.get(k), k) for k, _ in cols)))
        out += ["", "Majority-class baseline: %s. Test items: %d." % (_fmt(r["majority_baseline"], "accuracy"), r["n_test"])]
        if "spearman_expected" in r:
            out.append("Spearman of the expected score (probability-weighted): %.3f; mean absolute error %.2f levels."
                       % (r["spearman_expected"], r["mae"]))
        out.append("")
    out += ["## Sources of the published numbers", ""] + ["- [%s](%s)" % s for s in PUBLISHED_SOURCES] + [""]
    path = BENCH / "report.md"
    path.write_text("\n".join(out), encoding="utf-8")
    print("wrote %s" % path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["download", "local", "report"])
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--train-max", type=int, default=8000)
    ap.add_argument("--tasks", default="banking77,offensive,helpsteer2,ragtruth")
    a = ap.parse_args()
    {"download": download, "local": local, "report": report}[a.command](a)
