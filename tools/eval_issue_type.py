#!/usr/bin/env python3
"""Check an issue_type model on short reports it never trained on.

  python3 tools/eval_issue_type.py [models_dir ...]

models_dir holds issue_type.json and issue_type.bin (default: the starter models shipped with the skill).
For each folder it prints accuracy on dev-decide-kit/pretrained/data/triage/short_test.jsonl, how many
answers clear the model's own threshold (what dev-decide would answer locally) and how accurate those are.
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dev-decide-v5" / "skill" / "scripts"))
import fastmodel as fm  # noqa: E402
import inputs  # noqa: E402
import signals  # noqa: E402

TEST = ROOT / "dev-decide-kit" / "pretrained" / "data" / "triage" / "short_test.jsonl"


def load_model(folder):
    data = json.loads((Path(folder) / "issue_type.json").read_text(encoding="utf-8"))
    data["_idx"], data["_w"] = fm.read_binary(Path(folder) / "issue_type.bin", len(data["classes"]))
    return data


def evaluate(folder):
    model = load_model(folder)
    threshold = float(model.get("threshold", 0.95))
    rows = [json.loads(l) for l in TEST.read_text(encoding="utf-8").splitlines() if l.strip()]
    right = answered = answered_right = 0
    wrong = Counter()
    for r in rows:
        item = inputs.Item(None)
        item.kind, item.text = "text", r["input"]["text"]
        F, _ = signals.analyze(item, [])
        probs = fm.predict(model, fm.Features(item, F).get(model.get("featurizer") or "f1"))
        best = max(probs, key=probs.get)
        y = r["labels"]["issue_type"]
        right += best == y
        if best != y:
            wrong[(y, best)] += 1
        if probs[best] >= max(threshold, fm.class_floor(model, best)):
            answered += 1
            answered_right += best == y
    n = len(rows)
    return {"n": n, "accuracy": right / float(n), "answered": answered / float(n),
            "answered_accuracy": answered_right / float(answered) if answered else None,
            "top_confusions": wrong.most_common(4)}


if __name__ == "__main__":
    folders = sys.argv[1:] or [str(ROOT / "dev-decide-v5" / "skill" / "starter-models")]
    for folder in folders:
        r = evaluate(folder)
        print("%s\n  short reports: n=%d  accuracy %.1f%%  answered locally %.1f%%  accuracy when answered %s"
              % (folder, r["n"], 100 * r["accuracy"], 100 * r["answered"],
                 "%.1f%%" % (100 * r["answered_accuracy"]) if r["answered_accuracy"] is not None else "-"))
        print("  most common mistakes (true -> predicted): %s" % r["top_confusions"])
