"""Human review: correct a sample of teacher labels to build a gold test set.

  kit.py review export --family triage --n 60     writes a CSV to fill in
  kit.py review apply review-triage.csv           turns your answers into gold labels

In the CSV, leave a correct_<task> cell empty to skip it, write ok to confirm the
teacher's label, or write the right label. Gold examples never train; they become
the test set, so accuracy is then measured against people, not against the teacher.
"""
import csv
import random
import time
from collections import defaultdict

from . import prompts as P
from .common import FAMILIES, KitError, append_jsonl, read_jsonl

PREVIEW = 700


def _classes(task):
    return {"issue_type": set(P.ISSUE_TYPES), "severity": set(P.SEVERITIES), "domain": set(P.DOMAINS),
            "risk": {"1", "2", "3", "4", "5"}, "breaking_change": {"true", "false"}}.get(task)


def export_review(paths, family, n, seed, out_path):
    tasks = FAMILIES[family]["tasks"]
    examples = [e for e in read_jsonl(paths.examples(family)) if any(t in e.get("teacher", {}) for t in tasks)]
    done = set(g["id"] for g in read_jsonl(paths.gold(family)))
    examples = [e for e in examples if e["id"] not in done]
    if not examples:
        raise KitError("no labeled %s examples to review" % family)
    by_label = defaultdict(list)
    for e in examples:
        by_label[(e.get("teacher", {}).get(tasks[0]) or ["?"])[0]].append(e)
    rnd = random.Random(seed)
    for v in by_label.values():
        rnd.shuffle(v)
    picked = []
    while len(picked) < n and any(by_label.values()):
        for k in sorted(by_label):
            if by_label[k] and len(picked) < n:
                picked.append(by_label[k].pop())
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "input"] + [c for t in tasks for c in ("teacher_" + t, "correct_" + t)])
        for e in picked:
            inp = e["input"].get("text") or e["input"].get("diff", "")
            row = [e["id"], inp[:PREVIEW].replace("\n", " ⏎ ")]
            for t in tasks:
                row += [(e.get("teacher", {}).get(t) or [""])[0], ""]
            w.writerow(row)
    return len(picked)


def apply_review(paths, family, csv_path):
    tasks = FAMILIES[family]["tasks"]
    known = {e["id"]: e for e in read_jsonl(paths.examples(family))}
    rows, skipped = [], 0
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            ex = known.get(r.get("id"))
            if not ex:
                skipped += 1
                continue
            labels = {}
            for t in tasks:
                val = (r.get("correct_" + t) or "").strip()
                if not val:
                    continue
                if val.lower() in ("ok", "y", "yes", "correct") and t != "breaking_change":
                    val = r.get("teacher_" + t, "")
                elif t == "breaking_change" and val.lower() in ("ok",):
                    val = r.get("teacher_" + t, "")
                elif t == "breaking_change":
                    val = "true" if val.lower() in ("true", "yes", "y", "1") else "false"
                if val in (_classes(t) or {val}):
                    labels[t] = val
                else:
                    skipped += 1
            if labels:
                rows.append({"id": ex["id"], "labels": labels, "source": "human", "ts": int(time.time())})
    append_jsonl(paths.gold(family), rows)
    return len(rows), skipped
