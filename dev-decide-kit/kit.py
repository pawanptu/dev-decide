#!/usr/bin/env python3
"""dev-decide-kit: train fast, calibrated CPU models for dev-decide's learned tier.

  python3 kit.py plan                      what a generate run would do and how many model calls it needs
  python3 kit.py generate                  bootstrap labeled data with Claude (asks before spending)
  python3 kit.py generate --from-file F --family route|triage    label your own requests or issues
  python3 kit.py generate --from-git REPO                          label real commits for PR risk
  python3 kit.py review export|apply       correct a sample by hand to build a human gold test set
  python3 kit.py train                     train, calibrate and select one model per decision
  python3 kit.py report                    write reports/report.md
  python3 kit.py export --repo PATH        install the models into that repo's dev-decide
  python3 kit.py all                       generate, train and report in one go

Requirements: Python 3.8+, numpy, scipy, scikit-learn (training only; dev-decide itself stays stdlib),
and Claude Code (claude) or ANTHROPIC_API_KEY for the teacher.
"""
import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kitlib import common as C  # noqa: E402

DEFAULT_SIZES = {"route_per_class": 40, "triage_n": 320, "pr_n": 150}


def _families(s):
    fams = [f.strip() for f in (s or "").split(",") if f.strip()]
    bad = [f for f in fams if f not in C.FAMILIES]
    if bad:
        raise C.KitError("unknown family: %s (choose from %s)" % (", ".join(bad), ", ".join(C.FAMILIES)))
    return fams


def _routes(arg):
    if arg and arg != "auto":
        data = json.loads(Path(arg).read_text(encoding="utf-8"))
        routes = data.get("routes", data) if isinstance(data, dict) else data
    else:
        D = C.devdecide()
        root = D["dd"].project_root()
        routes = D["dd"].discover_routes(root, D["dd"].load_policy(root))
    routes = [{"name": r["name"], "kind": r.get("kind", "agent"), "description": r.get("description", "")}
              for r in routes if r.get("name") and r["name"] != "none"]
    if len(routes) < 2:
        raise C.KitError("routing needs at least two agents or skills; pass --routes routes.json")
    return routes


def _teachers(a, paths, budget):
    from kitlib import prompts, teacher
    b = teacher.Budget(budget)
    common = dict(backend=a.backend, budget=b, workers=a.workers, cache_dir=paths.cache, mock_fn=prompts.mock_reply)
    gen = teacher.Teacher(a.gen_model, temperature=1.0, **common)
    lab = teacher.Teacher(a.label_model, temperature=0.0, **common)
    return gen, lab, b


def _confirm(a, msg):
    if a.yes:
        return True
    if not sys.stdin.isatty():
        raise C.KitError("refusing to spend model calls without confirmation; re-run with --yes")
    return input(msg + " Proceed? [y/N] ").strip().lower() in ("y", "yes")


def cmd_plan(a, paths, show=True):
    from kitlib import generate as G
    fams = _families(a.families)
    routes = _routes(a.routes) if "route" in fams and not (a.from_file or a.from_git) else []
    sizes = {"route_per_class": a.route_per_class, "triage_n": a.triage_n, "pr_n": a.pr_n}
    est = G.estimate(fams, len(routes), sizes)
    total = sum(est.values())
    if show:
        print("Teacher: %s writes, %s labels (backend: %s)" % (a.gen_model, a.label_model, a.backend))
        if routes:
            print("Routes (%d + none): %s" % (len(routes), ", ".join(r["name"] for r in routes)))
        for f, n in est.items():
            what = {"route": "%d requests" % ((len(routes) + 1) * sizes["route_per_class"]),
                    "triage": "%d issues" % sizes["triage_n"], "pr": "%d diffs" % sizes["pr_n"]}[f]
            print("  %-7s %-14s ~%d model calls" % (f, what, n))
        print("Total: ~%d model calls (cached answers are free on re-runs)." % total)
    return est, routes, sizes


def cmd_generate(a, paths):
    from kitlib import generate as G
    if a.from_file or a.from_git:
        budget = a.budget or 200
        gen, lab, _ = _teachers(a, paths, budget)
        if a.from_file:
            fam = a.family or "triage"
            texts = Path(a.from_file).read_text(encoding="utf-8").splitlines()
            if Path(a.from_file).suffix == ".jsonl":
                texts = [json.loads(l).get("text", "") for l in texts if l.strip()]
            routes = _routes(a.routes) if fam == "route" else []
            if not _confirm(a, "Label %d texts for %s with %s (~%d calls)." % (
                    len(texts), fam, a.label_model, math.ceil(len(texts) / 25))):
                return 1
            res = G.label_texts(lab, fam, texts, routes, a.seed, paths)
        else:
            if not _confirm(a, "Label up to %d commits from %s with %s (~%d calls)." % (
                    a.pr_n, a.from_git, a.label_model, math.ceil(a.pr_n / 8))):
                return 1
            res = G.label_commits(lab, a.from_git, a.pr_n, a.seed, paths)
        C.append_jsonl(paths.runs, [{"created": C.stamp(), "result": res, "label": lab.summary()}])
        print(json.dumps(res, indent=1))
        return 0
    est, routes, sizes = cmd_plan(a, paths)
    budget = a.budget or int(sum(est.values()) * 1.15) + 2
    if not _confirm(a, "Hard cap: %d calls." % budget):
        return 1
    gen, lab, b = _teachers(a, paths, budget)
    for fam in _families(a.families):
        before = (gen.summary(), lab.summary())
        if fam == "route":
            res = G.synth_route(gen, lab, routes, sizes["route_per_class"], a.seed, paths)
        elif fam == "triage":
            res = G.synth_triage(gen, lab, sizes["triage_n"], a.seed, paths)
        else:
            res = G.synth_pr(gen, lab, sizes["pr_n"], a.seed, paths)
        g, l = gen.summary(), lab.summary()
        delta = lambda now, was: dict(now, calls=now["calls"] - was["calls"], cost_usd=round(now["cost_usd"] - was["cost_usd"], 4))
        C.append_jsonl(paths.runs, [{"created": C.stamp(), "result": res, "gen": delta(g, before[0]),
                                     "label": delta(l, before[1])}])
        agree = ", ".join("%s %d/%d" % (t, s.get("agree", 0), s.get("agree", 0) + s.get("disagree", 0))
                          for t, s in res["agreement"].items())
        print("%-7s %d new examples (%d duplicates skipped); writer and labeler agree: %s" % (
            fam, res["new"], res["duplicates"], agree))
    g, l = gen.summary(), lab.summary()
    print("Model calls: %d (%d cached), failures %d, cost about $%.3f." % (
        g["calls"] + l["calls"], g["cache_hits"] + l["cache_hits"], g["failures"] + l["failures"],
        g["cost_usd"] + l["cost_usd"]))
    return 0


