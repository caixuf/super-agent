#!/usr/bin/env python3
"""Observe fail-open memory prefetch exceptions without logging user/provider text."""
import argparse
from pathlib import Path

OLD = '''    with suppress(Exception):
        if not is_trivial_prompt(_query):
            ext_prefetch_cache = agent._memory_manager.prefetch_all(_query, session_id=agent.session_id) or ""
'''
NEW = '''    try:
        if not is_trivial_prompt(_query):
            ext_prefetch_cache = agent._memory_manager.prefetch_all(_query, session_id=agent.session_id) or ""
    except Exception as exc:
        logger.warning('External memory prefetch failed (turn continues): %s', type(exc).__name__)
'''


def transform(source):
    if NEW in source:
        return source
    if source.count(OLD) != 1:
        raise ValueError('memory prefetch source anchor drifted')
    return source.replace(OLD, NEW, 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-root', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    path = Path(args.hermes_root)/'agent/turn_context.py'
    before = path.read_text()
    after = transform(before)
    if before == after:
        print('memory prefetch observation already installed')
        return 0
    if not args.apply:
        print('memory prefetch observation needs installation')
        return 1
    compile(after, str(path), 'exec')
    path.write_text(after)
    print('memory prefetch observation installed; loaded modules require an idle reload window')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
