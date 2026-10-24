#!/usr/bin/env python3
"""Train the learned fast models (System One) from past decisions.

Usage:
  train.py                 report what can be trained and how well, without saving
  train.py --write         save the models that pass validation
  train.py --preset risk   only one preset
Options: --min-n 40  --target 0.9  --min-coverage 0.15

Teachers, strongest first: your labels (label.py, weight 3), model answers
(weight 1), exact local rules (weight 0.3). Each example's features were stored
as hashed numbers when it was decided, so no code or prompt text is needed.

A model is enabled only if, on held-out examples it never trained on, there is a
confidence threshold where accepted answers reach the target accuracy while still
covering a useful share of requests. Below that threshold the engine keeps asking
the model, so the learned tier only answers where it has proven itself.
"""
import argparse
import math
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ddcore as dd  # noqa: E402
import fastmodel as fm  # noqa: E402

TEMPS = [round(0.5 + 0.1 * i, 2) for i in range(26)]
THRESHOLDS = [round(0.5 + 0.025 * i, 3) for i in range(20)]
WEIGHT = {"label": 3.0, "llm": 1.0, "llm-deep": 1.0, "rule": 0.3}


def target_vector(row, truth, classes):
    K = len(classes)
    idx = {c: i for i, c in enumerate(classes)}
    if truth is not None:
        q = [0.0] * K
        key = truth
        if row["type"] == "verify":
            key = "true" if (truth is True or str(truth).lower() in ("true", "yes", "1")) else "false"
        elif row["type"] == "score":
            try:
                key = str(int(truth))
            except (TypeError, ValueError):
                return None
        if str(key) not in idx:
            return None
        q[idx[str(key)]] = 1.0
        return q
    raw = row.get("raw")
    if row["type"] == "verify":
        try:
            p = float(raw)
        except (TypeError, ValueError):
            return None
        return [1.0 - p, p] if classes == ["false", "true"] else None
    if not isinstance(raw, dict):
        return None
    q = [float(raw.get(c, 0.0)) for c in classes]
    rest = float(raw.get("_rest", 0.0)) + sum(v for k, v in raw.items() if k not in idx and k != "_rest")
    empty = [i for i, v in enumerate(q) if v == 0.0]
    if rest > 0 and empty:
        for i in empty:
            q[i] += rest / len(empty)
    s = sum(q)
    return [v / s for v in q] if s > 0 else None


def evaluate(model, X, Q, temp):
    pairs, nll = [], 0.0
    for x, q in zip(X, Q):
        s = fm.predict_scores(model, x)
        p = dd.softmax([v / temp for v in s])
        best = max(range(len(p)), key=lambda i: p[i])
        truth = max(range(len(q)), key=lambda i: q[i])
        pairs.append((p[best], best == truth))
        nll -= math.log(max(p[truth], 1e-6))
    return pairs, nll / max(1, len(X))


