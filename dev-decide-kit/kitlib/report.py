"""Readable benchmark report (markdown) from the training results and generation runs."""
from collections import defaultdict

from .common import read_json, read_jsonl, stamp

JEV_MS = (70, 500)  # response time reported by TypeSafe AI for Jev (Futura Sciences, Sept 2026)
LLM_TIER_S = (2.5, 4.5)  # dev-decide's measured single-decision Claude call (v3 live tests)


def _pct(x, digits=1):
    return "-" if x is None else ("%." + str(digits) + "f%%") % (x * 100)


def _us(x):
    if x is None:
        return "-"
    return "%.0f µs" % x if x < 1000 else "%.2f ms" % (x / 1000)


def _med(xs):
    xs = sorted(xs)
    if not xs:
        return 0.0
    return xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2


def _agreement(runs):
    agg = defaultdict(lambda: [0, 0])
    for r in runs:
        for task, st in (r.get("result", {}).get("agreement") or {}).items():
            agg[task][0] += st.get("agree", 0)
            agg[task][1] += st.get("agree", 0) + st.get("disagree", 0)
    return {t: (a / n if n else None, n) for t, (a, n) in agg.items()}


def build(paths):
    train = read_json(paths.reports / "train.json")
    if not train:
        return None
    runs = read_jsonl(paths.runs)
    agree = _agreement(runs)
    cfg = train["config"]
    res = [r for r in train["results"] if r["status"] == "trained"]
    skipped = [r for r in train["results"] if r["status"] != "trained"]
    cost = sum(((r.get(k) or {}).get("cost_usd") or 0) for r in runs for k in ("gen", "label"))
    calls = sum(((r.get(k) or {}).get("calls") or 0) for r in runs for k in ("gen", "label"))
    teachers = sorted(set("%s writes, %s labels" % ((r.get("gen") or {}).get("model", "-"),
                                                    (r.get("label") or {}).get("model", "-")) for r in runs))
    L = ["# dev-decide-kit benchmark", "",
         "Trained %s. Teacher: %s. Bootstrap: %d model calls, about $%.2f of usage." % (
             train["created"], "; ".join(teachers) or "-", calls, cost), "",
         "Accuracy means agreement with the teacher's labels on examples the model never saw, unless a decision "
         "says its test set is human gold labels. *CV* is 5-fold cross-validation on the development set; *test* "
         "is a separate held-out split that no choice ever looked at.", "",
         "## Summary", "",
         "| Decision | Model | CV accuracy | Test accuracy (95% CI) | Majority baseline | ECE raw → calibrated "
         "(noise floor) | Auto-accept at {0}% target (test) | Model p50 / p99 | Size | Status |".format(
             int(round(cfg["target"] * 100))),
         "|---|---|---|---|---|---|---|---|---|---|"]
    for r in res:
        t, w, sel = r["test"], r["winner"], r.get("selective") or {}
        lo, hi = t["accuracy_ci95"]
        auto = "%s answered, %s right" % (_pct(sel.get("coverage"), 0), _pct(sel.get("accepted_accuracy"), 0)) \
            if sel else "none"
        L.append("| %s | %s, C=%g%s | %s | %s (%s–%s) | %s | %.3f → %.3f (%.3f) | %s | %s / %s | %.0f KB | %s |" % (
            r["task"], w["featurizer"], w["C"], ", balanced" if w["class_weight"] == "balanced" else "",
            _pct(r["cv"]["accuracy"]), _pct(t["accuracy"]), _pct(lo, 0), _pct(hi, 0), _pct(r.get("majority_rate"), 0),
            t.get("ece_uncalibrated", t["ece"]), t["ece"], t.get("ece_noise_floor", 0), auto,
            _us(r["latency"]["model"]["p50_us"]), _us(r["latency"]["model"]["p99_us"]), w["size_kb"],
            "**enabled**" if r["enabled"] else "not enabled"))
    for r in skipped:
        L.append("| %s | - | - | - | - | - | - | - | - | skipped: %s |" % (r["task"], r.get("reason", "")))
    ord_rows = [r for r in res if "within_one" in r["test"]]
    if ord_rows:
        L += ["", "Ordered labels: " + "; ".join("%s is within one level %s of the time on test (MAE %.2f levels)" % (
            r["task"], _pct(r["test"]["within_one"], 0), r["test"]["ordinal_mae"]) for r in ord_rows) + "."]

    if res:
        model_ms = [r["latency"]["model"]["p50_us"] / 1000 for r in res]
        pipe_ms = [r["latency"]["pipeline"]["p50_us"] / 1000 for r in res]
        pipe_p99 = max(r["latency"]["pipeline"]["p99_us"] for r in res) / 1000
        ref = _med(pipe_ms)  # the analyzers and the model: everything that is specific to one decision
        L += ["", "## Speed against Jev and the LLM tier", "",
              "| Path | Latency for one decision | Relative to the analyzers + model |", "|---|---|---|",
              "| This kit, model only (featurize + predict) | %.2f ms median (%.2f–%.2f across decisions) | - |" % (
                  _med(model_ms), min(model_ms), max(model_ms)),
              "| This kit, dev-decide analyzers + model (in-process) | %.2f ms median (%.2f–%.2f), worst "
              "p99 %.1f ms | 1× |" % (ref, min(pipe_ms), max(pipe_ms), pipe_p99),
              "| Jev (TypeSafe AI, reported, over the network) | %d–%d ms | %s–%s× slower |" % (
                  JEV_MS[0], JEV_MS[1], "{:,.0f}".format(JEV_MS[0] / ref), "{:,.0f}".format(JEV_MS[1] / ref)),
              "| dev-decide's LLM tier (one Claude call, measured) | %.1f–%.1f s | %s–%s× slower |" % (
                  LLM_TIER_S[0], LLM_TIER_S[1], "{:,.0f}".format(LLM_TIER_S[0] * 1000 / ref),
                  "{:,.0f}".format(LLM_TIER_S[1] * 1000 / ref)), "",
              "Latencies are pure-Python inference on one CPU core, in a running process, with no network, and cover "
              "the analyzers and the model only. A whole dev-decide request also parses the request, checks the "
              "cache and writes the decision log: for dev-decide 3.1 on a 2-core cloud VM, a routing or triage "
              "request took about 2 ms in a warm process and about 3 ms through the local HTTP API, and a full "
              "pull-request bundle read from git about 11 ms. A new process per call (the command line, "
              "the router hook) also pays Python start-up and first-use pattern compilation, about 55–80 ms, which "
              "is roughly Jev's best case. Jev is a general model and these are narrow models trained for one "
              "decision each, so compare accuracy only within this report.",
              "", "## How each winner was chosen", "",
              "20 candidates per decision: featurizers f1 and f2, five L2 strengths, with and without class balancing. "
              "A held-out test split is set aside first. On the rest, every candidate is scored by 5-fold "
              "cross-validation that keeps near-duplicates in the same fold, and calibrated by temperature scaling "
              "on its out-of-fold predictions. Then:",
              "1. Calibration as a constraint: out-of-fold ECE may exceed the noise floor (what a perfectly calibrated "
              "model shows at this sample size, by simulation) by at most %.2f." % cfg["ece_max"],
              "2. Accuracy as the objective: the highest cross-validated accuracy; candidates within one standard "
              "error are tied.",
              "3. Latency as the tie-break: among tied candidates, the fastest featurizer, then the lowest log-loss.",
              "The winner is refit on the whole development set. Every test number comes from the exported model "
              "(pruned float32 weights) running through dev-decide's own predict code. The auto-accept threshold is "
              "the lowest confidence at which cross-validated accepted answers reach the target, but never below "
              "%.2f, where dev-decide would abstain anyway; those inputs go to the LLM tier instead. A model is "
              "enabled only if, on test, at least 10 answers clear the threshold, their accuracy is within 5 points of "
              "the target, and the 95%% lower bound is within 15 points." % cfg.get("min_confidence", 0.6)]

    for r in res:
        t, w, sel, cv = r["test"], r["winner"], r.get("selective"), r["cv"]
        a = agree.get(r["task"])
        L += ["", "## %s" % r["task"], "",
              "Data: %d usable of %d examples in the %s family (%d near-duplicate groups). Writer and blind labeler "
              "agreed on %s. Development set %d, test set %d (%s)." % (
                  r["n_usable"], r["family_examples"], r["family"], r["family_groups"],
                  "%s of %d" % (_pct(a[0]), a[1]) if a and a[0] is not None else "-",
                  r["splits"]["dev"], r["splits"]["test"], r["test_source"]),
              "",
              "Winner: %s, C=%g, class weight %s, temperature %.2f, %d weights, %.0f KB. Selection: %s." % (
                  w["featurizer"], w["C"], w["class_weight"], w["temperature"], w["n_buckets"], w["size_kb"],
                  w.get("why", "-")),
              "",
              "Cross-validated: accuracy %s, macro-F1 %.3f, ECE %.3f (noise floor %.3f), log-loss %.3f, AURC %.3f." % (
                  _pct(cv["accuracy"]), cv["macro_f1"], cv["ece"], cv.get("ece_noise_floor", 0), cv.get("nll", 0),
                  cv["aurc"]),
              "",
              "Test: accuracy %s (95%% CI %s–%s) against a majority baseline of %s, macro-F1 %.3f, ECE %.3f "
              "(uncalibrated %.3f, noise floor %.3f), Brier %s, AURC %.3f%s%s." % (
                  _pct(t["accuracy"]), _pct(t["accuracy_ci95"][0]), _pct(t["accuracy_ci95"][1]),
                  _pct(r.get("majority_rate")), t["macro_f1"], t["ece"], t.get("ece_uncalibrated", t["ece"]),
                  t.get("ece_noise_floor", 0), "%.3f" % t["brier"] if "brier" in t else "-", t["aurc"],
                  (", within one level %s (MAE %.2f)" % (_pct(t["within_one"]), t["ordinal_mae"]))
                  if "ordinal_mae" in t else "",
                  (", AUROC %.3f" % t["auroc"]) if "auroc" in t else "")]
        if sel:
            ci = sel.get("accepted_ci95") or (None, None)
            L += ["", "Auto-accept: confidence ≥ %.2f. In cross-validation that answered %s of inputs at %s accuracy; "
                  "on test it answered %s (%d inputs) at %s accuracy (95%% CI %s–%s). Everything below the threshold "
                  "falls through to the LLM tier." % (
                      sel["threshold"], _pct(sel["cv_coverage"]), _pct(sel["cv_accepted_accuracy"]),
                      _pct(sel["coverage"]), sel["accepted"], _pct(sel["accepted_accuracy"]), _pct(ci[0]), _pct(ci[1]))]
        L += ["", "Status: %s. %s." % ("enabled" if r["enabled"] else "not enabled",
                                       r["enabled_reason"][0].upper() + r["enabled_reason"][1:])]
        fz = r.get("by_featurizer") or {}
        if len(fz) > 1:
            L += ["", "| Featurizer | Best CV accuracy | Its CV ECE | Featurize p50 | Features |", "|---|---|---|---|---|"]
            for name, d in sorted(fz.items()):
                L.append("| %s | %s | %.3f | %s | %d |" % (name, _pct(d["cv_accuracy"]), d["cv_ece"],
                                                           _us(d["featurize_p50_us"]), d["n_features"]))
        if r.get("confusions"):
            L += ["", "Most common test confusions: " + "; ".join(
                "%s → %s (%d)" % (c["true"], c["pred"], c["count"]) for c in r["confusions"]) + "."]
        L += ["", "<details><summary>All %d candidates (cross-validated)</summary>" % len(r["candidates"]), "",
              "| Featurizer | C | Class weight | Accuracy | ECE | Noise floor | Log-loss |", "|---|---|---|---|---|---|---|"]
        for c in sorted(r["candidates"], key=lambda c: (-c["cv"]["accuracy"], c["cv"]["nll"])):
            L.append("| %s | %g | %s | %s | %.3f | %.3f | %.3f |" % (
                c["featurizer"], c["C"], c["class_weight"], _pct(c["cv"]["accuracy"]), c["cv"]["ece"],
                c["cv"]["ece_noise_floor"], c["cv"]["nll"]))
        L += ["", "</details>"]

    notes = []
    for r in res:
        width = r["test"]["accuracy_ci95"][1] - r["test"]["accuracy_ci95"][0]
        if width > 0.15:
            notes.append("%s: only %d test examples, so its test accuracy is uncertain by about ±%.0f points (the "
                         "cross-validated figure rests on %d examples)." % (r["task"], r["test"]["n"], width * 50,
                                                                          r["cv"]["n"]))
        if not r["enabled"]:
            notes.append("%s stays on the LLM tier for now: %s." % (r["task"], r["enabled_reason"]))
    if res and all(r["test_source"] != "human gold labels" for r in res):
        notes.append("All tests are against teacher labels, not people. `kit.py review` builds a human gold set.")
    if any((r.get("result") or {}).get("mode") != "label" for r in runs):
        notes.append("Most training data is synthetic (written by the teacher model). Your real requests, issues "
                     "and diffs will look different; label some with `generate --from-file` or `--from-git` and "
                     "retrain to see how the numbers transfer.")
    if notes:
        L += ["", "## Caveats", ""] + ["- " + n for n in notes]
    L += ["", "_Generated %s._" % stamp()]
    text = "\n".join(L) + "\n"
    (paths.reports / "report.md").write_text(text, encoding="utf-8")
    return text
