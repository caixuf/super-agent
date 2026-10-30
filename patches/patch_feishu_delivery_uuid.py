#!/usr/bin/env python3
"""Keep an administrator-supplied delivery UUID stable across Feishu retries."""
import argparse
from pathlib import Path

OLD = 'uuid_value=str(uuid.uuid4()),'
NEW = 'uuid_value=str((metadata or {}).get("_delivery_uuid") or uuid.uuid4()),'
ANCHOR = '    async def _send_raw_message('
MARKED = '    DURABLE_DELIVERY_UUID = True\n\n'+ANCHOR


def transform(source):
    if source.count(NEW) == 2 and OLD not in source and source.count(MARKED) == 1:
        return source
    if source.count(OLD) != 2 or NEW in source or source.count(ANCHOR) != 1 or MARKED in source:
        raise ValueError('Feishu raw-send UUID source anchor drifted')
    updated = source.replace(OLD, NEW).replace(ANCHOR, MARKED, 1)
    compile(updated, 'feishu_adapter.py', 'exec')
    return updated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-root', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    path = Path(args.hermes_root)/'plugins/platforms/feishu/adapter.py'
    before = path.read_text()
    after = transform(before)
    if before == after:
        print('Feishu delivery UUID patch already installed')
        return 0
    if not args.apply:
        print('Feishu delivery UUID patch needs installation')
        return 1
    path.write_text(after)
    print('Feishu delivery UUID patch installed; loaded adapters need an idle reload window')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
