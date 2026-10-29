#!/usr/bin/env python3
"""Install the profile-opted cron conservation check inside the memory file lock."""
import argparse
from pathlib import Path
import shutil

OLD='''            self._set_entries(target, result[0])
            from hermes_constants import mkdir_under_hermes_home
'''
NEW='''            try:
                from tools.memory_conservation_guard import check_write
                conservation = check_write(path, raw, ENTRY_DELIMITER.join(result[0]))
            except Exception as exc:
                return _error("Memory conservation gate failed: " + type(exc).__name__, "gate_refused")
            if conservation is not None:
                FAILURE_CLASS.set("staged")
                return conservation
            self._set_entries(target, result[0])
            from hermes_constants import mkdir_under_hermes_home
'''


def transform(source):
    if NEW in source:return source
    if source.count(OLD)!=1:raise ValueError('memory mutation source anchor drifted')
    after=source.replace(OLD,NEW,1);compile(after,'memory_tool_store.py','exec');return after


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--hermes-root',required=True);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();root=Path(args.hermes_root)
    path=root/'tools/memory_tool_store.py';source=path.read_text();after=transform(source)
    bridge=Path(__file__).resolve().parents[1]/'runtime/hermes_memory_conservation.py'
    installed=root/'tools/memory_conservation_guard.py'
    if not args.apply:
        ready=after==source and installed.is_file() and installed.read_bytes()==bridge.read_bytes()
        print('memory conservation '+('installed' if ready else 'needs installation'));return 0 if ready else 1
    shutil.copy2(bridge,installed)
    path.write_text(after)
    print('memory conservation installed; loaded memory tools require an idle reload window')
    return 0


if __name__=='__main__':raise SystemExit(main())
