"""Summarize model call timings from an agent log without copying prompts or IDs."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
import statistics

_LINE = re.compile(
    r'^(?P<time>\d{4}-\d\d-\d\d \d\d):\d\d:\d\d[,\d]*\s+.*?'
    r'\bAPI call #\d+:.*?\bin=(?P<input>\d+)\b.*?\blatency=(?P<latency>\d+(?:\.\d+)?)s\b'
)


def summarize(lines, *, since=None, include_buckets=False):
    """Group completed calls by log-local hour. In-flight calls are not included."""
    hours = defaultdict(list)
    buckets = defaultdict(list)
    for line in lines:
        match = _LINE.search(line)
        if not match:
            continue
        hour = match['time']
        try:
            instant = datetime.strptime(hour, '%Y-%m-%d %H')
        except ValueError:
            continue
        if since is not None and instant < since:
            continue
        input_tokens = int(match['input'])
        latency = float(match['latency'])
        hours[hour].append((input_tokens, latency))
        if include_buckets:
            bucket = 'under_50k' if input_tokens < 50000 else '50k_to_200k' if input_tokens < 200000 else '200k_or_more'
            buckets[bucket].append(latency)
    hourly = [dict(hour=hour, calls=len(values),
                 median_seconds=round(statistics.median(v[1] for v in values), 2),
                 max_seconds=max(v[1] for v in values),
                 max_input_tokens=max(v[0] for v in values))
            for hour, values in sorted(hours.items())]
    if not include_buckets:
        return hourly
    grouped = [dict(input_tokens=bucket, calls=len(values),
                    median_seconds=round(statistics.median(values), 2),
                    max_seconds=max(values), calls_over_120_seconds=sum(v >= 120 for v in values))
               for bucket, values in sorted(buckets.items())]
    return hourly, grouped


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    parser.add_argument('--tail-mib', type=int, default=8)
    parser.add_argument('--input-buckets', action='store_true', help='also group completed timings by input size')
    args = parser.parse_args(argv)
    if not 1 <= args.tail_mib <= 128:
        parser.error('--tail-mib must be between 1 and 128')
    with args.log.open('rb') as stream:
        stream.seek(0, 2)
        size = stream.tell()
        offset = max(0, size - args.tail_mib * 1024 * 1024)
        stream.seek(offset)
        if offset:
            stream.readline()  # Discard the partial first line.
        result = summarize((line.decode('utf-8', 'replace') for line in stream),
                           include_buckets=args.input_buckets)
    output = {'source': 'tail' if offset else 'full_log'}
    if args.input_buckets:
        output['hours'], output['input_buckets'] = result
    else:
        output['hours'] = result
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()
