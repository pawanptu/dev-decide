"""Install trained models into a repository's dev-decide learned tier, then prove they load and agree."""
import shutil
from pathlib import Path

from .common import KitError, devdecide, log, read_json, write_json


def installed_devdecide(repo=None):
    """dev-decide installs that would read the models, with their versions."""
    found, seen = [], set()
    bases = ([Path(repo).resolve() / ".claude"] if repo else []) + [Path.cwd() / ".claude", Path.home() / ".claude"]
    for base in bases:
        dd = base / "skills" / "dev-decide" / "scripts" / "ddcore.py"
        if str(dd.resolve()) in seen:
            continue
        seen.add(str(dd.resolve()))
        if dd.is_file():
            version = "?"
            for line in dd.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("VERSION"):
                    version = line.split("=", 1)[1].strip().strip("\"'")
                    break
            found.append((str(dd.parent.parent), version))
    return found


def _ok_version(v):
    try:
        major, minor = (int(x) for x in v.split(".")[:2])
        return (major, minor) >= (3, 1)
    except ValueError:
        return False


def export(paths, repo, only=None, force=False):
    D = devdecide()
    fm = D["fm"]
    train = read_json(paths.reports / "train.json")
    if not train:
        raise KitError("nothing trained yet: run `kit.py train` first")
    installs = installed_devdecide(repo)
    old = [p for p, v in installs if not _ok_version(v)]
    if old and not force:
        raise KitError("dev-decide older than 3.1 is installed at %s and cannot read these models. Install the bundled "
                       "dev-decide-v5 (python3 install.py) or pass --force." % ", ".join(old))
    dest = Path(repo).resolve() / ".claude" / "dev-decide" / "models"
    dest.mkdir(parents=True, exist_ok=True)
    index = read_json(dest / "index.json", {}) or {}
    done = []
    for r in train["results"]:
        task = r["task"]
        if r["status"] != "trained" or (only and task not in only):
            continue
        for ext in (".json", ".bin"):
            shutil.copy2(str(paths.models / (task + ext)), str(dest / (task + ext)))
        sel = r.get("selective") or {}
        index[task] = {"enabled": bool(r["enabled"] or force), "featurizer": r["winner"]["featurizer"],
                       "kind": r["kind"], "classes": r["classes"], "threshold": sel.get("threshold", 1.01),
                       "temperature": r["winner"]["temperature"], "source": "dev-decide-kit",
                       "trained": train["created"], "test_accuracy": round(r["test"]["accuracy"], 4),
                       "test_ece": round(r["test"]["ece"], 4), "coverage": sel.get("coverage"),
                       "accepted_accuracy": sel.get("accepted_accuracy"), "reason": r["enabled_reason"],
                       "test_source": r.get("test_source")}
        done.append(task)
    write_json(dest / "index.json", index)

    reg = fm.Registry(str(Path(repo).resolve()))
    checks = {}
    for task in done:
        model = reg.load(task)
        if not model:
            checks[task] = "failed to load"
            continue
        worst = 0.0
        for case in read_json(paths.models / ("%s.parity.json" % task), []):
            got = fm.predict(model, {int(k): v for k, v in case["vec"].items()})
            worst = max(worst, max(abs(got[c] - p) for c, p in zip(model["classes"], case["probs"])))
        checks[task] = worst
    for task in done:
        state = "enabled" if index[task]["enabled"] else "installed, not enabled (%s)" % index[task]["reason"]
        c = checks[task]
        parity = ("parity ok (max diff %.1e)" % c) if isinstance(c, float) and c < 1e-4 else "PARITY PROBLEM: %s" % c
        log("  %-16s %s; %s" % (task, state, parity))
    unverified = [t for t in done if index[t]["enabled"] and index[t].get("test_source") != "human gold labels"]
    if unverified:
        log("note: %s %s tested against the teacher's labels on bootstrapped examples only, which can be optimistic. "
            "Check a sample of your real inputs (kit.py review export) before relying on the automatic answers."
            % (", ".join(unverified), "was" if len(unverified) == 1 else "were"))
    if not installs:
        log("note: no dev-decide install found in %s, this folder or ~/.claude; install the bundled dev-decide-v5 "
            "to use these here (a server that runs serve.py from its own copy needs no install)" % Path(repo).resolve())
    return {"dest": str(dest), "tasks": done, "parity": checks}
