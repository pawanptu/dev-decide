"""Train, calibrate, select and export one fast model per decision.

Per decision: a held-out test split is set aside first and never used for any choice. On the rest
(the development set) every candidate (2 featurizers, or 4 with meaning features, x 5 regularization strengths
x 2 class weightings)
is scored with 5-fold, near-duplicate-aware cross-validation, so selection, temperature scaling and
the auto-accept threshold all rest on out-of-fold predictions for the whole development set rather
than on a small calibration split.

Selection follows the three priorities in order:
  1. calibration (constraint): out-of-fold ECE may exceed what a perfectly calibrated model would show
     at this sample size (estimated by simulation) by at most --ece-max
  2. accuracy (objective): highest out-of-fold accuracy; candidates within one standard error are tied
  3. latency (tie-break): among tied candidates, the fastest featurizer, then the lowest log-loss
If no candidate passes the calibration test, the lowest out-of-fold log-loss wins (a proper scoring rule
that cannot be gamed by a constant model).

The winner is refit on the whole development set, rebuilt exactly as dev-decide runs it (pruned float32
weights, the pure-Python predict code), and every headline test number comes from that rebuilt model.
"""
import math
import os
import time
import warnings
import zlib
from array import array
from collections import Counter, defaultdict

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression

from . import dataset as DS
from . import metrics as M
from .common import FAMILIES, ORDINAL, TASK_KIND, devdecide, log, stamp, write_json

GRID_C = (0.25, 1.0, 4.0, 16.0, 64.0)
GRID_CW = (None, "balanced")
FEATURIZER_NAMES = ("f1", "f2")  # f3 and f4 (meaning features) are added when dev-decide's embedding model is present


def featurizer_names():
    """f1 and f2 always; f3 and f4 when dev-decide 5 and its bundled embedding model are available."""
    names = ["f1", "f2"]
    if os.environ.get("KIT_NO_SEMANTIC"):
        return tuple(names)
    fm = devdecide()["fm"]
    if "f3" in fm.FEATURIZERS:
        try:
            import semantic
            if semantic.get() is not None:
                names += ["f3", "f4"]
        except ImportError:
            pass
    return tuple(names)
FOLDS = 5
TEST_SHARE = 0.15
PRUNE = 1e-4
MAX_BUCKETS = 60000
ACCEPT_TOLERANCE = 0.05  # accepted test accuracy may sit this far below the target (sampling noise)
EVIDENCE_MARGIN = 0.15   # and its 95% lower bound this far below, so thin evidence does not enable a model
MIN_ACCEPTED = 10
MISSING_LOGIT = -30.0


class Config(object):
    def __init__(self, target=0.9, min_coverage=0.1, ece_max=0.05, min_n=40, seed=7, min_confidence=0.6):
        self.target, self.min_coverage, self.ece_max, self.min_n, self.seed = target, min_coverage, ece_max, min_n, seed
        # The learned tier answers only when it would not abstain: dev-decide marks answers below 0.6 as abstain,
        # and those are better left to the LLM tier.
        self.min_confidence = min_confidence


def _idx(labels, classes):
    pos = {c: i for i, c in enumerate(classes)}
    return np.array([pos.get(l, -1) for l in labels], dtype=np.int64)


def _logits(clf, X, classes):
    """Decision scores aligned to `classes` (classes a fold model never saw get a very low score)."""
    z = clf.decision_function(X)
    if z.ndim == 1:
        z = np.stack([np.zeros_like(z), z], axis=1)
    own = [str(c) for c in clf.classes_]
    if own == classes:
        return z
    out = np.full((X.shape[0], len(classes)), MISSING_LOGIT)
    for j, c in enumerate(own):
        out[:, classes.index(c)] = z[:, j]
    return out


def _fit(X, y, w, C, cw):
    clf = LogisticRegression(C=C, class_weight=cw, max_iter=5000)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        warnings.simplefilter("ignore", UserWarning)
        clf.fit(X, y, sample_weight=w)
    return clf


def _order_key(group, seed):
    return zlib.crc32(("%s|%s" % (seed, group)).encode())