def cmd_train(a, paths):
    from kitlib import train as T
    cfg = T.Config(target=a.target, min_coverage=a.min_coverage, ece_max=a.ece_max, min_n=a.min_n, seed=a.seed,
                   min_confidence=a.min_confidence)
    T.train_families(paths, _families(a.families), cfg)
    return cmd_report(a, paths, quiet=True)


def cmd_report(a, paths, quiet=False):
    from kitlib import report as R
    text = R.build(paths)
    if text is None:
        raise C.KitError("nothing trained yet: run `kit.py train` first")
    print("Report: %s" % (paths.reports / "report.md"))
    if not quiet:
        print(text)
    return 0


def cmd_export(a, paths):
    from kitlib import export as E
    only = set(_t.strip() for _t in a.only.split(",")) if a.only else None
    out = E.export(paths, a.repo, only=only, force=a.force)
    print("Installed %d model(s) into %s" % (len(out["tasks"]), out["dest"]))
    return 0


def cmd_review(a, paths):
    from kitlib import review as RV
    if a.action == "export":
        out = a.out or "review-%s.csv" % a.family
        n = RV.export_review(paths, a.family, a.n, a.seed, out)
        print("Wrote %d examples to %s. Fill in the correct_* columns (ok, a label, or leave empty), then run "
              "`kit.py review apply %s --family %s`." % (n, out, out, a.family))
    else:
        if not a.file:
            raise C.KitError("review apply needs the CSV path")
        added, skipped = RV.apply_review(paths, a.family, a.file)
        print("Added %d gold examples (%d cells skipped). Re-run `kit.py train`." % (added, skipped))
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # a Windows console cannot print every character in the report
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "generate", "review", "train", "report", "export", "all"])
    ap.add_argument("action", nargs="?", help="for review: export or apply")
    ap.add_argument("file", nargs="?", help="for review apply: the CSV")
    ap.add_argument("--data", default="dev-decide-kit-data", help="working folder (default: ./dev-decide-kit-data)")
    ap.add_argument("--devdecide", help="dev-decide scripts folder (default: the bundled dev-decide-v5)")
    ap.add_argument("--families", default="route,pr,triage")
    ap.add_argument("--routes", default="auto", help="auto (your agents and skills) or a JSON file")
    ap.add_argument("--route-per-class", type=int, default=DEFAULT_SIZES["route_per_class"])
    ap.add_argument("--triage-n", type=int, default=DEFAULT_SIZES["triage_n"])
    ap.add_argument("--pr-n", type=int, default=DEFAULT_SIZES["pr_n"])
    ap.add_argument("--gen-model", default="haiku")
    ap.add_argument("--label-model", default="sonnet")
    ap.add_argument("--backend", default="auto", choices=["auto", "cli", "api", "mock"])
    ap.add_argument("--budget", type=int, help="hard cap on model calls (default: the plan + 15%%)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--yes", action="store_true", help="do not ask before spending model calls")
    ap.add_argument("--from-file")
    ap.add_argument("--from-git")
    ap.add_argument("--family", default="triage")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--out")
    ap.add_argument("--target", type=float, default=0.9, help="accuracy auto-accepted answers must reach")
    ap.add_argument("--min-coverage", type=float, default=0.1)
    ap.add_argument("--min-confidence", type=float, default=0.6,
                    help="the learned tier never auto-answers below this confidence (dev-decide abstains below 0.6)")
    ap.add_argument("--ece-max", type=float, default=0.05)
    ap.add_argument("--min-n", type=int, default=40)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--only")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    paths = C.Paths(a.data)
    try:
        C.devdecide(a.devdecide)
        if a.command == "plan":
            cmd_plan(a, paths)
            return 0
        if a.command == "generate":
            return cmd_generate(a, paths)
        if a.command == "review":
            if a.action not in ("export", "apply"):
                raise C.KitError("review needs export or apply")
            return cmd_review(a, paths)
        if a.command == "train":
            return cmd_train(a, paths)
        if a.command == "report":
            return cmd_report(a, paths)
        if a.command == "export":
            return cmd_export(a, paths)
        rc = cmd_generate(a, paths)
        return rc or cmd_train(a, paths)
    except C.KitError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