def ece(pairs, bins=10):
    if not pairs:
        return 0.0
    total, n = 0.0, len(pairs)
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(c, ok) for c, ok in pairs if lo < c <= hi or (b == 0 and c == 0)]
        if sel:
            total += abs(sum(ok for _, ok in sel) / len(sel) - sum(c for c, _ in sel) / len(sel)) * len(sel) / n
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--preset")
    ap.add_argument("--min-n", type=int, default=40)
    ap.add_argument("--target", type=float, default=0.9)
    ap.add_argument("--min-coverage", type=float, default=0.15)
    args = ap.parse_args()

    root = dd.project_root()
    sd = root / ".claude" / "dev-decide"
    log = dd.read_jsonl(sd / "log.jsonl")
    feats = {(r["id"], r.get("item")): r["x"] for r in dd.read_jsonl(sd / "features.jsonl")
             if r.get("fv") == fm.FEATURIZER and isinstance(r.get("x"), dict)}
    labels = {(l.get("id"), l.get("item"), l.get("key")): l.get("truth") for l in dd.read_jsonl(sd / "labels.jsonl")}
    if not log or not feats:
        print("Nothing to train on yet: decisions are logged to %s with hashed features in %s.\n"
              "Use the skill for a while (model answers are the teacher), and label decisions with label.py."
              % (sd / "log.jsonl", sd / "features.jsonl"))
        return 1

    policy = dd.load_policy(root)
    cat = dd.load_catalog(policy)
    latest = {}
    for r in log:
        latest[(r.get("id"), r.get("item"), r.get("key"))] = r
    groups = {}
    for (did, item, key), r in latest.items():
        x = feats.get((did, item))
        if x is None or r.get("via") not in WEIGHT and (did, item, key) not in labels:
            continue
        if args.preset and r.get("preset") != args.preset:
            continue
        groups.setdefault(r["preset"], []).append((r, x, labels.get((did, item, key)), "%s|%s|%s" % (did, item, key)))

    header = "%-26s %5s %5s %8s %6s %6s %9s %9s  %s" % ("preset", "train", "val", "val acc", "temp", "ECE", "threshold", "coverage", "status")
    print(header)
    print("-" * len(header))
    saved = []
    for preset, rows in sorted(groups.items()):
        kind, spec = dd.find_preset(cat, preset)
        if kind is None:
            continue
        if spec.get("options") == "auto":
            names = sorted(set(str(r.get("pred_raw")) for r, _, _, _ in rows) | {"none"})
            spec = dict(spec, options=[n for n in names if n != "none"], allow_none=True)
        classes = fm.classes_for(kind, spec)
        data = []
        for r, x, truth, uid in rows:
            q = target_vector(r, truth, classes)
            if q is None:
                continue
            w = WEIGHT["label"] if truth is not None else WEIGHT.get(r.get("via"), 0.0)
            if w <= 0:
                continue
            data.append(({int(k): v for k, v in x.items()}, q, w, uid))
        n = len(data)
        if n < args.min_n:
            print("%-26s %5d %5s %8s %6s %6s %9s %9s  need %d+ examples" % (preset, n, "-", "-", "-", "-", "-", "-", args.min_n))
            continue
        train = [d for d in data if zlib.crc32(d[3].encode()) % 5 != 0]
        val = [d for d in data if zlib.crc32(d[3].encode()) % 5 == 0]
        if len(val) < 10:
            print("%-26s %5d %5d %8s %6s %6s %9s %9s  need 10+ held-out examples" % (preset, len(train), len(val), "-", "-", "-", "-", "-"))
            continue
        started = time.time()
        W, b = fm.train_softmax([d[0] for d in train], [d[1] for d in train], [d[2] for d in train], len(classes))
        model = {"classes": classes, "kind": kind, "weights": W, "bias": b, "temperature": 1.0}
        Xv, Qv = [d[0] for d in val], [d[1] for d in val]
        temp = min(TEMPS, key=lambda t: evaluate(model, Xv, Qv, t)[1])
        pairs, _ = evaluate(model, Xv, Qv, temp)
        acc = sum(ok for _, ok in pairs) / len(pairs)
        chosen = None
        for t in THRESHOLDS:
            covered = [ok for c, ok in pairs if c >= t]
            if len(covered) < max(5, int(args.min_coverage * len(pairs))):
                break
            if sum(covered) / len(covered) >= args.target:
                chosen = (t, sum(covered) / len(covered), len(covered) / len(pairs))
                break
        enabled = chosen is not None
        status = "enabled" if enabled else "disabled: %.0f%% accuracy not reached at %.0f%%+ coverage" % (
            args.target * 100, args.min_coverage * 100)
        print("%-26s %5d %5d %7.0f%% %6.1f %6.3f %9s %8s  %s (%.1fs)" % (
            preset, len(train), len(val), acc * 100, temp, ece(pairs),
            "%.3f" % chosen[0] if chosen else "-", "%.0f%%" % (chosen[2] * 100) if chosen else "-", status,
            time.time() - started))
        if args.write:
            pruned = {k: [round(v, 5) for v in ws] for k, ws in W.items() if max(abs(v) for v in ws) >= 1e-3}
            model.update({"weights": pruned, "temperature": temp, "threshold": chosen[0] if chosen else 1.01,
                          "bias": [round(v, 5) for v in b]})
            meta = {"enabled": enabled, "featurizer": fm.FEATURIZER, "kind": kind, "classes": classes,
                    "threshold": model["threshold"], "temperature": temp, "val_accuracy": round(acc, 3),
                    "accepted_accuracy": round(chosen[1], 3) if chosen else None,
                    "coverage": round(chosen[2], 3) if chosen else 0.0, "n_train": len(train), "n_val": len(val),
                    "trained": time.strftime("%Y-%m-%d %H:%M")}
            fm.save_model(root, preset, model, meta)
            saved.append(preset)
    if args.write:
        print("\nSaved %d model(s) to %s" % (len(saved), sd / "models"))
    else:
        print("\nDry run. Re-run with --write to save; only enabled models are used by the engine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
