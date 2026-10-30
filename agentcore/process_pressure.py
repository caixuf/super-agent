"""Read-only Linux process and host memory-pressure sampler.

This does not inspect command lines, send signals, or diagnose a cause. Host
swap counters are global and must not be attributed to the sampled process.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time


def _snapshot(pid: int, proc_root: Path):
    path = proc_root / str(pid)
    raw = (path / 'stat').read_text()
    end = raw.rfind(')')
    if end < 0:
        raise ValueError('invalid process stat')
    fields = raw[end + 2:].split()
    if len(fields) < 20:
        raise ValueError('incomplete process stat')
    status = {}
    for line in (path / 'status').read_text().splitlines():
        key, sep, value = line.partition(':')
        if sep and key in ('VmRSS', 'VmSwap'):
            parts = value.split()
            if len(parts) != 2 or parts[1] != 'kB':
                raise ValueError('invalid memory unit')
            status[key] = int(parts[0])
    host = {}
    for line in (proc_root / 'vmstat').read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in ('pswpin', 'pswpout', 'pgmajfault'):
            host[parts[0]] = int(parts[1])
    if set(host) != {'pswpin', 'pswpout', 'pgmajfault'}:
        raise ValueError('host counters unavailable')
    # The process can exit and its PID be reused between reading stat and
    # status. Check the identity again before reporting status values.
    last_stat = (path / 'stat').read_text()
    last_end = last_stat.rfind(')')
    last_fields = last_stat[last_end + 2:].split() if last_end >= 0 else []
    if len(last_fields) < 20 or last_fields[19] != fields[19]:
        raise ValueError('process identity changed during snapshot')
    return {'state': fields[0], 'major_faults': int(fields[9]),
            'cpu_ticks': int(fields[11]) + int(fields[12]), 'start_ticks': int(fields[19]),
            'rss_kib': status.get('VmRSS'), 'swap_kib': status.get('VmSwap'), 'host': host}


def sample(pid: int, *, interval: float = 5.0, proc_root=Path('/proc'),
           sleeper=time.sleep, monotonic=time.monotonic):
    if type(pid) is not int or pid <= 0 or not 0.1 <= interval <= 10:
        raise ValueError('invalid sampling scope')
    root = Path(proc_root)
    start = monotonic()
    before = _snapshot(pid, root)
    sleeper(interval)
    after = _snapshot(pid, root)
    elapsed = monotonic() - start
    if before['start_ticks'] != after['start_ticks']:
        raise ValueError('process identity changed during sampling')
    if elapsed <= 0:
        raise ValueError('invalid sampling clock')
    deltas = {'process_major_faults': after['major_faults'] - before['major_faults'],
              'process_cpu_ticks': after['cpu_ticks'] - before['cpu_ticks']}
    host = {key: after['host'][key] - before['host'][key] for key in before['host']}
    if min(*deltas.values(), *host.values()) < 0:
        raise ValueError('counter decreased during sampling')
    return {'ok': True, 'interval_seconds': round(elapsed, 3), 'process_state': after['state'],
            'process_rss_kib': after['rss_kib'], 'process_swap_kib': after['swap_kib'],
            **deltas, 'clock_ticks_per_second': os.sysconf('SC_CLK_TCK'),
            'host_counter_deltas': host}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--pid', required=True, type=int)
    parser.add_argument('--interval', default=5.0, type=float)
    args = parser.parse_args(argv)
    try:
        result = sample(args.pid, interval=args.interval)
    except (OSError, ValueError) as exc:
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