def split_test(samples, seed):
    """Hold out a test split by group (stratified by label); human gold labels are the test set when enough."""
    gold_n = sum(1 for s in samples if s.gold)
    if gold_n >= DS.MIN_GOLD_TEST:
        gold_groups = set(s.group for s in samples if s.gold)
        for s in samples:
            s.split = "test" if s.gold else ("drop" if s.group in gold_groups else "dev")
        return True
    by_group = defaultdict(list)
    for s in samples:
        by_group[s.group].append(s)
    by_label = defaultdict(list)
    for g, members in by_group.items():
        by_label[Counter(s.label for s in members).most_common(1)[0][0]].append(g)
    for label, groups in by_label.items():
        groups.sort(key=lambda g: _order_key(g, seed))
        n_test = int(round(TEST_SHARE * len(groups))) if len(groups) >= 4 else (1 if len(groups) >= 2 else 0)
        for i, g in enumerate(groups):
            for s in by_group[g]:
                s.split = "test" if i < n_test else "dev"
    return False


def assign_folds(dev, k, seed):
    by_group = defaultdict(list)
    for s in dev:
        by_group[s.group].append(s)
    by_label = defaultdict(list)
    for g, members in by_group.items():
        by_label[Counter(s.label for s in members).most_common(1)[0][0]].append(g)
    fold_of = {}
    offset = 0
    for label in sorted(by_label):
        groups = sorted(by_label[label], key=lambda g: _order_key(g, seed + 1))
        for i, g in enumerate(groups):
            fold_of[g] = (i + offset) % k
        offset += len(groups)
    return np.array([fold_of[s.group] for s in dev])


def _matrix(samples, name, cols):
    data, idx, ptr = [], [], [0]
    for s in samples:
        for b, v in s.vec[name].items():
            c = cols.get(b)
            if c is not None:
                idx.append(c)
                data.append(v)
        ptr.append(len(idx))
    from scipy import sparse
    return sparse.csr_matrix((np.array(data, dtype=np.float64), np.array(idx, dtype=np.int64),
                              np.array(ptr, dtype=np.int64)), shape=(len(samples), max(1, len(cols))))


def ece_noise_floor(conf, sims=300, seed=0):
    """Mean ECE a perfectly calibrated model with these confidences would show at this sample size."""
    rng = np.random.RandomState(seed)
    return float(np.mean([M.ece(conf, rng.random_sample(len(conf)) < conf) for _ in range(sims)]))


def export_rows(clf, inv_cols, classes):
    coef, intercept = clf.coef_, clf.intercept_
    own = [str(c) for c in clf.classes_]
    if coef.shape[0] == 1:  # binary: dev-decide uses the logits [0, z]
        W = np.stack([np.zeros(coef.shape[1]), coef[0]], axis=1)
        bias = [0.0, float(intercept[0])]
    else:
        W = coef.T
        bias = [float(v) for v in intercept]
    if own != classes:
        raise ValueError("final model is missing classes")
    mags = np.abs(W).max(axis=1)
    keep = np.where(mags >= PRUNE)[0]
    if len(keep) > MAX_BUCKETS:
        keep = keep[np.argsort(-mags[keep], kind="stable")[:MAX_BUCKETS]]
    buckets = inv_cols[keep]
    order = np.argsort(buckets, kind="stable")
    return buckets[order], W[keep][order], bias


def devdecide_model(classes, kind, buckets, rows, bias, temperature, featurizer):
    """The model exactly as dev-decide holds it after loading the binary file."""
    w = array("f", rows.astype(np.float32).ravel().tolist())
    return {"classes": classes, "kind": kind, "bias": bias, "temperature": temperature, "featurizer": featurizer,
            "_idx": dict(zip(buckets.tolist(), range(len(buckets)))), "_w": w}


def predict_all(model, vecs):
    fm = devdecide()["fm"]
    classes = model["classes"]
    return np.array([[p[c] for c in classes] for p in (fm.predict(model, v) for v in vecs)])


def _select(cands, lat, cfg):
    passed = [c for c in cands if c["cv"]["ece_excess"] <= cfg.ece_max]
    if not passed:
        return min(cands, key=lambda c: c["cv"]["nll"]), "no candidate passed the calibration test; lowest log-loss"
    best = max(c["cv"]["accuracy"] for c in passed)
    n = passed[0]["cv"]["n"]
    tol = max(0.005, math.sqrt(best * (1 - best) / max(1, n)))
    tied = [c for c in passed if c["cv"]["accuracy"] >= best - tol]
    win = min(tied, key=lambda c: (lat[c["featurizer"]]["p50_us"], c["cv"]["nll"]))
    return win, "%d of %d candidates passed calibration; %d tied within one standard error (%.1f points)" % (
        len(passed), len(cands), len(tied), tol * 100)


