"""Metrics for the three priorities: accuracy, calibration and latency.

Accuracy:    accuracy (with a Wilson 95% interval), macro-F1, ordinal MAE / within-one for ordered labels
Calibration: ECE (15 bins, top label), Brier, NLL, AURC (area under the risk-coverage curve)
Selective:   the confidence threshold that keeps accepted answers at the target accuracy, and its coverage
Latency:     pure-Python featurize + predict, the exact code dev-decide runs, in microseconds
"""
import math
import time

import numpy as np
from scipy.optimize import minimize_scalar


def softmax(z):
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def fit_temperature(logits, y):
    """Temperature that minimizes NLL on calibration data (labels the model never saw are ignored)."""
    mask = y >= 0
    if mask.sum() < 5:
        return 1.0
    L, Y = logits[mask], y[mask]

    def nll(log_t):
        p = softmax(L / math.exp(log_t))
        return -np.log(np.clip(p[np.arange(len(Y)), Y], 1e-12, 1)).mean()

    res = minimize_scalar(nll, bounds=(math.log(0.05), math.log(20.0)), method="bounded")
    return float(math.exp(res.x))


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - r) / d), min(1.0, (c + r) / d))


def ece(conf, correct, bins=15):
    if len(conf) == 0:
        return 0.0
    edges = np.linspace(0, 1, bins + 1)
    total = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (conf > lo) & (conf <= hi) if lo > 0 else (conf >= lo) & (conf <= hi)
        if m.any():
            total += abs(correct[m].mean() - conf[m].mean()) * m.mean()
    return float(total)


def aurc(conf, correct):
    if len(conf) == 0:
        return 0.0
    order = np.argsort(-conf, kind="stable")
    errors = np.cumsum(~correct[order])
    return float((errors / np.arange(1, len(conf) + 1)).mean())


def macro_f1(y_true, y_pred, labels):
    f1s = []
    for c in labels:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == c and p == c)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != c and p == c)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == c and p != c)
        if tp + fp + fn:
            f1s.append(2 * tp / (2 * tp + fp + fn))
    return float(np.mean(f1s)) if f1s else 0.0


def threshold_for(conf, correct, target, min_coverage):
    """Lowest confidence threshold whose accepted answers reach the target accuracy (max coverage)."""
    n = len(conf)
    if n == 0:
        return None
    order = np.argsort(-conf, kind="stable")
    c_sorted, ok_sorted = conf[order], correct[order]
    best = None
    hits = 0
    for k in range(1, n + 1):
        hits += int(ok_sorted[k - 1])
        if k < n and c_sorted[k] == c_sorted[k - 1]:
            continue  # thresholds sit between distinct confidences
        if k / n >= min_coverage and hits / k >= target:
            best = (float(c_sorted[k - 1]), hits / k, k / n)
    return best


def evaluate(probs, y, classes, ordinal=None):
    """probs (n, K), y: class index or -1 for labels the model cannot output."""
    n = len(y)
    pred = probs.argmax(axis=1)
    conf = probs.max(axis=1)
    correct = pred == y
    k = int(correct.sum())
    out = {"n": n, "accuracy": k / n if n else 0.0, "accuracy_ci95": wilson(k, n),
           "macro_f1": macro_f1([classes[i] if i >= 0 else None for i in y], [classes[i] for i in pred],
                                sorted(set(classes[i] for i in y if i >= 0))),
           "ece": ece(conf, correct), "aurc": aurc(conf, correct)}
    known = y >= 0
    if known.any():
        onehot = np.zeros_like(probs)
        onehot[np.arange(n)[known], y[known]] = 1.0
        out["brier"] = float(((probs - onehot) ** 2).sum(axis=1).mean())
        out["nll"] = float(-np.log(np.clip(probs[known, y[known]], 1e-12, 1)).mean())
    if ordinal:
        pos = {c: i for i, c in enumerate(ordinal)}
        diffs = [abs(pos[classes[p]] - pos[classes[t]]) for p, t in zip(pred, y)
                 if t >= 0 and classes[t] in pos and classes[p] in pos]
        if diffs:
            out["ordinal_mae"] = float(np.mean(diffs))
            out["within_one"] = float(np.mean([d <= 1 for d in diffs]))
    if len(classes) == 2 and known.all() and 0 < y.sum() < n:
        pos_scores = probs[:, 1]
        order = np.argsort(pos_scores, kind="stable")
        ranks = np.empty(n)
        ranks[order] = np.arange(1, n + 1)
        n_pos = int(y.sum())
        out["auroc"] = float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * (n - n_pos)))
    return out, conf, correct


def at_threshold(conf, correct, tau):
    m = conf >= tau
    cov = float(m.mean()) if len(conf) else 0.0
    k, n = int(correct[m].sum()), int(m.sum())
    return {"coverage": cov, "accepted": n, "accepted_accuracy": k / n if n else None,
            "accepted_ci95": wilson(k, n) if n else None}


def latency(fn, inputs, min_runs=300):
    """Per-call wall time in microseconds over warm repeated runs."""
    if not inputs:
        return None
    for x in inputs[:20]:
        fn(x)
    times = []
    while len(times) < min_runs:
        for x in inputs:
            t = time.perf_counter()
            fn(x)
            times.append((time.perf_counter() - t) * 1e6)
            if len(times) >= max(min_runs, len(inputs)):
                break
    times.sort()

    def q(p):
        return round(times[min(len(times) - 1, int(p * len(times)))], 1)

    return {"p50_us": q(0.5), "p95_us": q(0.95), "p99_us": q(0.99), "runs": len(times)}
