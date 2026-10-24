#!/usr/bin/env python3
"""Measure how well dev-decide's confidence matches reality, per preset,
and fit a temperature and an abstain threshold for each one.

Usage:
  calibrate.py [--write] [--min-n 20] [--target 0.9]

Joins .claude/dev-decide/log.jsonl with labels.jsonl (written by label.py)
and reports, for every preset with labels:
  accuracy, ECE (expected calibration error) before and after scaling,
  the fitted temperature, and the lowest confidence threshold at which
  accepted answers reach the target accuracy.

--write saves temperature and threshold into .claude/dev-decide.json under
"calibration" (only for presets with at least --min-n labels). The skill's
finalizer applies them automatically from then on.
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ddcore as dd  # noqa: E402

TEMPS = [round(0.5 + 0.05 * i, 2) for i in range(71)]  # 0.50 .. 4.00
THRESHOLDS = [round(0.40 + 0.05 * i, 2) for i in range(12)]  # 0.40 .. 0.95


def as_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "y", "1")


def score_row(row, truth, temp):
    """Return (prob_of_truth, correct, confidence) under temperature temp."""
    kind, raw = row["type"], row["raw"]
    if kind == "verify":
        t = as_bool(truth)
        p = dd.temper_p(raw, temp)
        return (p if t else 1 - p), ((p >= 0.5) == t), max(p, 1 - p)
    probs = dd.temper(raw, temp)
    key = str(int(truth)) if kind == "score" else str(truth)
    pred = max((k for k in probs if k != "_rest"), key=probs.get)
    pt = probs.get(key)
    if pt is None:
        pt = probs.get("_rest", 0.0) if kind == "choose" else 0.0
    return max(pt, 1e-4), pred == key, probs[pred]


def ece(pairs, bins=10):
    if not pairs:
        return 0.0
    total, n = 0.0, len(pairs)
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(c, ok) for c, ok in pairs if (lo < c <= hi) or (b == 0 and c == 0)]
        if sel:
            conf = sum(c for c, _ in sel) / len(sel)
            acc = sum(1 for _, ok in sel if ok) / len(sel)
            total += abs(acc - conf) * len(sel) / n
    return total


def evaluate(samples, temp):
    nll, pairs = 0.0, []
    for row, truth in samples:
        pt, ok, conf = score_row(row, truth, temp)
        nll -= math.log(pt)
        pairs.append((conf, ok))
    return nll / len(samples), pairs


def pick_threshold(pairs, target):
    n = len(pairs)
    for t in THRESHOLDS:
        covered = [ok for c, ok in pairs if c >= t]
        if len(covered) < max(5, int(0.1 * n)):
            break
        acc = sum(covered) / len(covered)
        if acc >= target:
            return t, acc, len(covered) / n, True
    # The target is not reachable at any useful coverage. After temperature scaling,
    # confidence approximates accuracy, so accept only answers whose calibrated
    # confidence meets the target; everything else abstains.
    t = round(target, 2)
    covered = [ok for c, ok in pairs if c >= t]
    acc = sum(covered) / len(covered) if covered else 0.0
    return t, acc, len(covered) / n, False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--target", type=float, default=0.9)
    args = ap.parse_args()

    root = dd.project_root()
    d = root / ".claude" / "dev-decide"
    log = dd.read_jsonl(d / "log.jsonl")
    labels = dd.read_jsonl(d / "labels.jsonl")
    if not log or not labels:
        print("Need both %s and %s. Log decisions with the skill, then add labels with label.py."
              % (d / "log.jsonl", d / "labels.jsonl"))
        return 1

    latest_row = {(r.get("id"), r.get("item"), r.get("key")): r for r in log}
    latest_label = {(l.get("id"), l.get("item"), l.get("key")): l["truth"] for l in labels if "truth" in l}
    groups = {}
    for k, truth in latest_label.items():
        row = latest_row.get(k)
        # Only model (LLM) answers are temperature-calibrated here: local rules are exact, and the
        # learned models calibrate themselves in train.py.
        if row and row.get("via") in (None, "llm", "llm-deep"):
            groups.setdefault(row["preset"], []).append((row, truth))
    if not groups:
        print("No labels match logged decisions.")
        return 1

    header = "%-20s %5s %6s %13s %6s %10s %9s" % ("preset", "n", "acc", "ECE before>after", "temp", "threshold", "coverage")
    print(header)
    print("-" * len(header))
    results = {}
    for preset, samples in sorted(groups.items()):
        n = len(samples)
        _, pairs1 = evaluate(samples, 1.0)
        acc = sum(ok for _, ok in pairs1) / n
        best_t = min(TEMPS, key=lambda t: evaluate(samples, t)[0])
        _, pairs_t = evaluate(samples, best_t)
        thr, cov_acc, coverage, reached = pick_threshold(pairs_t, args.target)
        flag = "" if n >= args.min_n else "  (too few labels to save)"
        if not reached:
            flag += "  (%.0f%% target not reachable: only answers with calibrated confidence >= %.2f are accepted)" % (
                args.target * 100, thr)
        print("%-20s %5d %5.0f%% %6.3f>%-6.3f %6.2f %10.2f %8.0f%%%s"
              % (preset, n, acc * 100, ece(pairs1), ece(pairs_t), best_t, thr, coverage * 100, flag))
        if n >= args.min_n:
            results[preset] = {"temperature": best_t, "threshold": thr, "n": n,
                               "accuracy": round(acc, 3), "ece_before": round(ece(pairs1), 3),
                               "ece_after": round(ece(pairs_t), 3), "target_reached": reached,
                               "updated": time.strftime("%Y-%m-%d")}

    print("\ntemp > 1 means the engine was overconfident, < 1 underconfident.")
    if not args.write:
        print("Dry run. Re-run with --write to save %d preset(s) to %s" % (len(results), dd.policy_path(root)))
        return 0
    if not results:
        print("Nothing to save: no preset has %d or more labels." % args.min_n)
        return 0
    path = dd.policy_path(root)
    policy = dd.read_json(path)
    policy = policy if isinstance(policy, dict) else {}
    policy.setdefault("calibration", {}).update(results)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    print("Saved calibration for %s to %s" % (", ".join(sorted(results)), path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