def class_thresholds(p, y, classes, base, target, min_n=8):
    """Per predicted class, the confidence its answers need to reach the target on out-of-fold predictions.

    A class whose answers already reach the target at the base threshold keeps it (no entry). A weaker class
    gets the lowest higher threshold that reaches the target with at least min_n answers, or 1.01 (never
    answered locally) when none does. Classes with fewer than min_n answers keep the base threshold.
    """
    pred, conf = p.argmax(axis=1), p.max(axis=1)
    out = {}
    for j, c in enumerate(classes):
        m = (pred == j) & (conf >= base)
        if m.sum() < min_n or (y[m] == j).mean() >= target:
            continue
        found = None
        for t in np.unique(conf[m]):
            mm = m & (conf >= t)
            if mm.sum() < min_n:
                break
            if (y[mm] == j).mean() >= target:
                found = round(float(t), 4)
                break
        out[c] = found if found is not None else 1.01
    return out


def _effective(conf, pred, classes, base, per_class):
    """Confidences shifted so one base threshold applies each class's own threshold."""
    eff = np.array([max(base, per_class.get(classes[k], 0.0)) for k in pred])
    return conf - eff + base


def _confusions(y, pred, classes, top=5):
    c = Counter((classes[t], classes[p]) for t, p in zip(y, pred) if t >= 0 and t != p)
    return [{"true": a, "pred": b, "count": n} for (a, b), n in c.most_common(top)]


