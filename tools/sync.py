#!/usr/bin/env python3
"""Copy the engine (dev-decide-v5/skill) into the plugin, so the two never drift.

The server image is built from dev-decide-v5/skill directly (see the root Dockerfile).

  python3 tools/sync.py
"""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dev-decide-v5" / "skill"
TARGETS = [ROOT / "plugin" / "skills" / "dev-decide"]

for dest in TARGETS:
    if dest.exists():
        shutil.rmtree(str(dest))
    shutil.copytree(str(SRC), str(dest), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print("synced", dest.relative_to(ROOT))
