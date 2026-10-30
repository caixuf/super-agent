import pytest

from agentcore import process_pressure


def write_snapshot(root, *, start=100, major=10, cpu=20, swap=1024, host=100):
    proc = root / '123'
    proc.mkdir(exist_ok=True)
    # /proc/<pid>/stat fields after the command name: state then fields 4..22.
    fields = ['0'] * 20
    fields[0] = 'D'
    fields[9] = str(major)
    fields[11] = str(cpu)
    fields[12] = '2'
    fields[19] = str(start)
    (proc / 'stat').write_text('123 (private command) ' + ' '.join(fields))
    (proc / 'status').write_text('VmRSS:\t4096 kB\nVmSwap:\t%d kB\n' % swap)
    (root / 'vmstat').write_text('pswpin %d\npswpout 5\npgmajfault %d\n' % (host, host))


def test_sampling_reports_process_and_host_separately(tmp_path):
    write_snapshot(tmp_path)
    def advance(_):
        write_snapshot(tmp_path, major=14, cpu=23, swap=512, host=108)
    ticks = iter((1.0, 3.0))
    result = process_pressure.sample(123, interval=2, proc_root=tmp_path,
                                     sleeper=advance, monotonic=lambda: next(ticks))
    assert result['process_major_faults'] == 4
    assert result['process_cpu_ticks'] == 3
    assert result['process_swap_kib'] == 512
    assert result['host_counter_deltas']['pswpin'] == 8
    assert 'private command' not in str(result)


def test_process_reuse_is_not_reported_as_one_sample(tmp_path):
    write_snapshot(tmp_path)
    def replace(_):
        write_snapshot(tmp_path, start=200, major=14, cpu=23, host=108)
    ticks = iter((1.0, 3.0))
    with pytest.raises(ValueError, match='identity changed'):
        process_pressure.sample(123, interval=2, proc_root=tmp_path,
                                sleeper=replace, monotonic=lambda: next(ticks))


def test_invalid_interval_is_rejected_before_reading(tmp_path):
    with pytest.raises(ValueError, match='scope'):
        process_pressure.sample(123, interval=60, proc_root=tmp_path)


def test_process_reuse_inside_snapshot_is_rejected(tmp_path, monkeypatch):
    from pathlib import Path
    write_snapshot(tmp_path)
    read_text = Path.read_text
    switched = []
    def read_and_replace(path, *args, **kwargs):
        text = read_text(path, *args, **kwargs)
        if path.name == 'vmstat' and not switched:
            switched.append(True)
            write_snapshot(tmp_path, start=200)
        return text
    monkeypatch.setattr(Path, 'read_text', read_and_replace)
    with pytest.raises(ValueError, match='during snapshot'):
        process_pressure._snapshot(123, tmp_path)