def train_task(task, samples, cfg):
    D = devdecide()
    fm, signals = D["fm"], D["signals"]
    kind, ordinal = TASK_KIND[task], ORDINAL.get(task)
    started = time.time()
    base = {"task": task, "kind": kind, "n_usable": len(samples),
            "label_counts": dict(Counter(s.label for s in samples).most_common())}
    if len(samples) < cfg.min_n:
        return dict(base, status="skipped", reason="only %d usable examples (need %d)" % (len(samples), cfg.min_n))
    DS.featurize(samples, FEATURIZER_NAMES)
    use_gold = split_test(samples, cfg.seed)
    samples = [s for s in samples if s.split != "drop"]
    dev = [s for s in samples if s.split == "dev"]
    test = [s for s in samples if s.split == "test"]
    base["splits"] = {"dev": len(dev), "test": len(test)}
    base["test_source"] = "human gold labels" if use_gold else "held-out teacher labels"
    if len(dev) < 30 or len(test) < 8:
        return dict(base, status="skipped", reason="splits too small: %s" % base["splits"])
    classes = sorted(set(s.label for s in dev))
    if len(classes) < 2:
        return dict(base, status="skipped", reason="training data has a single label")
    folds = assign_folds(dev, FOLDS, cfg.seed)
    y_dev = _idx([s.label for s in dev], classes)
    y_test = _idx([s.label for s in test], classes)
    w_dev = np.array([s.weight for s in dev])
    labels_dev = np.array([s.label for s in dev])

    cands, lat, mats = [], {}, {}
    for fz in FEATURIZER_NAMES:
        cols = {}
        for s in dev:
            for b in s.vec[fz]:
                if b not in cols:
                    cols[b] = len(cols)
        inv = np.empty(len(cols), dtype=np.int64)
        for b, c in cols.items():
            inv[c] = b
        X_dev, X_test = _matrix(dev, fz, cols), _matrix(test, fz, cols)
        mats[fz] = (X_dev, X_test, inv)
        lat[fz] = M.latency(lambda s, f=fz: fm.FEATURIZERS[f](s.item, s.facts), dev[:60] + test, min_runs=200)
        for C in GRID_C:
            for cw in GRID_CW:
                t0 = time.time()
                oof = np.zeros((len(dev), len(classes)))
                for f in range(FOLDS):
                    tr, va = folds != f, folds == f
                    if not va.any() or len(set(labels_dev[tr])) < 2:
                        continue
                    clf = _fit(X_dev[tr], labels_dev[tr], w_dev[tr], C, cw)
                    oof[va] = _logits(clf, X_dev[va], classes)
                T = M.fit_temperature(oof, y_dev)
                p = M.softmax(oof / T)
                cv, conf, _ = M.evaluate(p, y_dev, classes, ordinal)
                floor = ece_noise_floor(conf)
                cv["ece_uncalibrated"] = M.evaluate(M.softmax(oof), y_dev, classes, ordinal)[0]["ece"]
                cv["ece_noise_floor"] = floor
                cv["ece_excess"] = cv["ece"] - floor
                cands.append({"featurizer": fz, "C": C, "class_weight": cw or "none", "temperature": T,
                              "cv_seconds": round(time.time() - t0, 2), "n_features": X_dev.shape[1], "cv": cv,
                              "_oof": p})
    win, why_win = _select(cands, lat, cfg)
    fz = win["featurizer"]
    X_dev, X_test, inv = mats[fz]
    final = _fit(X_dev, labels_dev, w_dev, win["C"], None if win["class_weight"] == "none" else "balanced")
    buckets, rows, bias = export_rows(final, inv, classes)
    model = devdecide_model(classes, kind, buckets, rows, bias, win["temperature"], fz)
    p_test = predict_all(model, [s.vec[fz] for s in test])
    p_ref = M.softmax(_logits(final, X_test, classes) / win["temperature"])
    parity = float(np.abs(p_test - p_ref).max())

    test_final, t_conf, t_ok = M.evaluate(p_test, y_test, classes, ordinal)
    test_final["ece_uncalibrated"] = M.evaluate(M.softmax(_logits(final, X_test, classes)), y_test, classes,
                                                ordinal)[0]["ece"]
    test_final["ece_noise_floor"] = ece_noise_floor(t_conf)
    oof_conf = win["_oof"].max(axis=1)
    oof_ok = win["_oof"].argmax(axis=1) == y_dev
    thr = M.threshold_for(oof_conf, oof_ok, cfg.target, cfg.min_coverage)
    if thr and thr[0] < cfg.min_confidence:  # never auto-answer below the confidence dev-decide would abstain at
        at = M.at_threshold(oof_conf, oof_ok, cfg.min_confidence)
        thr = (cfg.min_confidence, at["accepted_accuracy"], at["coverage"]) if at["accepted"] else None
    per_class = class_thresholds(win["_oof"], y_dev, classes, thr[0], cfg.target) if thr else {}
    selective = None
    if thr:
        t_pred = p_test.argmax(axis=1)
        selective = dict(M.at_threshold(_effective(t_conf, t_pred, classes, thr[0], per_class), t_ok, thr[0]),
                         threshold=thr[0], class_thresholds=per_class, cv_coverage=thr[2],
                         cv_accepted_accuracy=thr[1], target=cfg.target)
    if not thr:
        enabled, why = False, "no confidence threshold reaches %.0f%% accuracy in cross-validation" % (cfg.target * 100)
    elif selective["accepted"] < MIN_ACCEPTED:
        enabled, why = False, "only %d confident test answers, too few to verify the threshold (need %d)" % (
            selective["accepted"], MIN_ACCEPTED)
    elif selective["accepted_accuracy"] < cfg.target - ACCEPT_TOLERANCE:
        enabled, why = False, "accepted test accuracy %.0f%% is below the %.0f%% target" % (
            selective["accepted_accuracy"] * 100, cfg.target * 100)
    elif selective["accepted_ci95"][0] < cfg.target - EVIDENCE_MARGIN:
        enabled, why = False, "accepted test accuracy %.0f%% is not yet proven: its 95%% lower bound is %.0f%%" % (
            selective["accepted_accuracy"] * 100, selective["accepted_ci95"][0] * 100)
    else:
        enabled, why = True, "accepted answers reach the target on held-out data"

    def analyze_and_predict(s):
        F, _ = signals.analyze(s.item, [])
        return fm.predict(model, fm.FEATURIZERS[fz](s.item, F))

    lat_model = M.latency(lambda s: fm.predict(model, fm.FEATURIZERS[fz](s.item, s.facts)), test, min_runs=300)
    lat_pipe = M.latency(analyze_and_predict, test, min_runs=100)

    per_fz = {}
    for name in FEATURIZER_NAMES:
        group = [c for c in cands if c["featurizer"] == name]
        b, _ = _select(group, lat, cfg)
        per_fz[name] = {"C": b["C"], "class_weight": b["class_weight"], "cv_accuracy": b["cv"]["accuracy"],
                        "cv_ece": b["cv"]["ece"], "featurize_p50_us": lat[name]["p50_us"],
                        "n_features": b["n_features"]}
    majority = Counter(s.label for s in dev).most_common(1)[0][1] / len(dev)

    payload = {"classes": classes, "kind": kind, "bias": bias, "temperature": win["temperature"],
               "threshold": thr[0] if thr else 1.01, "class_thresholds": per_class, "featurizer": fz, "format": "bin1",
               "buckets": buckets, "rows": rows,
               "parity": [{"vec": {str(k): v for k, v in s.vec[fz].items()}, "probs": [float(x) for x in p]}
                          for s, p in list(zip(test, p_test))[:20]]}
    result = dict(base, status="trained", seconds=round(time.time() - started, 1),
                  winner={"featurizer": fz, "C": win["C"], "class_weight": win["class_weight"],
                          "temperature": round(win["temperature"], 4), "n_buckets": int(len(buckets)),
                          "size_kb": round((12 + 4 * len(buckets) * (1 + len(classes))) / 1024, 1),
                          "why": why_win},
                  classes=classes, cv=win["cv"], test=test_final, selective=selective, majority_rate=majority,
                  enabled=enabled, enabled_reason=why, parity_max_abs=parity,
                  latency={"model": lat_model, "pipeline": lat_pipe, "featurize": lat},
                  by_featurizer=per_fz, confusions=_confusions(y_test, p_test.argmax(axis=1), classes),
                  candidates=[{k: v for k, v in c.items() if not k.startswith("_")} for c in cands])
    return result, payload


