import json
import importlib.util
import subprocess
from pathlib import Path

import pytest

_path = Path(__file__).resolve().parents[1] / 'tools' / 'install_nightly_review.py'
_spec = importlib.util.spec_from_file_location('super_agent_nightly_review_installer', _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
InstallError, install_or_check = _module.InstallError, _module.install_or_check


class FakeHermes:
    def __init__(self, home: Path, timezone='Asia/Shanghai'):
        self.home = home
        self.timezone = timezone
        self.calls = []
        self.create_count = 0

    @property
    def store(self):
        return self.home / 'cron' / 'jobs.json'

    def jobs(self):
        if not self.store.exists():
            return []
        return json.loads(self.store.read_text())['jobs']

    def save(self, jobs):
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text(json.dumps({'jobs': jobs}))

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        if args[1:4] == ['config', 'get', 'timezone']:
            return subprocess.CompletedProcess(args, 0, json.dumps(self.timezone), '')
        if args[1:3] == ['cron', 'create']:
            self.create_count += 1
            schedule, prompt = args[3], args[4]
            name = args[args.index('--name') + 1]
            deliver = args[args.index('--deliver') + 1]
            workdir = args[args.index('--workdir') + 1]
            jobs = self.jobs()
            jobs.append({'name': name, 'schedule': schedule, 'prompt': prompt,
                         'deliver': deliver, 'workdir': workdir, 'enabled': True})
            self.save(jobs)
            return subprocess.CompletedProcess(args, 0, '', '')
        raise AssertionError(args)


@pytest.fixture
def setup(tmp_path):
    home = tmp_path / 'profile home'
    home.mkdir()
    workdir = tmp_path / 'repo with spaces;safe'
    workdir.mkdir()
    fake = FakeHermes(home)
    call = lambda **kw: install_or_check(profile='team-bot', profile_home=str(home),
        workdir=str(workdir), timezone='Asia/Shanghai', hermes='/bin/true', runner=fake, **kw)
    return home, workdir, fake, call


def test_check_is_read_only_and_install_is_idempotent(setup):
    home, workdir, fake, call = setup
    assert call()[0] == 'MISSING'
    assert fake.create_count == 0
    assert not fake.store.exists()

    status, _ = call(install=True)
    assert status == 'ADDED_PENDING_RUNTIME_TZ_CHECK'
    assert fake.create_count == 1
    cmd = fake.calls[-1][0]
    assert cmd[cmd.index('--workdir') + 1] == str(workdir)
    assert cmd[cmd.index('--name') + 1] == 'nightly-session-review'
    assert fake.calls[-1][1]['env']['HERMES_HOME'] == str(home)

    assert call(install=True)[0] == 'EXISTS'
    assert fake.create_count == 1


def test_unrelated_jobs_are_preserved_and_same_name_conflict_is_not_overwritten(setup):
    home, workdir, fake, call = setup
    unrelated = {'name': 'keep-me', 'schedule': '7 4 * * *', 'prompt': 'untouched'}
    fake.save([unrelated])
    call(install=True)
    jobs = fake.jobs()
    assert unrelated in jobs
    target = next(j for j in jobs if j['name'] == 'nightly-session-review')
    target['schedule'] = '5 2 * * *'
    fake.save(jobs)
    before = fake.store.read_bytes()
    with pytest.raises(InstallError, match='existing_job_conflict'):
        call(install=True)
    assert fake.store.read_bytes() == before
    assert fake.create_count == 1


def test_known_legacy_job_is_reported_without_duplicate_creation(setup):
    home, workdir, fake, call = setup
    legacy = {'name': '夜间会话复盘与改进', 'schedule': '0 1 * * *', 'prompt': 'old prompt'}
    fake.save([legacy])
    before = fake.store.read_bytes()
    status, detail = call(install=True)
    assert status == 'LEGACY_CONFLICT'
    assert 'migrate manually' in detail
    assert fake.create_count == 0
    assert fake.store.read_bytes() == before


def test_legacy_detection_precedes_missing_timezone_query(setup):
    home, workdir, fake, call = setup
    fake.timezone = ''
    fake.save([{'name': '夜间会话复盘与改进', 'schedule': '0 1 * * *', 'prompt': 'old'}])
    assert call(install=True)[0] == 'LEGACY_CONFLICT'
    assert fake.calls == []
    assert fake.create_count == 0


def test_marker_collision_under_other_name_fails_closed(setup):
    home, workdir, fake, call = setup
    fake.save([{'name': 'someone-else', 'prompt': '[nightly-self-review-template:v1]'}])
    with pytest.raises(InstallError, match='existing_job_conflict'):
        call(install=True)
    assert fake.create_count == 0


@pytest.mark.parametrize(('timezone', 'error'), [('', 'profile_timezone_unconfigured'),
    ('Mars/Olympus', 'profile_timezone_invalid'), ('UTC', 'profile_timezone_mismatch')])
def test_timezone_must_be_explicit_and_match(timezone, error, setup):
    home, workdir, fake, call = setup
    fake.timezone = timezone
    with pytest.raises(InstallError, match=error):
        call(install=True)
    assert fake.create_count == 0


def test_invalid_schedule_time_never_calls_create(setup):
    home, workdir, fake, call = setup
    with pytest.raises(InstallError, match='invalid_time'):
        call(install=True, at='01:00; touch /tmp/nope')
    assert fake.create_count == 0


def test_create_timeout_after_commit_is_unknown_and_never_replayed(setup):
    home, workdir, fake, call = setup

    def timeout_after_create(args, **kwargs):
        result = fake(args, **kwargs)
        if args[1:3] == ['cron', 'create']:
            raise subprocess.TimeoutExpired(args, 45)
        return result

    # The timeout happens after the fake CLI committed the job.
    status, _ = install_or_check(profile='team-bot', profile_home=str(home), workdir=str(workdir),
        timezone='Asia/Shanghai', install=True, hermes='/bin/true', runner=timeout_after_create)
    assert status == 'UNKNOWN'
    assert fake.create_count == 1
    assert install_or_check(profile='team-bot', profile_home=str(home), workdir=str(workdir),
        timezone='Asia/Shanghai', install=True, hermes='/bin/true', runner=fake)[0] == 'EXISTS'
    assert fake.create_count == 1
