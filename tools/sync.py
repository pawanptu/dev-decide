#!/usr/bin/env python3
"""Copy the engine (dev-decide-v5/skill) into the plugin, so the two never drift.

The server image is built from dev-decide-v5/skill directly (see the root Dockerfile).

  python3 tools/sync.py
"""
import filecmp
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dev-decide-v5" / "skill"
TARGETS = [ROOT / "plugin" / "skills" / "dev-decide"]


def skipped(path):
    return path.name == "__pycache__" or path.suffix == ".pyc"


def mirror(src, dest):
    """Make dest an exact copy of src, rewriting only the files that differ. A running hint server keeps the meaning
    model's file mapped, which Windows will not let anyone delete; an unchanged file is never touched."""
    dest.mkdir(parents=True, exist_ok=True)
    names = {p.name for p in src.iterdir() if not skipped(p)}
    for p in dest.iterdir():
        if p.name not in names:
            shutil.rmtree(str(p)) if p.is_dir() else p.unlink()
    for name in sorted(names):
        s, d = src / name, dest / name
        if s.is_dir():
            if d.exists() and not d.is_dir():
                d.unlink()
            mirror(s, d)
            continue
        if d.is_dir():
            shutil.rmtree(str(d))
        if not d.exists() or not filecmp.cmp(str(s), str(d), shallow=False):
            shutil.copy2(str(s), str(d))


# The lean plugin (dev-decide-lite) carries only the token guard and the savings report.
LITE = ROOT / "plugin-lite" / "scripts"
LITE_FILES = ("run.sh", "guard.py", "tokens.py", "savings.py", "lite.py")


def sync_lite():
    LITE.mkdir(parents=True, exist_ok=True)
    for p in LITE.iterdir():
        if p.name not in LITE_FILES and not skipped(p):
            shutil.rmtree(str(p)) if p.is_dir() else p.unlink()
    for name in LITE_FILES:
        s, d = SRC / "scripts" / name, LITE / name
        if not d.exists() or not filecmp.cmp(str(s), str(d), shallow=False):
            shutil.copy2(str(s), str(d))


for dest in TARGETS:
    mirror(SRC, dest)
    print("synced", dest.relative_to(ROOT))
sync_lite()
print("synced", LITE.relative_to(ROOT))
