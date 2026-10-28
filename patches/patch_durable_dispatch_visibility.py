#!/usr/bin/env python3
"""Expose only the durable-dispatch plugin toolset directly in Hermes sessions.

Hermes currently defers every plugin toolset by default and has no plugin metadata
opt-in for eager visibility. This idempotent host patch adds the already-enabled
``durable_dispatch`` toolset to the direct surface set; toolset authorization still
filters tools before tool search assembly.

Use: python3 patch_durable_dispatch_visibility.py --check | --apply [--agent-dir DIR]
Exit: 0 = present/applied; 1 = absent under --check; 2 = source anchor drift.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REL = Path('tools/tool_search.py')
OLD = '_DIRECT_SURFACE_TOOLSETS = CLIENT_SURFACE_TOOLSETS | TOOLSET_SESSION_PLATFORMS.keys()'
NEW = OLD + ' | {"durable_dispatch"}'
PROBE = 'TOOLSET_SESSION_PLATFORMS.keys() | {"durable_dispatch"}'


def agent_dir(value: str | None) -> Path:
    if value:
        return Path(value).expanduser()
    return Path(os.environ.get('HERMES_AGENT_DIR', Path.home() / '.hermes' / 'hermes-agent'))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true')
    group.add_argument('--apply', action='store_true')
    parser.add_argument('--agent-dir', default=None)
    args = parser.parse_args()
    target = agent_dir(args.agent_dir) / REL
    if not target.is_file():
        sys.exit(f'[FAIL] 找不到 {target}')
    source = target.read_text(encoding='utf-8')
    if PROBE in source:
        print('[PASS] durable_dispatch direct-surface 补丁在位')
        return 0
    if args.check:
        print('[FAIL] durable_dispatch direct-surface 补丁不在位')
        return 1
    if source.count(OLD) != 1:
        sys.exit(f'[FAIL] direct-surface 锚点命中 {source.count(OLD)} 次（应为 1），文件已漂移')
    updated = source.replace(OLD, NEW, 1)
    sibling = target.with_name(target.name + '.durable-dispatch-tmp')
    sibling.write_text(updated, encoding='utf-8')
    os.replace(sibling, target)
    if PROBE not in target.read_text(encoding='utf-8'):
        return 2
    print('[PASS] 已应用 durable_dispatch direct-surface 补丁')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
