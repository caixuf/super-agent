#!/usr/bin/env python3
"""Prefer-D7 thin smoke runner — pytest the wrap lifecycle module.

Fake Feishu / fake clock only. Does not invent live delivery.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    target = root / "tests" / "test_prefer_d7_wrap_smoke.py"
    cmd = [sys.executable, "-m", "pytest", "-q", str(target)]
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(root))


if __name__ == "__main__":
    raise SystemExit(main())
