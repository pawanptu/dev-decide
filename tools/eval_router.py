#!/usr/bin/env python3
"""Measure the zero-shot router (no trained model) on the kit's labeled routing requests.

  python3 tools/eval_router.py [--split dev|test|all]

Reports top-1 accuracy, and for the routing hint (min confidence 0.6, margin 0.2): how often it speaks
(coverage) and how often it is right when it does (precision). Most people have only a few agents and skills,
so it also tries each request with one skill installed: the right one (the hint should fire: "one-skill
recall") and a random wrong one (it should stay silent: "one-skill false hints"). Examples are split by a
stable hash so tuning on "dev" and reporting on "test" never mixes them.
"""
import argparse
import json
import random
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dev-decide-v5" / "skill" / "scripts"))
import router  # noqa: E402

DATA = ROOT / "dev-decide-kit" / "pretrained"


def load(split):
    routes = json.loads((DATA / "routes.json").read_text(encoding="utf-8"))["routes"]
    rows = []
    for line in (DATA / "data" / "route" / "examples.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        y = (r.get("labels") or {}).get("route")
        if not y:
            continue
        part = "dev" if zlib.crc32(r["id"].encode()) % 2 == 0 else "test"
        if split in ("all", part):
            rows.append((r["input"]["text"], y))
    return routes, rows


def evaluate(routes, rows, min_conf=0.6, min_margin=0.2):
    right = spoke = spoke_right = 0
    rec_n = rec = fp = 0
    rnd = random.Random(7)
    byname = {r["name"]: r for r in routes}
    for text, y in rows:
        if y in byname:
            rec_n += 1
            rec += bool(router.best_route(text, [byname[y]], min_confidence=min_conf, min_margin=min_margin))
        wrong = byname[rnd.choice([n for n in byname if n != y])]
        fp += bool(router.best_route(text, [wrong], min_confidence=min_conf, min_margin=min_margin))
        res = router.route(text, routes)
        best = res[2] if res else "none"
        right += best == y
        hint = router.best_route(text, routes, min_confidence=min_conf, min_margin=min_margin)
        if hint:
            spoke += 1
            spoke_right += hint[0] == y
    n = len(rows)
    return {"n": n, "accuracy": right / float(n), "hint_coverage": spoke / float(n),
            "hint_precision": spoke_right / float(spoke) if spoke else None,
            "one_skill_recall": rec / float(rec_n) if rec_n else None, "one_skill_false_hints": fp / float(n)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="all", choices=["dev", "test", "all"])
    args = ap.parse_args()
    routes, rows = load(args.split)
    r = evaluate(routes, rows)
    print("%s: n=%d  accuracy %.1f%%  hint coverage %.1f%%  hint precision %s  one-skill recall %.1f%%  "
          "one-skill false hints %.1f%%" % (
              args.split, r["n"], 100 * r["accuracy"], 100 * r["hint_coverage"],
              "%.1f%%" % (100 * r["hint_precision"]) if r["hint_precision"] is not None else "-",
              100 * (r["one_skill_recall"] or 0), 100 * r["one_skill_false_hints"]))
