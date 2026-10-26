#!/usr/bin/env python3
"""Check that a Hermes install exposes the public surfaces completion backfill needs.

The plugin deliberately does not patch the host. It relies on two documented public
capabilities instead, and this harness fails loudly when a host predates either:

* ``PluginContext.inject_message`` plus the ``allow_gateway_injection`` consent gate,
  which is how a completion re-enters the conversation that submitted the work.
* The bound session variables (``HERMES_SESSION_KEY`` and friends), which are the only
  route recorded at submit time. Without them a completion must be parked, not guessed.

Use: python3 verify_durable_dispatch_backfill.py [--agent-dir DIR]
Exit: 0 = all required surfaces present; 1 = a surface is missing.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

CHECKS = (
    ('hermes_cli/plugins.py',
     ('def inject_message(', '_gateway_injection_allowed', 'allow_gateway_injection')),
    ('hermes_cli/plugins_injection.py',
     ('def inject_plugin_message(', 'session_key', 'origin')),
    ('gateway/session_context.py',
     ('def get_session_env(', 'HERMES_SESSION_KEY', 'HERMES_SESSION_PLATFORM',
      'HERMES_SESSION_ID')),
)


def agent_dir(value: str | None) -> Path:
    if value:
        return Path(value).expanduser()
    return Path(os.environ.get('HERMES_AGENT_DIR', Path.home() / '.hermes' / 'hermes-agent'))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--agent-dir', default=None)
    args = parser.parse_args()
    root = agent_dir(args.agent_dir)
    if not root.is_dir():
        print(f'[FAIL] Hermes install not found: {root}')
        return 1
    missing = []
    for relative, anchors in CHECKS:
        target = root / relative
        if not target.is_file():
            missing.append(f'{relative}: file missing')
            continue
        source = target.read_text(encoding='utf-8')
        for anchor in anchors:
            if anchor not in source:
                missing.append(f'{relative}: missing {anchor!r}')
    if missing:
        for item in missing:
            print('[FAIL] ' + item)
        print('[FAIL] host cannot route a durable completion back to its origin')
        return 1
    print('[PASS] inject_message + consent gate + bound session route are available')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