def train_families(paths, families, cfg):
    global FEATURIZER_NAMES
    FEATURIZER_NAMES = featurizer_names()
    log("candidate featurizers: %s" % ", ".join(FEATURIZER_NAMES))
    results, payloads = [], {}
    for fam in families:
        examples, gold = DS.load(paths, fam)
        if not examples:
            log("%s: no examples yet (run generate first)" % fam)
            continue
        groups = DS.near_dup_groups(examples, fam)
        group_of = {ex["id"]: g for ex, g in zip(examples, groups)}
        n_groups = len(set(groups))
        log("%s: %d examples in %d near-duplicate groups" % (fam, len(examples), n_groups))
        for task in FAMILIES[fam]["tasks"]:
            samples = DS.usable(examples, gold, task)
            for s in samples:
                s.group = group_of[s.ex["id"]]
            out = train_task(task, samples, cfg)
            if isinstance(out, tuple):
                res, payload = out
                payloads[task] = payload
            else:
                res = out
            res["family"] = fam
            res["family_examples"] = len(examples)
            res["family_groups"] = n_groups
            results.append(res)
            if res["status"] == "trained":
                t, cv = res["test"], res["cv"]
                log("  %-16s %s C=%-4g cv acc %.1f%%  test acc %.1f%%  ECE %.3f (noise %.3f)  model p50 %.0f us  %s"
                    % (task, res["winner"]["featurizer"], res["winner"]["C"], cv["accuracy"] * 100,
                       t["accuracy"] * 100, t["ece"], t["ece_noise_floor"], res["latency"]["model"]["p50_us"],
                       "enabled" if res["enabled"] else "not enabled"))
            else:
                log("  %-16s skipped: %s" % (task, res["reason"]))
    for task, p in payloads.items():
        save_model(paths, task, p)
    write_json(paths.reports / "train.json", {"created": stamp(), "config": vars(cfg), "results": results})
    return results


def save_model(paths, task, p):
    fm = devdecide()["fm"]
    meta = {k: p[k] for k in ("classes", "kind", "bias", "temperature", "threshold", "featurizer", "format")}
    if p.get("class_thresholds"):
        meta["class_thresholds"] = p["class_thresholds"]
    write_json(paths.models / ("%s.json" % task), meta, indent=None)
    fm.write_binary(str(paths.models / ("%s.bin" % task)), p["buckets"].tolist(),
                    p["rows"].astype(np.float32).ravel().tolist(), len(p["classes"]))
    write_json(paths.models / ("%s.parity.json" % task), p["parity"], indent=None)
