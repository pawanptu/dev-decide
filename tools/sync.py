#!/usr/bin/env python3
"""Copy the engine (dev-decide-v5/skill) into the plugin and the deploy folder, so the three never drift.

  python3 tools/sync.py
"""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dev-decide-v5" / "skill"
TARGETS = [ROOT / "plugin" / "skills" / "dev-decide", ROOT / "dev-decide-server" / "skill"]

for dest in TARGETS:
    if dest.exists():
        shutil.rmtree(str(dest))
    shutil.copytree(str(SRC), str(dest), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print("synced", dest.relative_to(ROOT))
