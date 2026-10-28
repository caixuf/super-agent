#!/usr/bin/env python3
"""Structural verifier for patches/patch_durable_dispatch_visibility.py."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROBE = 'TOOLSET_SESSION_PLATFORMS.keys() | {"durable_dispatch"}'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent-dir', default=None)
    args = parser.parse_args()
    root = Path(args.agent_dir).expanduser() if args.agent_dir else Path(
        os.environ.get('HERMES_AGENT_DIR', Path.home() / '.hermes' / 'hermes-agent'))
    target = root / 'tools/tool_search.py'
    if not target.is_file():
        sys.exit(f'[FAIL] 找不到 {target}')
    source = target.read_text(encoding='utf-8')
    present = source.count(PROBE) == 1
    print('[PASS] direct-surface allowlist has durable_dispatch' if present else
          '[FAIL] direct-surface allowlist missing or duplicated')
    return 0 if present else 1


if __name__ == '__main__':
    raise SystemExit(main())
