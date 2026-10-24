"""Shared plumbing: decision families, dev-decide import, data paths and JSONL helpers."""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent

# The three decision families and the dev-decide presets each one trains.
FAMILIES = {
    "route": {"tasks": ["route"], "input": "text"},
    "pr": {"tasks": ["risk", "breaking_change"], "input": "diff"},
    "triage": {"tasks": ["issue_type", "severity", "domain"], "input": "text"},
}
TASK_FAMILY = {t: fam for fam, spec in FAMILIES.items() for t in spec["tasks"]}
TASK_KIND = {"route": "choose", "risk": "score", "breaking_change": "verify",
             "issue_type": "choose", "severity": "choose", "domain": "choose"}
# Ordered classes for tasks where "how far off" matters, not just right or wrong.
ORDINAL = {
    "risk": ["1", "2", "3", "4", "5"],
    "severity": ["sev1_outage", "sev2_major", "sev3_minor", "sev4_cosmetic", "not_a_problem"],
}


class KitError(Exception):
    pass


def log(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- dev-decide

def find_devdecide(explicit=None):
    """Locate dev-decide's scripts folder (needs v3.1+ for the f2 featurizer and binary models)."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("DEV_DECIDE_SCRIPTS"):
        candidates.append(Path(os.environ["DEV_DECIDE_SCRIPTS"]))
    candidates += [
        KIT_DIR.parent / "dev-decide-v5" / "skill" / "scripts",
        KIT_DIR.parent / "dev-decide-v4" / "skill" / "scripts",
        KIT_DIR.parent / "dev-decide-v3.1" / "skill" / "scripts",
        Path.cwd() / ".claude" / "skills" / "dev-decide" / "scripts",
        Path.home() / ".claude" / "skills" / "dev-decide" / "scripts",
    ]
    for c in candidates:
        if (c / "fastmodel.py").is_file() and (c / "signals.py").is_file():
            return c.resolve()
    raise KitError("dev-decide scripts not found. Pass --devdecide /path/to/dev-decide/skill/scripts "
                   "(the bundled dev-decide-v5 folder works).")


_DD = {}


def devdecide(explicit=None):
    """Import dev-decide's modules once. Returns a namespace dict."""
    if _DD:
        return _DD
    path = find_devdecide(explicit)
    sys.path.insert(0, str(path))
    import ddcore
    import fastmodel
    import inputs
    import signals
    if not hasattr(fastmodel, "FEATURIZERS") or not hasattr(fastmodel, "write_binary"):
        raise KitError("%s is dev-decide %s; the kit needs dev-decide 3.1 or later (bundled in this zip)."
                       % (path, getattr(ddcore, "VERSION", "?")))
    _DD.update(path=path, dd=ddcore, fm=fastmodel, inputs=inputs, signals=signals)
    return _DD


def make_item(example):
    """Build the same dev-decide Item the engine builds at decision time."""
    D = devdecide()
    inp = example["input"]
    item = D["inputs"].Item(example.get("id"))
    if inp.get("kind") == "diff":
        item.kind = "diff"
        item.diff_text = inp.get("diff", "")
        item.files = D["inputs"].parse_diff(item.diff_text)
        if inp.get("text"):
            item.kind, item.text = "mixed", inp["text"]
    else:
        item.kind, item.text = "text", inp.get("text", "")
    return item


def facts_for(item):
    D = devdecide()
    F, _ = D["signals"].analyze(item, [])
    return F


# ---------------------------------------------------------------- data folder

class Paths(object):
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()

    def family(self, fam):
        p = self.root / "data" / fam
        p.mkdir(parents=True, exist_ok=True)
        return p

    def examples(self, fam):
        return self.family(fam) / "examples.jsonl"

    def gold(self, fam):
        return self.family(fam) / "gold.jsonl"

    @property
    def cache(self):
        p = self.root / "cache"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def models(self):
        p = self.root / "models"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def reports(self):
        p = self.root / "reports"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def runs(self):
        return self.root / "runs.jsonl"


def read_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return rows


def append_jsonl(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_json(path, obj, indent=2):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return default


def sha(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()


def stamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")
