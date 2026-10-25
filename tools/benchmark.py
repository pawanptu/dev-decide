#!/usr/bin/env python3
"""One command: label (optional), train with calibration, evaluate, and write the results table.

  python3 tools/benchmark.py                                  # train + evaluate on the shipped data (no model calls)
  python3 tools/benchmark.py --label-issues 200 --label-diffs 100   # first write and label new examples (costs calls)
  python3 tools/benchmark.py --data my-data --families triage --out results.md

Steps:
  1. label      kit.py generate: Haiku writes examples, Sonnet labels them blind (only with --label-*; spends calls)
  2. train      kit.py train: per decision, 5-fold cross-validated model selection, calibrated by temperature
                scaling, with a global and per-class confidence threshold measured on held-out data
  3. evaluate   held-out accuracy and calibration (ECE) per decision, routing hints without a trained model
                (tools/eval_router.py), short issue reports never trained on (tools/eval_issue_type.py), and speed
  4. report     a Markdown table, printed and written to <data>/reports/benchmark.md (or --out)

Training needs numpy, scipy and scikit-learn in the Python that runs this script.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KIT = ROOT / "dev-decide-kit" / "kit.py"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "dev-decide-v5" / "skill" / "scripts"))


def run(args, what):
    print("== %s: %s" % (what, " ".join(str(a) for a in args[1:])), flush=True)
    t0 = time.time()
    rc = subprocess.call(args)
    if rc != 0:
        raise SystemExit("%s failed (exit %d)" % (what, rc))
    return time.time() - t0


def pct(x):
    return "-" if x is None else "%.1f%%" % (100 * x)


def speed():
    """Median time of one local triage decision, in-process (models loaded, as in the server)."""
    import engine
    import tempfile
    repo = Path(tempfile.mkdtemp(prefix="dd-bench-"))
    subprocess.call(["git", "init", "-q", str(repo)])
    spec = {"questions": ["issue_type"], "state": "Login returns 500 for users with a plus sign in their email",
            "mode": "local"}
    engine.decide(spec=dict(spec), cwd=str(repo), ephemeral=True)  # warm up
    times = []
    for _ in range(30):
        t0 = time.perf_counter()
        engine.decide(spec=dict(spec), cwd=str(repo), ephemeral=True)
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    return times[len(times) // 2]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(ROOT / "dev-decide-kit" / "pretrained"), help="kit data folder")
    ap.add_argument("--families", default="triage,pr,route")
    ap.add_argument("--label-issues", type=int, default=0)
    ap.add_argument("--label-diffs", type=int, default=0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--skip-train", action="store_true", help="evaluate the models already in --data")
    ap.add_argument("--out")
    args = ap.parse_args()
    data = Path(args.data).resolve()
    py = sys.executable
    took = {}

    if args.label_issues or args.label_diffs:
        fams = ",".join(f for f, n in (("triage", args.label_issues), ("pr", args.label_diffs)) if n)
        took["label"] = run([py, str(KIT), "generate", "--data", str(data), "--families", fams,
                             "--triage-n", str(args.label_issues), "--pr-n", str(args.label_diffs),
                             "--seed", str(args.seed + 1), "--yes"], "label")
    if not args.skip_train:
        try:
            import numpy  # noqa: F401
            import scipy  # noqa: F401
            import sklearn  # noqa: F401
        except ImportError:
            raise SystemExit("training needs numpy, scipy and scikit-learn: pip install numpy scipy scikit-learn")
        took["train"] = run([py, str(KIT), "train", "--data", str(data), "--families", args.families,
                             "--seed", str(args.seed)], "train + calibrate")

    report = json.loads((data / "reports" / "train.json").read_text(encoding="utf-8"))
    lines = ["# dev-decide benchmark", "",
             "Data: `%s`. Trained %s. Held-out test splits are never used for any choice; accuracy is on them."
             % (data.name, report.get("created", "?")), "",
             "| Decision | Features | Test accuracy | ECE (calibrated, was) | Answered locally | Right when answered | Status |",
             "|---|---|---|---|---|---|---|"]
    for r in report["results"]:
        if r.get("status") != "trained":
            lines.append("| `%s` | - | - | - | - | - | skipped: %s |" % (r["task"], r.get("reason", "")))
            continue
        t, sel = r["test"], r.get("selective") or {}
        lines.append("| `%s` | %s | %s | %.3f (%.3f) | %s | %s | %s |" % (
            r["task"], r["winner"]["featurizer"], pct(t["accuracy"]), t["ece"], t.get("ece_uncalibrated", t["ece"]),
            pct(sel.get("coverage")), pct(sel.get("accepted_accuracy")),
            "**enabled**" if r["enabled"] else "not yet (%s)" % r.get("enabled_reason", "")))

    import eval_router
    import eval_issue_type
    routes, rows = eval_router.load("test")
    rr = eval_router.evaluate(routes, rows)
    lines += ["", "**Routing hint without a trained model** (held-out half of %d labeled requests): accuracy %s, "
              "hint precision %s at %s coverage; with one unrelated skill, false hints %s."
              % (rr["n"], pct(rr["accuracy"]), pct(rr["hint_precision"]), pct(rr["hint_coverage"]),
                 pct(rr["one_skill_false_hints"]))]
    if (data / "models" / "issue_type.json").exists():
        ri = eval_issue_type.evaluate(data / "models")
        lines += ["", "**Short issue reports never trained on** (%d): accuracy %s; answered locally %s, right when "
                  "answered %s." % (ri["n"], pct(ri["accuracy"]), pct(ri["answered"]), pct(ri["answered_accuracy"]))]
    lines += ["", "**Speed:** one local triage decision takes %.1f ms (median of 30, in-process, starter models)."
              % speed()]
    if took:
        lines += ["", "Run time: " + ", ".join("%s %.0f s" % kv for kv in took.items()) + "."]
    text = "\n".join(lines) + "\n"
    out = Path(args.out) if args.out else data / "reports" / "benchmark.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print()
    print(text)
    print("Written to %s" % out)


if __name__ == "__main__":
    main()
