from datetime import datetime
import json

from agentcore.model_latency_report import main, summarize


def test_aggregates_completed_calls_without_copying_sensitive_text(capsys, tmp_path):
    log = tmp_path / 'agent.log'
    log.write_text(
        '2026-10-30 09:01:02,001 INFO [private-id] agent.conversation_loop: API call #1: model=main in=100 out=20 latency=2.5s private-text\n'
        '2026-10-30 09:04:02,001 INFO [private-id] agent.conversation_loop: API call #2: model=main in=200 out=20 latency=7.5s\n'
        '2026-10-30 10:04:02,001 INFO incomplete API call without timings\n', encoding='utf-8')
    main([str(log)])
    raw = capsys.readouterr().out
    assert 'private' not in raw and 'model=main' not in raw
    assert json.loads(raw) == {'source': 'full_log', 'hours': [
        {'hour': '2026-10-30 09', 'calls': 2, 'median_seconds': 5.0,
         'max_seconds': 7.5, 'max_input_tokens': 200}]}


def test_since_filters_and_invalid_timestamps_are_ignored():
    rows = ['2026-13-30 09:00:00 INFO API call #1: in=4 latency=1s',
            '2026-10-30 09:00:00 INFO API call #1: in=4 latency=1s',
            '2026-10-30 10:00:00 INFO API call #2: in=7 latency=3s']
    result = summarize(rows, since=datetime(2026, 10, 30, 10))
    assert len(result) == 1 and result[0]['hour'] == '2026-10-30 10'
